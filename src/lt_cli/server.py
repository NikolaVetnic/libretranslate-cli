"""Container lifecycle: start LibreTranslate on demand, stop it again when idle.

Every API call records a "last used" timestamp. When the CLI starts (or uses) the
container it makes sure a small detached watcher process is running; the watcher
sleeps until the idle timeout has passed since the last use, then runs
`docker compose stop` and exits. A file lock guarantees at most one watcher.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .client import LibreTranslate, LTUnreachable

IS_WIN = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000

DEFAULT_IDLE_MINUTES = 15.0


class DockerError(Exception):
    pass


# --------------------------------------------------------------------------- config

def find_compose_file() -> Path:
    candidates = []
    if os.environ.get("LT_COMPOSE_FILE"):
        candidates.append(Path(os.environ["LT_COMPOSE_FILE"]))
    candidates.append(Path.cwd() / "docker-compose.yml")
    # repo root when installed in editable mode: src/lt_cli/server.py -> ../../
    candidates.append(Path(__file__).resolve().parents[2] / "docker-compose.yml")
    for c in candidates:
        if c.is_file():
            return c.resolve()
    raise FileNotFoundError(
        "docker-compose.yml not found; set LT_COMPOSE_FILE or run from the project directory"
    )


def load_dotenv() -> None:
    """Use the project's .env as defaults (real environment variables take precedence)."""
    try:
        path = find_compose_file().parent / ".env"
    except FileNotFoundError:
        return
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def idle_timeout_seconds() -> float:
    """LT_IDLE_TIMEOUT in minutes; 0 or negative disables auto-stop."""
    try:
        return float(os.environ.get("LT_IDLE_TIMEOUT", DEFAULT_IDLE_MINUTES)) * 60
    except ValueError:
        return DEFAULT_IDLE_MINUTES * 60


def autostart_enabled() -> bool:
    return os.environ.get("LT_AUTOSTART", "1").strip().lower() not in {"0", "false", "no", "off"}


def is_local(url: str) -> bool:
    return urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}


# --------------------------------------------------------------------------- docker compose

def compose(*args: str, capture: bool = False) -> subprocess.CompletedProcess:
    if not shutil.which("docker"):
        raise DockerError("docker is not on PATH")
    f = find_compose_file()
    kwargs: dict = {"cwd": f.parent}
    if capture:
        kwargs.update(capture_output=True, text=True, encoding="utf-8", errors="replace")
        if IS_WIN:
            kwargs["creationflags"] = _CREATE_NO_WINDOW
    return subprocess.run(["docker", "compose", "-f", str(f), *args], **kwargs)


def wait_ready(url: str, timeout: float, progress: bool = True) -> bool:
    probe = LibreTranslate(url, timeout=3)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe.is_ready():
            return True
        if progress:
            print(".", end="", file=sys.stderr, flush=True)
        time.sleep(1)
    return False


def start(url: str, wait_timeout: float = 900) -> float:
    """Start the container and wait until the API answers. Returns seconds taken."""
    t0 = time.monotonic()
    r = compose("up", "-d", capture=True)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        msg = detail[-1] if detail else f"exit code {r.returncode}"
        if "docker API" in msg or "daemon" in msg:
            msg = "the Docker engine is not running (start Docker Desktop)"
        raise DockerError(f"could not start LibreTranslate: {msg}")
    if not wait_ready(url, wait_timeout):
        raise LTUnreachable(f"LibreTranslate did not become ready within {wait_timeout:.0f}s")
    return time.monotonic() - t0


def ensure_running(url: str) -> None:
    """Start the local container if the API isn't answering (no-op when it is)."""
    if LibreTranslate(url, timeout=3).is_ready():
        return
    print("lt: starting LibreTranslate", end="", file=sys.stderr, flush=True)
    try:
        took = start(url)
    except Exception:
        print(file=sys.stderr)
        raise
    print(f" ready ({took:.0f}s)", file=sys.stderr)


# --------------------------------------------------------------------------- idle tracking

def state_dir() -> Path:
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    d = base / "lt-cli"
    d.mkdir(parents=True, exist_ok=True)
    return d


def touch_activity() -> None:
    p = state_dir() / "last_used"
    p.touch()
    os.utime(p)


def last_activity() -> float:
    try:
        return (state_dir() / "last_used").stat().st_mtime
    except FileNotFoundError:
        return 0.0


class _FileLock:
    """Non-blocking exclusive lock held for the lifetime of the watcher process."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+")
        try:
            if IS_WIN:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self) -> None:
        if not self.fh:
            return
        try:
            if IS_WIN:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


def _lock() -> _FileLock:
    return _FileLock(state_dir() / "watcher.lock")


def watcher_running() -> bool:
    lock = _lock()
    if lock.acquire():
        lock.release()
        return False
    return True


def spawn_watcher() -> None:
    """Start the detached idle watcher unless auto-stop is disabled or one is running."""
    if idle_timeout_seconds() <= 0 or watcher_running():
        return
    exe = Path(sys.executable)
    if IS_WIN and (exe.parent / "pythonw.exe").is_file():
        exe = exe.parent / "pythonw.exe"  # no console window
    cmd = [str(exe), "-m", "lt_cli.server", "--watch"]
    env = {**os.environ, "LT_COMPOSE_FILE": str(find_compose_file())}
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, env=env, close_fds=True)
    if IS_WIN:
        flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
        try:  # survive the terminal closing, if the job object allows it
            subprocess.Popen(cmd, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            return
        except OSError:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def record_use() -> None:
    """Mark the server as just used and make sure it will be stopped once idle."""
    touch_activity()
    spawn_watcher()


def watch() -> int:
    """Watcher loop: stop the container once it has been idle for the timeout."""
    lock = _lock()
    if not lock.acquire():
        return 0  # another watcher is already on duty
    try:
        while True:
            timeout = idle_timeout_seconds()
            if timeout <= 0:
                return 0
            idle = time.time() - last_activity()
            if idle >= timeout:
                compose("stop", capture=True)
                return 0
            time.sleep(min(timeout - idle + 1, 3600))
    finally:
        lock.release()


if __name__ == "__main__":
    if "--watch" in sys.argv[1:]:
        sys.exit(watch())
