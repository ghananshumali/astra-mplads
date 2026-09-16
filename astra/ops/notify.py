"""Telling a person: when a condition has lasted, and when it has cleared.

Rules
-----
* A condition is announced once it has lasted longer than its grace period.
* While it lasts, it is repeated every `REMIND_HOURS`, not every minute.
* When an announced condition clears, that is announced too, so an alert
  never stays "open" in someone's head after the problem went away.
* Conditions starting or clearing in the same minute go out as one message.

Channels
--------
``log``    always: one line per message in `data/logs/alerts.log`.
``toast``  a Windows notification, through PowerShell's built-in WinRT API
           (nothing to install). The default on Windows.
``ntfy``   a push to a phone through ntfy (https://ntfy.sh, open source, no
           account). Off unless ASTRA_ALERT_NTFY_TOPIC is set, because it
           sends the message text to that server: choose a topic name nobody
           will guess, or point ASTRA_ALERT_NTFY_SERVER at your own.

Choose with ASTRA_ALERT_CHANNELS, e.g. "log,toast,ntfy". A channel that fails
is logged and never stops the others or the supervisor.
"""
from __future__ import annotations

import os
import subprocess
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..config import DATA_DIR
from . import state
from .health import Condition

LOG_DIR = Path(os.environ.get("ASTRA_LOG_DIR") or (DATA_DIR / "logs"))
REMIND_HOURS = float(os.environ.get("ASTRA_ALERT_REMIND_HOURS", "12"))
NTFY_TOPIC = os.environ.get("ASTRA_ALERT_NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("ASTRA_ALERT_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
_DEFAULT = "log,toast" if os.name == "nt" else "log"
CHANNELS = tuple(c.strip() for c in os.environ.get("ASTRA_ALERT_CHANNELS", _DEFAULT).split(",")
                 if c.strip())

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: PowerShell's own app id, which Windows accepts for toasts without registration.
_TOAST_APP = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_TOAST_SCRIPT = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$esc = { param($s) [System.Security.SecurityElement]::Escape($s) }
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>" + (& $esc $env:ASTRA_TOAST_TITLE) + "</text><text>" + (& $esc $env:ASTRA_TOAST_BODY) + "</text></binding></visual></toast>")
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:ASTRA_TOAST_APP).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""


def _log_line(title: str, body: str, now: datetime) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_DIR / "alerts.log", "a", encoding="utf-8") as fh:
        fh.write(f"{now.isoformat()}  {title} | {body.replace(chr(10), ' / ')}\n")


def _toast(title: str, body: str, now: datetime) -> None:
    env = {**os.environ, "ASTRA_TOAST_TITLE": title, "ASTRA_TOAST_BODY": body[:400],
           "ASTRA_TOAST_APP": _TOAST_APP}
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_SCRIPT],
        env=env, capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "toast failed").strip()[:200])


def _ntfy(title: str, body: str, now: datetime) -> None:
    if not NTFY_TOPIC:
        raise RuntimeError("ASTRA_ALERT_NTFY_TOPIC is not set")
    request = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode("utf-8"), method="POST",
        headers={"Title": title.encode("ascii", "replace").decode(), "Tags": "warning"})
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


SENDERS: dict[str, Callable[[str, str, datetime], None]] = {
    "log": _log_line, "toast": _toast, "ntfy": _ntfy,
}


def send(title: str, body: str, *, channels=None, now: datetime | None = None) -> dict:
    """Send one message on every channel; returns {channel: "sent" | error}."""
    now = now or datetime.now(timezone.utc)
    out = {}
    for name in channels or CHANNELS:
        sender = SENDERS.get(name)
        if sender is None:
            out[name] = "unknown channel"
            continue
        try:
            sender(title, body, now)
            out[name] = "sent"
        except Exception as exc:                      # a channel never stops the rest
            out[name] = f"{type(exc).__name__}: {str(exc)[:160]}"
    failed = {k: v for k, v in out.items() if v != "sent" and k != "log"}
    if failed:
        try:
            _log_line("alert channel failed", str(failed), now)
        except OSError:
            pass
    return out


class Notifier:
    def __init__(self, *, channels=None, state_path: Path | None = None,
                 remind: timedelta | None = None, sender=send) -> None:
        self.channels = channels
        self.state_path = state_path
        self.remind = remind or timedelta(hours=REMIND_HOURS)
        self.sender = sender

    def update(self, conditions: list[Condition], now: datetime | None = None) -> list[dict]:
        """Record what holds now and send what is due. Returns the messages sent."""
        now = now or datetime.now(timezone.utc)
        tracked: dict = state.load(self.state_path).get("alerts") or {}
        current = {c.key: c for c in conditions}
        starting, reminding, cleared = [], [], []

        for key, cond in current.items():
            entry = tracked.get(key) or {"first_seen": now.isoformat(), "notified_at": None}
            entry.update(title=cond.title, detail=cond.detail, severity=cond.severity)
            first = datetime.fromisoformat(entry["first_seen"])
            notified = datetime.fromisoformat(entry["notified_at"]) if entry["notified_at"] else None
            if notified is None and now - first >= timedelta(minutes=cond.grace_minutes):
                starting.append(cond)
                entry["notified_at"] = now.isoformat()
            elif notified is not None and now - notified >= self.remind:
                reminding.append(cond)
                entry["notified_at"] = now.isoformat()
            tracked[key] = entry
        for key in [k for k in tracked if k not in current]:
            entry = tracked.pop(key)
            if entry.get("notified_at"):
                cleared.append(entry)

        sent = []
        problems = starting + reminding
        if problems:
            critical = any(c.severity == "critical" for c in problems)
            title = ("ASTRA needs attention" if critical else "ASTRA: something to check")
            if reminding and not starting:
                title += " (still)"
            body = "\n".join(f"{c.title}. {c.detail}".strip() for c in problems)
            sent.append({"kind": "problem", "title": title, "body": body,
                         "result": self.sender(title, body, channels=self.channels, now=now)})
        if cleared:
            title = "ASTRA recovered"
            body = "\n".join(f"Resolved: {e['title']}." for e in cleared)
            sent.append({"kind": "recovered", "title": title, "body": body,
                         "result": self.sender(title, body, channels=self.channels, now=now)})
        state.update({"alerts": tracked,
                      "alerts_checked_at": now.isoformat(),
                      **({"alerts_last_sent": sent[-1] | {"at": now.isoformat()}} if sent else {})},
                     self.state_path)
        return sent
