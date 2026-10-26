"""Start, reuse, and stop a detached Studio using only the standard library."""

import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .lab import Lab, read_text

READY_TIMEOUT = 20
STOP_TIMEOUT = 3


def pid_alive(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied still means alive.
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _state(path: Path) -> dict:
    try:
        value = json.loads(read_text(path))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class _TokenMeta(HTMLParser):
    found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta" and dict(attrs).get("name") == "sfx-lab-token":
            self.found = True


def _studio_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost")
                and parts.port is not None and parts.path == "/" and not parts.username)
    except (TypeError, ValueError, AttributeError):
        return False


def _healthy(state: dict) -> bool:
    if not pid_alive(state.get("pid")) or not _studio_url(state.get("url")):
        return False
    try:
        with urllib.request.urlopen(state["url"], timeout=2) as response:
            parser = _TokenMeta()
            parser.feed(response.read().decode("utf-8", errors="replace"))
            return response.status == 200 and parser.found
    except (OSError, ValueError):
        return False


def open_browser(url: str) -> None:
    if os.environ.get("SFX_LAB_NO_BROWSER") != "1":
        webbrowser.open(url)


def _terminate(pid: int) -> bool:
    if not pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)
    except ProcessLookupError:
        pass
    return True


def open_studio(lab: Lab) -> str:
    path = lab.safe_path(".sfx-lab/studio.json")
    state = _state(path)
    if _healthy(state):
        url = state["url"]
    else:
        path.unlink(missing_ok=True)
        log_path = lab.safe_path(".sfx-lab/studio.log")
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
                   if os.name == "nt" else {"start_new_session": True})
        # Select the persisted active project even when the caller has an env override.
        env = os.environ.copy()
        env.pop("SFX_LAB_PROJECT", None)
        with log_path.open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "sfx_lab.cli", "--lab", str(lab.root),
                 "studio", "--no-open", "--port", "0", "--ready-json"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                env=env, **options)
        lines = queue.Queue()

        def read_ready() -> None:
            try:
                lines.put(child.stdout.readline(65536))
            except OSError:
                lines.put(b"")

        threading.Thread(target=read_ready, daemon=True).start()
        try:
            ready = json.loads(lines.get(timeout=READY_TIMEOUT))
            url = ready["url"]
            if not _studio_url(url):
                raise ValueError("Invalid Studio URL")
        except (queue.Empty, ValueError, KeyError, TypeError) as error:
            _terminate(child.pid)
            child.wait()
            path.unlink(missing_ok=True)
            tail = "\n".join(read_text(log_path).splitlines()[-10:])
            raise RuntimeError(f"Studio did not become ready within {READY_TIMEOUT} seconds.\n{tail}") from error
        finally:
            child.stdout.close()
    open_browser(url)
    return url


def stop_studio(lab: Lab) -> bool:
    path = lab.safe_path(".sfx-lab/studio.json")
    stopped = _terminate(_state(path).get("pid"))
    path.unlink(missing_ok=True)
    return stopped
