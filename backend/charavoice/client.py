"""Small synchronous client used by the CLI and the MCP server.

connect() finds the running backend through data/runtime.json. When none is running (the app is
closed), it starts one in the background without a window and waits for it, so an agent can use
CharaVoice without the user opening the app first; an app opened later reuses that backend.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from typing import Iterator

import httpx

from . import config

START_WAIT = 90.0  # seconds to wait for a freshly started backend to answer


class BackendDown(RuntimeError):
    pass


class ApiFailure(RuntimeError):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


def _alive(info: dict | None) -> bool:
    """The backend described by runtime.json answers and accepts its token (a stale file from a
    backend that died can point at the port of a newer one that has not rewritten it yet)."""
    if not info:
        return False
    try:
        r = httpx.get(info["base_url"] + "/v1/status", headers={"Authorization": f"Bearer {info['token']}"}, timeout=3)
        return r.status_code == 200
    except (httpx.HTTPError, KeyError):
        return False


def _spawn_backend() -> None:
    """Start `charavoice serve` detached and headless (it outlives the agent's MCP process)."""
    if getattr(sys, "frozen", False):  # installed app: the backend exe is also the CLI / MCP server
        args = [sys.executable, "serve"]
    else:
        args = [sys.executable, "-m", "charavoice", "serve"]
    if os.name != "nt":
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        return
    # MCP clients start their server inside a job object that kills every process in it when the
    # agent exits, and usually forbid breaking away. A process created through WMI is not part of
    # that job, so the backend (and the models it loaded) stays up for the next call and the app.
    cmd = subprocess.list2cmdline(args)
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          "@{CommandLine=$env:CV_CMD; CurrentDirectory=$env:CV_CWD; ProcessStartupInformation=$si}; exit $r.ReturnValue")
    env = {**os.environ, "CV_CMD": cmd, "CV_CWD": os.getcwd()}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env,
                           capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    # fallback: a plain detached child (lives as long as the agent's MCP process)
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def ensure_backend(autostart: bool = True) -> dict:
    info = config.read_runtime()
    if _alive(info):
        return info  # type: ignore[return-value]
    if not autostart:
        raise BackendDown("CharaVoice 没有在运行。请先打开 CharaVoice（或运行 charavoice serve）。")
    config.ensure_dirs()
    # one starter at a time: two agents connecting at once must not start two backends
    lock = config.DATA_DIR / "backend.starting"
    mine = False
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > START_WAIT:
            lock.unlink(missing_ok=True)  # left over from a start that died
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        mine = True
        _spawn_backend()
    except FileExistsError:
        pass
    try:
        end = time.monotonic() + START_WAIT
        while time.monotonic() < end:
            info = config.read_runtime()
            if _alive(info):
                return info  # type: ignore[return-value]
            time.sleep(0.5)
    finally:
        if mine:
            lock.unlink(missing_ok=True)
    raise BackendDown("无法在后台启动 CharaVoice。请打开 CharaVoice 软件看看是否有报错（日志在数据目录的 logs\\backend.log）。")


@contextlib.contextmanager
def connect(timeout: float = 30.0, autostart: bool = True) -> Iterator[httpx.Client]:
    info = ensure_backend(autostart)
    with httpx.Client(base_url=info["base_url"], headers={"Authorization": f"Bearer {info['token']}"},
                      timeout=httpx.Timeout(timeout, connect=3)) as c:
        yield c


def call(c: httpx.Client, method: str, path: str, **kw):
    try:
        r = c.request(method, path, **kw)
    except httpx.TimeoutException:
        raise ApiFailure(504, "请求超时：CharaVoice 没有及时响应。") from None
    except httpx.HTTPError as e:
        raise BackendDown(f"与 CharaVoice 通信失败：{e}") from None
    if r.status_code >= 400:
        try:
            err = r.json()["error"]
            raise ApiFailure(r.status_code, err["message"], err.get("code", ""))
        except (ValueError, KeyError, TypeError):
            raise ApiFailure(r.status_code, r.text[:300]) from None
    return r
