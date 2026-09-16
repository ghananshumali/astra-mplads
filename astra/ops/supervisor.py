"""Run ASTRA unattended: start everything, restart what stops, say what is wrong.

    python -m astra.ops.supervisor              # API, poller and website
    python -m astra.ops.supervisor --no-web     # API and poller only
    python -m astra.ops.supervisor --status     # what the running one reports
    python -m astra.ops.supervisor --stop       # stop the running one and everything it started

`scripts/install_autostart.ps1` registers it to start at Windows sign-in.
`run_dev.ps1` stays the way to work on the code; the two use the same ports,
so stop one before starting the other.

What it does, once a second and once a minute
---------------------------------------------
* Starts the API (uvicorn on 127.0.0.1:8000), the poller and the website (Vite
  on 5173), each with its output in `data/logs/<name>.log`.
* Restarts a process that exits, after 5 s, doubling to at most 5 min while it
  keeps failing, and back to 5 s once it has run for 10 min. A poller that
  exits because another poller already holds the database (exit code 3) is
  tried again in 5 min without complaint: the data is still being kept current.
* Every minute: checks the API answers and the website accepts connections,
  assesses health (`astra.ops.health`), sends what is due (`astra.ops.notify`),
  takes the daily backup when it is owed (`astra.ops.backup`), and records all
  of it in `data/processed/ops_state.json` for `/meta/ops` and `--status`.

Everything it starts is placed in a Windows job object that closes with the
supervisor, so ending the supervisor, however it ends, ends its processes too;
nothing is left holding the ports or the database.

It never sends a request to the eSAKSHI portal; only the poller does.

Keeping the machine awake
-------------------------
Updates stop while the laptop sleeps. With ASTRA_KEEP_AWAKE=1 the supervisor
asks Windows not to sleep while it runs and the laptop is on mains power; it
changes no power setting, and the request ends with the process.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..config import DB_PATH, PROJECT_ROOT
from ..ingestion import instance_lock
from ..ingestion.instance_lock import ALREADY_RUNNING
from . import backup, health, state
from .notify import LOG_DIR, Notifier

API_PORT = int(os.environ.get("ASTRA_API_PORT", "8000"))
WEB_PORT = int(os.environ.get("ASTRA_WEB_PORT", "5173"))
KEEP_AWAKE = os.environ.get("ASTRA_KEEP_AWAKE", "0") == "1"
#: One supervisor per database, locked beside it like the poller.
LOCK_PATH = DB_PATH.with_name(DB_PATH.name + ".supervisor.lock")
#: `--stop` asks by creating this file; the supervisor ends cleanly within a
#: second. A clean exit (code 0) matters: Task Scheduler restarts a task that
#: exits with an error, which would undo a forced stop within a minute.
STOP_PATH = DB_PATH.with_name(DB_PATH.name + ".supervisor.stop")
#: A log file larger than this is rotated when its process next starts.
LOG_MAX_BYTES = 10 * 1024 ** 2

FIRST_BACKOFF = 5.0
MAX_BACKOFF = 300.0
#: A process that ran this long before exiting starts the backoff again.
STABLE_AFTER = 600.0
TICK_SECONDS = 60.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
log = logging.getLogger("astra.supervisor")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _python() -> str:
    """The console interpreter beside this one: children launched from a
    windowless `pythonw.exe` should still write their output normally."""
    exe = Path(sys.executable)
    if exe.stem.lower().startswith("pythonw"):
        console = exe.with_name(exe.name.lower().replace("pythonw", "python"))
        if console.exists():
            return str(console)
    return str(exe)


# ------------------------------------------------------------ job object
class _Job:
    """A Windows job object that kills every process in it when it closes."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        self._k = kernel32
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000     # KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info),
                                                ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            return
        self.handle = handle

    def add(self, pid: int) -> bool:
        if not self.handle:
            return False
        process = self._k.OpenProcess(0x0101, False, pid)   # SET_QUOTA | TERMINATE
        if not process:
            return False
        try:
            return bool(self._k.AssignProcessToJobObject(self.handle, process))
        finally:
            self._k.CloseHandle(process)


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=_NO_WINDOW)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass


def _keep_awake(on: bool) -> bool:
    """Ask Windows not to sleep (on mains power only). Returns whether it is held."""
    if os.name != "nt":
        return False
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    class Power(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
    power = Power()
    mains = bool(kernel32.GetSystemPowerStatus(ctypes.byref(power))) and power.ACLineStatus == 1
    hold = on and mains
    kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if hold else 0))
    return hold


# ------------------------------------------------------------ one process
class Service:
    def __init__(self, name: str, argv: list[str], *, cwd: Path, job: _Job | None = None,
                 log_dir: Path | None = None, first_backoff: float = FIRST_BACKOFF,
                 max_backoff: float = MAX_BACKOFF, stable_after: float = STABLE_AFTER) -> None:
        self.name, self.argv, self.cwd, self.job = name, argv, cwd, job
        self.log_path = Path(log_dir or LOG_DIR) / f"{name}.log"
        self.first_backoff, self.max_backoff = first_backoff, max_backoff
        self.stable_after = stable_after
        self.backoff = first_backoff
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.started_iso: str | None = None
        self.next_start = 0.0
        self.last_exit: int | None = None
        self.last_exit_at: str | None = None
        self.starts: deque[float] = deque()
        self._log_file = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _rotate(self) -> None:
        try:
            if self.log_path.exists() and self.log_path.stat().st_size > LOG_MAX_BYTES:
                for i in (2, 1):
                    older = self.log_path.with_name(f"{self.log_path.name}.{i}")
                    if older.exists():
                        os.replace(older, self.log_path.with_name(f"{self.log_path.name}.{i + 1}"))
                os.replace(self.log_path, self.log_path.with_name(self.log_path.name + ".1"))
        except OSError:
            pass

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate()
        self._log_file = open(self.log_path, "a", encoding="utf-8", buffering=1)
        self._log_file.write(f"\n===== {self.name} starting {_now().isoformat()} =====\n")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        kwargs = {"creationflags": _NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, stdout=self._log_file,
                                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env=env, **kwargs)
        if self.job:
            self.job.add(self.proc.pid)
        self.started_at = time.monotonic()
        self.started_iso = _now().isoformat()
        self.starts.append(time.time())
        log.info("%s started (pid %s)", self.name, self.proc.pid)

    def poll(self) -> None:
        """Start it if it should run and is due; notice if it has exited."""
        now = time.monotonic()
        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            ran = now - (self.started_at or now)
            self.last_exit, self.last_exit_at = code, _now().isoformat()
            self.proc = None
            if self._log_file:
                self._log_file.close()
                self._log_file = None
            if self.name == "poller" and code == ALREADY_RUNNING:
                wait = self.max_backoff
                log.info("poller: another poller holds the database; trying again in %.0f s", wait)
            else:
                if ran >= self.stable_after:
                    self.backoff = self.first_backoff
                wait = self.backoff
                self.backoff = min(self.backoff * 2, self.max_backoff)
                log.warning("%s exited with code %s after %.0f s; restarting in %.0f s",
                            self.name, code, ran, wait)
            self.next_start = now + wait
        if self.proc is None and now >= self.next_start:
            try:
                self.start()
            except OSError as exc:
                log.error("%s could not start: %s", self.name, exc)
                self.next_start = now + self.backoff
                self.backoff = min(self.backoff * 2, self.max_backoff)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            _kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    def restarts_last_hour(self) -> int:
        cutoff = time.time() - 3600
        while self.starts and self.starts[0] < cutoff:
            self.starts.popleft()
        return max(0, len(self.starts) - 1)

    def status(self) -> dict:
        return {"running": self.running, "pid": self.proc.pid if self.running else None,
                "started_at": self.started_iso if self.running else None,
                "restarts_last_hour": self.restarts_last_hour(),
                "last_exit_code": self.last_exit, "last_exit_at": self.last_exit_at,
                "log": str(self.log_path)}


def _answers_http(url: str, timeout: float = 10.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def _accepts(port: int, timeout: float = 3.0) -> bool:
    """Does anything listen on this local port? By name, not 127.0.0.1: Vite
    listens on the IPv6 loopback only, which `localhost` also resolves to."""
    try:
        with socket.create_connection(("localhost", port), timeout=timeout):
            return True
    except OSError:
        return False


# ------------------------------------------------------------ the supervisor
class Supervisor:
    def __init__(self, *, web: bool = True, poller: bool = True, api: bool = True,
                 state_path: Path | None = None, notifier: Notifier | None = None,
                 services: list[Service] | None = None, backups: bool = True) -> None:
        self.state_path = state_path
        self.notifier = notifier or Notifier(state_path=state_path)
        self.job = _Job()
        self.backups = backups
        self._stop = False
        self.want = {"api": api, "poller": poller, "web": web}
        self.services = services if services is not None else self._default_services()
        self.awake = False
        self.started_at: str | None = None

    def _default_services(self) -> list[Service]:
        py = _python()
        out = []
        if self.want["api"]:
            out.append(Service("api", [py, "-m", "uvicorn", "astra.api.main:app", "--host",
                                       "127.0.0.1", "--port", str(API_PORT), "--no-access-log"],
                               cwd=PROJECT_ROOT, job=self.job))
        if self.want["poller"]:
            out.append(Service("poller", [py, "-m", "astra.ingestion.poller"],
                               cwd=PROJECT_ROOT, job=self.job))
        if self.want["web"]:
            node = shutil.which("node")
            vite = PROJECT_ROOT / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
            if node and vite.exists():
                out.append(Service("web", [node, str(vite), "--port", str(WEB_PORT), "--strictPort"],
                                   cwd=PROJECT_ROOT / "frontend", job=self.job))
            else:
                log.error("website not started: %s", "node not found" if not node else
                          "frontend dependencies missing (run npm install in frontend)")
        return out

    def stop(self, *_args) -> None:
        self._stop = True

    def service_health(self) -> dict:
        by_name = {s.name: s for s in self.services}
        out = {}
        for name, expected in self.want.items():
            svc = by_name.get(name)
            detail, started = "", None
            if svc is not None:
                st = svc.status()
                started = st["started_at"]
                detail = (f"Restarted {st['restarts_last_hour']} time(s) in the last hour; last "
                          f"exit code {st['last_exit_code']}; see {st['log']}.")
            if name == "api":
                healthy = _answers_http(f"http://127.0.0.1:{API_PORT}/")
            elif name == "web":
                healthy = _accepts(WEB_PORT)
            else:
                healthy = instance_lock.is_held()
            out[name] = {"expected": expected, "healthy": healthy, "detail": detail,
                         "started_at": started}
        return out

    def tick(self, now: datetime | None = None) -> dict:
        """The once-a-minute work. Returns what it recorded."""
        now = now or _now()
        if self.backups and backup.due():
            log.info("taking the daily backup")
            outcome = backup.take(state_path=self.state_path)
            log.info("backup: %s", json.dumps(outcome))
        services = self.service_health()
        ops = state.load(self.state_path)
        conditions = health.assess(now=now, services=services, ops=ops)
        sent = self.notifier.update(conditions, now=now)
        for message in sent:
            log.info("alert sent: %s — %s", message["title"], message["body"].replace("\n", " / "))
        self.awake = _keep_awake(KEEP_AWAKE) if KEEP_AWAKE or self.awake else False
        record = {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "first_started_at": (ops.get("supervisor") or {}).get("first_started_at")
            or self.started_at,
            "last_tick_at": now.isoformat(),
            "keep_awake": {"requested": KEEP_AWAKE, "held": self.awake},
            "services": {s.name: {**s.status(), **{k: v for k, v in services.get(s.name, {}).items()
                                                  if k in ("healthy",)}}
                         for s in self.services},
            "conditions": [c.as_dict() for c in conditions],
        }
        state.update({"supervisor": record}, self.state_path)
        return record

    def run(self, max_seconds: float | None = None, stop_path: Path | None = STOP_PATH) -> None:
        self.started_at = _now().isoformat()
        log.info("supervisor starting: %s", ", ".join(s.name for s in self.services))
        deadline = time.monotonic() + max_seconds if max_seconds else None
        next_tick = time.monotonic() + 20          # let the processes come up first
        if stop_path is not None:
            stop_path.unlink(missing_ok=True)      # a request left from before
        try:
            while not self._stop and (deadline is None or time.monotonic() < deadline):
                if stop_path is not None and stop_path.exists():
                    log.info("stop requested")
                    break
                for service in self.services:
                    service.poll()
                if time.monotonic() >= next_tick:
                    try:
                        self.tick()
                    except Exception as exc:                # a tick never ends supervision
                        log.exception("tick failed: %s", exc)
                    next_tick = time.monotonic() + TICK_SECONDS
                time.sleep(1.0)
        finally:
            log.info("supervisor stopping")
            for service in reversed(self.services):
                service.stop()
            if self.awake:
                _keep_awake(False)
            if stop_path is not None:
                stop_path.unlink(missing_ok=True)
            state.update({"supervisor": {**(state.load(self.state_path).get("supervisor") or {}),
                                         "pid": None, "stopped_at": _now().isoformat()}},
                         self.state_path)


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "supervisor.log", maxBytes=LOG_MAX_BYTES,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger("astra")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None and sys.stderr.isatty():
        root.addHandler(logging.StreamHandler())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ASTRA unattended")
    parser.add_argument("--no-web", action="store_true", help="do not serve the website")
    parser.add_argument("--no-poller", action="store_true", help="do not run the poller")
    parser.add_argument("--status", action="store_true", help="print what the supervisor reports")
    parser.add_argument("--stop", action="store_true", help="stop the running supervisor")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps({k: state.load().get(k) for k in
                          ("supervisor", "backup", "alerts", "alerts_last_sent")}, indent=2))
        return 0
    if args.stop:
        if not instance_lock.is_held(LOCK_PATH):
            print("no supervisor is running")
            return 0
        STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STOP_PATH.write_text(_now().isoformat(), encoding="utf-8")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and instance_lock.is_held(LOCK_PATH):
            time.sleep(0.5)
        if not instance_lock.is_held(LOCK_PATH):
            print("stopped the supervisor and the processes it started")
            return 0
        pid = (instance_lock.holder(LOCK_PATH) or {}).get("pid")
        if not pid:
            print("the supervisor did not stop and did not record its process id", file=sys.stderr)
            return 1
        _kill_tree(int(pid))
        STOP_PATH.unlink(missing_ok=True)
        print(f"the supervisor did not stop within a minute; ended it (pid {pid}) and its processes")
        return 0

    _configure_logging()
    lock = instance_lock.WriterLock(LOCK_PATH)
    if not lock.acquire(role="supervisor"):
        note = instance_lock.holder(LOCK_PATH) or {}
        message = f"a supervisor is already running (pid {note.get('pid', '?')})"
        log.info(message)
        print(message, file=sys.stderr)
        return ALREADY_RUNNING
    try:
        supervisor = Supervisor(web=not args.no_web, poller=not args.no_poller)
        signal.signal(signal.SIGINT, supervisor.stop)
        signal.signal(signal.SIGTERM, supervisor.stop)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, supervisor.stop)
        supervisor.run()
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
