"""Child-process helpers.

On Windows every child we start (inference engine, training scripts) is put into
a Job object with KILL_ON_JOB_CLOSE. The job handle lives as long as this
process, so if the backend exits or is killed by the UI, the OS tears down the
children too and no orphaned GPU process is left behind.
"""

from __future__ import annotations

import locale
import os
import subprocess
import sys
import threading
import time

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_ulonglong)
            for n in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD

    _JobObjectExtendedLimitInformation = 9
    _KILL_ON_JOB_CLOSE = 0x2000
    _SYNCHRONIZE = 0x00100000
    _job_handle = None
    _job_lock = threading.Lock()

    def _job() -> int | None:
        global _job_handle
        with _job_lock:
            if _job_handle is None:
                h = _k32.CreateJobObjectW(None, None)
                if not h:
                    return None
                info = _EXTENDED_LIMIT()
                info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
                if not _k32.SetInformationJobObject(
                    h, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
                ):
                    return None
                _job_handle = h
            return _job_handle


CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def spawn(args: list[str], cwd: str, env: dict[str, str] | None = None, encoding: str | None = None, **kw) -> subprocess.Popen:
    """Start a child bound to this process's lifetime, stdout+stderr merged as text.

    Children started with `python -I` ignore PYTHONIOENCODING and print in the
    system ANSI code page, so that is the default decoding here.
    """
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_FLAGS,
        encoding=encoding or locale.getencoding(),
        errors="replace",
        bufsize=1,
        **kw,
    )
    if os.name == "nt":
        job = _job()
        if job:
            _k32.AssignProcessToJobObject(job, int(proc._handle))  # type: ignore[attr-defined]
    return proc


def child_env(extra_path: list[str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    if extra_path:
        env["PATH"] = os.pathsep.join(extra_path + [env.get("PATH", "")])
    return env


def watch_parent(pid: int, on_exit) -> None:
    """Call on_exit() once the given parent process has gone away."""

    def run() -> None:
        if os.name == "nt":
            h = _k32.OpenProcess(_SYNCHRONIZE, False, pid)
            if not h:
                on_exit()
                return
            _k32.WaitForSingleObject(h, 0xFFFFFFFF)
            on_exit()
        else:
            while True:
                try:
                    os.kill(pid, 0)
                except OSError:
                    on_exit()
                    return
                time.sleep(1)

    threading.Thread(target=run, name="parent-watch", daemon=True).start()


def python_exe() -> str:
    return sys.executable
