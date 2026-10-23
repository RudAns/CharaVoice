"""charavoice command line.

    charavoice serve [--port N] [--parent-pid PID]   run the backend
    charavoice speak "文字" [--voice X] [--lang zh|en|ja] [--no-wait] [--out a.wav]
    charavoice listen [--timeout 15] [--json]
    charavoice voices
    charavoice status
    charavoice stop                                  stop current playback
"""

from __future__ import annotations

import argparse
import json
import os
import sys

EXIT_BACKEND_DOWN = 2
EXIT_API_ERROR = 3


def _out(s: str) -> None:
    sys.stdout.write(s + "\n")
    sys.stdout.flush()


def cmd_serve(a) -> int:
    import logging

    import uvicorn

    from . import config, procutil

    config.ensure_dirs()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(config.LOG_DIR / "backend.log", encoding="utf-8"), logging.StreamHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = config.load_settings()["server"]
    port = a.port or int(s["port"])
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            sys.stderr.write(f"错误：端口 {port} 已被占用（可能 CharaVoice 已经在运行）。\n")
            return EXIT_BACKEND_DOWN
    from . import server

    server.RUNTIME.update(config.write_runtime(port, os.getpid()))
    if a.parent_pid:
        procutil.watch_parent(a.parent_pid, lambda: os._exit(0))
    _out("CHARAVOICE_READY " + json.dumps({"base_url": server.RUNTIME["base_url"], "pid": os.getpid()}))
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning", ws_ping_interval=20)
    return 0


def cmd_speak(a) -> int:
    from .client import call, connect

    text = " ".join(a.text).strip() if a.text else sys.stdin.read().strip()
    with connect(timeout=900) as c:
        if a.out:
            r = call(c, "POST", "/v1/audio/speech", json={"input": text, "voice": a.voice, "language": a.lang,
                                                        "response_format": os.path.splitext(a.out)[1].lstrip(".") or "wav", "play": not a.no_play})
            with open(a.out, "wb") as f:
                f.write(r.content)
            _out(f"已保存 {a.out}（声音 {r.headers.get('x-charavoice-voice')}）")
            return 0
        r = call(c, "POST", "/v1/speak", json={"text": text, "voice": a.voice, "language": a.lang, "wait": not a.no_wait}).json()
        if a.json:
            _out(json.dumps(r, ensure_ascii=False))
        else:
            secs = f"，{r['audio_seconds']} 秒" if r.get("audio_seconds") else ""
            _out(f"[{r['voice']} · {r['language']}] 已播放{secs}" if r["finished"] else f"[{r['voice']} · {r['language']}] 正在播放")
    return 0


def cmd_listen(a) -> int:
    from .client import call, connect

    with connect(timeout=a.timeout + a.max_seconds + 30) as c:
        if not a.json:
            sys.stderr.write("正在听……请说话\n")
            sys.stderr.flush()
        r = call(c, "POST", "/v1/listen", json={"timeout": a.timeout, "max_seconds": a.max_seconds}).json()
    if a.json:
        _out(json.dumps(r, ensure_ascii=False))
    elif r.get("timeout"):
        sys.stderr.write("（超时，没有听到说话）\n")
        return 1
    else:
        _out(r["text"])
    return 0


def cmd_voices(a) -> int:
    from .client import call, connect

    with connect() as c:
        data = call(c, "GET", "/v1/voices").json()["data"]
    if a.json:
        _out(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    if not data:
        _out("还没有声音。请在 CharaVoice 界面里新建。")
    for v in data:
        flag = "*" if v.get("default") else " "
        kind = "已训练" if v["trained"] else ("零样本" if v["ready"] else "缺参考音频")
        _out(f"{flag} {v['id']:<24} {v['name']}  [{v['language']} · {kind}]")
    return 0


def cmd_status(a) -> int:
    from .client import call, connect

    with connect() as c:
        r = call(c, "GET", "/v1/status").json()
    _out(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_stop(a) -> int:
    from .client import call, connect

    with connect() as c:
        r = call(c, "POST", "/v1/audio/stop").json()
    _out(f"已停止 {r['stopped']} 段语音")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="charavoice", description="CharaVoice：本地角色声音（说 / 听）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="启动后端")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--parent-pid", type=int, default=0)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("speak", help="合成并播放")
    s.add_argument("text", nargs="*", help="要说的文字（省略则从标准输入读取）")
    s.add_argument("--voice", "-v")
    s.add_argument("--lang", "-l", help="zh / en / ja / auto（默认按文字自动判断）")
    s.add_argument("--no-wait", action="store_true", help="开始播放就返回，不等播完")
    s.add_argument("--out", "-o", help="保存为文件（wav/mp3/flac/opus）而不是只播放")
    s.add_argument("--no-play", action="store_true", help="配合 --out：只保存不播放")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_speak)

    s = sub.add_parser("listen", help="听一句话并输出文字")
    s.add_argument("--timeout", type=float, default=15.0, help="多少秒内没开口就放弃")
    s.add_argument("--max-seconds", type=float, default=60.0)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_listen)

    for name, fn, h in (("voices", cmd_voices, "列出声音"), ("status", cmd_status, "后端状态"), ("stop", cmd_stop, "停止播放")):
        s = sub.add_parser(name, help=h)
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=fn)

    a = p.parse_args(argv)
    from .client import ApiFailure, BackendDown

    try:
        return a.fn(a)
    except BackendDown as e:
        sys.stderr.write(f"错误：{e}\n")
        return EXIT_BACKEND_DOWN
    except ApiFailure as e:
        sys.stderr.write(f"错误：{e}\n")
        return EXIT_API_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
