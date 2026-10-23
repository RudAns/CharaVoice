"""Small synchronous client used by the CLI and the MCP server."""

from __future__ import annotations

import contextlib
from typing import Iterator

import httpx

from . import config


class BackendDown(RuntimeError):
    pass


class ApiFailure(RuntimeError):
    def __init__(self, status: int, message: str, code: str = ""):
        super().__init__(message)
        self.status, self.code = status, code


@contextlib.contextmanager
def connect(timeout: float = 30.0) -> Iterator[httpx.Client]:
    info = config.read_runtime()
    if not info:
        raise BackendDown("CharaVoice 没有在运行。请先打开 CharaVoice（或运行 start.bat / charavoice serve）。")
    with httpx.Client(base_url=info["base_url"], headers={"Authorization": f"Bearer {info['token']}"},
                      timeout=httpx.Timeout(timeout, connect=3)) as c:
        try:
            c.get("/health", timeout=3)
        except httpx.HTTPError:
            raise BackendDown(f"无法连接 CharaVoice 后端（{info['base_url']}）。它可能已经退出，请重新打开 CharaVoice。") from None
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
