"""GPT-SoVITS inference engine, run as a separate process via its own api_v2.py.

We never import GPT-SoVITS code: the official Windows package ships its own
Python runtime, and we talk to it over HTTP on 127.0.0.1. Endpoints used
(see api_v2.py in the package): POST /tts, GET /set_gpt_weights,
GET /set_sovits_weights. api_v2 has no health endpoint, so readiness is probed
with FastAPI's built-in GET /docs.
"""

from __future__ import annotations

import asyncio
import collections
import io
import logging
import os
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import httpx
import numpy as np
import soundfile as sf

from . import config, procutil
from .events import THINKING, bus

log = logging.getLogger("charavoice.engine")

# Base weights shipped in the package, relative to the engine folder.
BASE_MODELS: dict[str, tuple[str, str]] = {
    "v2Pro": ("GPT_SoVITS/pretrained_models/s1v3.ckpt", "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2Pro.pth"),
    "v2ProPlus": ("GPT_SoVITS/pretrained_models/s1v3.ckpt", "GPT_SoVITS/pretrained_models/v2Pro/s2Gv2ProPlus.pth"),
    "v5turbo": ("GPT_SoVITS/pretrained_models/s1v3.ckpt", "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth"),
}

STOPPED, STARTING, READY, ERROR = "stopped", "starting", "ready", "error"


class EngineUnavailable(RuntimeError):
    """Raised with a user-facing message when synthesis cannot run."""

    def __init__(self, message: str, code: str = "engine_unavailable"):
        super().__init__(message)
        self.code = code


@dataclass
class SynthRequest:
    text: str
    text_lang: str
    ref_audio: str
    prompt_text: str
    prompt_lang: str
    gpt_weights: str
    sovits_weights: str
    aux_refs: list[str] | None = None
    speed: float = 1.0
    streaming_mode: int = 1
    seed: int = -1
    top_k: int = 15
    top_p: float = 1.0
    temperature: float = 1.0


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


class Engine:
    def __init__(self) -> None:
        self.state = STOPPED
        self.message = "引擎未启动"
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.loaded: tuple[str, str] | None = None
        self.logs: collections.deque[str] = collections.deque(maxlen=400)
        self._start_lock = asyncio.Lock()
        self._synth_lock = asyncio.Lock()
        self._ready_event = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        # set while a training run owns the GPU: synthesis is refused instead of auto-starting
        self.hold: str | None = None

    # ---------- paths ----------
    @property
    def settings(self) -> dict:
        return config.load_settings()["engine"]

    @property
    def dir(self) -> Path:
        return config.resolve(self.settings["dir"])

    def installed(self) -> bool:
        d = self.dir
        return (d / "api_v2.py").exists() and (d / "runtime" / "python.exe").exists()

    def base_weights(self, version: str | None = None) -> tuple[str, str]:
        version = version or self.settings["version"]
        if version not in BASE_MODELS:
            raise EngineUnavailable(f"未知的基础模型版本：{version}", "bad_request")
        return BASE_MODELS[version]

    def info(self) -> dict:
        return {
            "state": self.state,
            "message": self.message,
            "installed": self.installed(),
            "port": self.port or None,
            "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
            "version": self.settings["version"],
            "loaded": list(self.loaded) if self.loaded else None,
            "hold": self.hold,
        }

    def _set(self, state: str, message: str) -> None:
        self.state, self.message = state, message
        if state == READY:
            self._ready_event.set()
        else:
            self._ready_event.clear()
        bus.publish("engine", **self.info())

    # ---------- lifecycle ----------
    def _write_config(self) -> Path:
        s = self.settings
        gpt, sovits = self.base_weights(s["version"])
        cfg = config.ENGINE_DATA_DIR / "tts_infer.yaml"
        config.ensure_dirs()
        lines = [
            "custom:",
            "  bert_base_path: GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large",
            "  cnhuhbert_base_path: GPT_SoVITS/pretrained_models/chinese-hubert-base",
            f"  device: {s['device']}",
            f"  is_half: {'true' if s['is_half'] else 'false'}",
            f"  t2s_weights_path: {gpt}",
            f"  version: {s['version']}",
            f"  vits_weights_path: {sovits}",
        ]
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.loaded = (gpt, sovits)
        return cfg

    def _pump_logs(self, proc: subprocess.Popen) -> None:
        log_path = config.LOG_DIR / "engine.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n===== engine start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                self.logs.append(line)
                f.write(line + "\n")
                f.flush()
        code = proc.wait()
        if self.proc is proc and self.state != STOPPED:
            loop = bus._loop
            msg = f"引擎进程意外退出（退出码 {code}），详见 data/logs/engine.log"
            if loop and not loop.is_closed():
                loop.call_soon_threadsafe(self._set, ERROR, msg)

    async def start(self) -> None:
        async with self._start_lock:
            if self.proc and self.proc.poll() is None and self.state in (STARTING, READY):
                return
            if not self.installed():
                self._set(ERROR, f"没有找到 GPT-SoVITS 引擎（{self.settings['dir']}），请先运行 setup.bat 安装")
                return
            s = self.settings
            port = int(s["port"])
            for candidate in range(port, port + 20):
                if _port_free(candidate):
                    port = candidate
                    break
            else:
                self._set(ERROR, f"端口 {s['port']} 起的 20 个端口都被占用，无法启动引擎")
                return
            self.port = port
            cfg = self._write_config()
            d = self.dir
            args = [str(d / "runtime" / "python.exe"), "-I", "api_v2.py", "-a", "127.0.0.1", "-p", str(port), "-c", str(cfg)]
            self._set(STARTING, "正在加载声音模型…")
            config.ensure_dirs()
            self.proc = procutil.spawn(args, cwd=str(d), env=procutil.child_env([str(d / "runtime")]))
            threading.Thread(target=self._pump_logs, args=(self.proc,), daemon=True).start()
            self._client = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", timeout=httpx.Timeout(float(s.get("synth_timeout", 90)), connect=5)
            )
            deadline = time.monotonic() + float(s["start_timeout"])
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    tail = "\n".join(list(self.logs)[-8:])
                    self._set(ERROR, f"引擎启动失败（退出码 {self.proc.returncode}）。日志末尾：\n{tail}")
                    return
                try:
                    r = await self._client.get("/docs", timeout=2)
                    if r.status_code == 200:
                        self._set(READY, "引擎就绪")
                        if s.get("warmup", True):
                            asyncio.create_task(self._warmup())
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
            self._set(ERROR, f"引擎在 {s['start_timeout']} 秒内没有就绪，已放弃等待；详见 data/logs/engine.log")
            await self._kill()

    async def _kill(self) -> None:
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def stop(self) -> None:
        async with self._start_lock:
            self._set(STOPPED, "引擎已停止")
            await self._kill()
            self.proc = None
            self.loaded = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def ensure_ready(self, wait: float = 90.0) -> None:
        if self.hold:
            raise EngineUnavailable(self.hold, "engine_busy")
        if self.state == READY and self.proc and self.proc.poll() is None:
            return
        if self.state in (STOPPED, ERROR):
            if self.state == ERROR and not self.installed():
                raise EngineUnavailable(self.message)
            if not self.settings["autostart"]:
                raise EngineUnavailable("声音引擎未启动。请在 CharaVoice 界面点“启动引擎”，或调用 POST /v1/engine/start")
            asyncio.create_task(self.start())
            await asyncio.sleep(0)
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            raise EngineUnavailable("声音引擎仍在启动中，请稍后再试", "engine_starting") from None
        if self.state != READY:
            raise EngineUnavailable(self.message)

    async def _warmup(self) -> None:
        """One short synthesis with the default voice so kernels and caches are hot."""
        from . import speech, voices

        try:
            v = voices.find(None)
            req = speech.build_request(v, "你好。" if v.get("language") != "en" else "Hello.", None, streaming_mode=0)
        except (LookupError, EngineUnavailable):
            return
        t0 = time.monotonic()
        try:
            async for _ in self.synthesize(req, presence=False):
                pass
            log.info("engine warm-up done in %.1fs", time.monotonic() - t0)
        except EngineUnavailable as e:
            log.warning("engine warm-up failed: %s", e)

    # ---------- inference ----------
    async def _load_weights(self, gpt: str, sovits: str) -> None:
        assert self._client is not None
        if self.loaded and self.loaded[0] == gpt and self.loaded[1] == sovits:
            return
        cur = self.loaded or ("", "")
        if cur[0] != gpt:
            r = await self._client.get("/set_gpt_weights", params={"weights_path": gpt})
            if r.status_code != 200:
                raise EngineUnavailable(f"加载 GPT 权重失败：{r.text}", "weights_failed")
        if cur[1] != sovits:
            r = await self._client.get("/set_sovits_weights", params={"weights_path": sovits})
            if r.status_code != 200:
                raise EngineUnavailable(f"加载 SoVITS 权重失败：{r.text}", "weights_failed")
        self.loaded = (gpt, sovits)

    async def synthesize(self, req: SynthRequest, presence: bool = True) -> AsyncIterator[tuple[int, np.ndarray]]:
        """Yield (sample_rate, int16 mono chunk) as the engine produces audio."""
        await self.ensure_ready()
        assert self._client is not None
        payload = {
            "text": req.text,
            "text_lang": req.text_lang,
            "ref_audio_path": req.ref_audio,
            "aux_ref_audio_paths": req.aux_refs or [],
            "prompt_text": req.prompt_text,
            "prompt_lang": req.prompt_lang,
            "top_k": req.top_k,
            "top_p": req.top_p,
            "temperature": req.temperature,
            "text_split_method": "cut5",
            "batch_size": 1,
            "speed_factor": req.speed,
            "seed": req.seed,
            "media_type": "wav",
            "streaming_mode": int(req.streaming_mode),
            "parallel_infer": True,
            "use_cuda_graph": bool(self.settings.get("use_cuda_graph", True)),
        }
        async with self._synth_lock:
            if presence:
                bus.enter(THINKING)
            try:
                await self._load_weights(req.gpt_weights, req.sovits_weights)
                if req.streaming_mode == 0:
                    r = await self._client.post("/tts", json=payload)
                    if r.status_code != 200:
                        raise EngineUnavailable(f"合成失败：{_err_text(r.content)}", "synthesis_failed")
                    data, sr = sf.read(io.BytesIO(r.content), dtype="int16")
                    yield sr, data.reshape(-1)
                    return
                async with self._client.stream("POST", "/tts", json=payload) as r:
                    if r.status_code != 200:
                        body = await r.aread()
                        raise EngineUnavailable(f"合成失败：{_err_text(body)}", "synthesis_failed")
                    header = b""
                    sr = 0
                    rest = b""
                    async for chunk in r.aiter_bytes():
                        if not sr:
                            header += chunk
                            if len(header) < 44:
                                continue
                            sr, offset = _parse_wav_header(header)
                            chunk = header[offset:]
                        buf = rest + chunk
                        usable = len(buf) - (len(buf) % 2)
                        rest = buf[usable:]
                        if usable:
                            yield sr, np.frombuffer(buf[:usable], dtype="<i2").copy()
            except httpx.TimeoutException as e:
                limit = self.settings.get("synth_timeout", 90)
                asyncio.create_task(self.restart())
                raise EngineUnavailable(
                    f"声音引擎超过 {limit} 秒没有返回音频（显卡可能被其他程序占满），已自动重启引擎，请稍后重试", "synthesis_timeout"
                ) from e
            except httpx.HTTPError as e:
                raise EngineUnavailable(f"与声音引擎通信失败：{type(e).__name__} {e}", "engine_unavailable") from e
            finally:
                if presence:
                    bus.leave(THINKING)


def _parse_wav_header(buf: bytes) -> tuple[int, int]:
    """Return (sample_rate, offset of first PCM byte) for a RIFF/WAVE header."""
    if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise EngineUnavailable("引擎返回了无法识别的音频格式", "synthesis_failed")
    pos, sr = 12, 0
    while pos + 8 <= len(buf):
        cid, size = buf[pos : pos + 4], struct.unpack("<I", buf[pos + 4 : pos + 8])[0]
        if cid == b"fmt ":
            sr = struct.unpack("<I", buf[pos + 12 : pos + 16])[0]
        if cid == b"data":
            return sr, pos + 8
        pos += 8 + size
    return sr or 32000, 44


def _err_text(body: bytes) -> str:
    try:
        import json

        d = json.loads(body)
        return f"{d.get('message', '')} {d.get('Exception', '')}".strip()
    except Exception:
        return body[:300].decode("utf-8", "replace")


engine = Engine()
