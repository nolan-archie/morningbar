from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time as time_module
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .models import Settings
from .status import StatusWriter


@dataclass(frozen=True)
class NetworkState:
    connected: bool
    metered: bool
    connectivity: str
    active_devices: tuple[str, ...] = ()


class TriggerLock:
    """Non-blocking process lock held for the entire scheduled run."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> "TriggerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.handle = self.path.open("a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class TriggerState:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def last_started(self) -> datetime | None:
        value = self.read().get("last_started_at")
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None

    def completed_today(self, local_day: str) -> bool:
        return self.read().get("completed_local_day") == local_day

    def mark_started(self, when: datetime) -> None:
        values = self.read()
        values["last_started_at"] = when.astimezone(UTC).isoformat()
        self.write(values)

    def last_outside_event(self, local_day: str) -> datetime | None:
        values = self.read()
        if values.get("last_outside_local_day") != local_day:
            return None
        value = values.get("last_outside_event_at")
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None

    def mark_outside_event(self, local_day: str, when: datetime) -> None:
        values = self.read()
        values["last_outside_local_day"] = local_day
        values["last_outside_event_at"] = when.astimezone(UTC).isoformat()
        self.write(values)

    def mark_completed(self, local_day: str, when: datetime) -> None:
        values = self.read()
        values["completed_local_day"] = local_day
        values["completed_at"] = when.astimezone(UTC).isoformat()
        self.write(values)

    def write(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(values, indent=2) + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)


def is_in_morning_window(
    now: datetime,
    timezone: str,
    start: time,
    end: time,
    late_grace_minutes: int = 0,
) -> bool:
    local = now.astimezone(ZoneInfo(timezone))
    current = local.timetz().replace(tzinfo=None)
    current_datetime = datetime.combine(local.date(), current)
    start_datetime = datetime.combine(local.date(), start)
    end_datetime = datetime.combine(local.date(), end) + timedelta(minutes=late_grace_minutes)
    return start_datetime <= current_datetime < end_datetime


def _run_nmcli(arguments: list[str]) -> str:
    result = subprocess.run(
        ["/usr/bin/nmcli", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def probe_network() -> NetworkState:
    try:
        connectivity = _run_nmcli(["-g", "CONNECTIVITY", "general"]).strip().lower()
        rows = _run_nmcli(["-t", "-f", "TYPE,STATE,DEVICE", "device", "status"]).splitlines()
    except (OSError, subprocess.SubprocessError):
        return NetworkState(False, False, "networkmanager-unavailable")

    active: list[tuple[str, str]] = []
    for row in rows:
        fields = row.split(":", 2)
        if len(fields) == 3 and fields[1].strip().lower() in {"connected", "connected (externally)"}:
            device_type, _, device = fields
            if device_type.strip().lower() in {"wifi", "ethernet"}:
                active.append((device_type.strip().lower(), device.strip()))

    if connectivity in {"full", "unknown"}:
        connected = bool(active) or connectivity == "full"
    else:
        connected = False

    metered = False
    relevant = [item for item in active if item[0] == "wifi"] or active
    for _, device in relevant:
        try:
            value = _run_nmcli(["-g", "GENERAL.METERED", "device", "show", device]).lower()
        except (OSError, subprocess.SubprocessError):
            metered = True
            break
        if value.startswith("yes") or value.startswith("unknown") or not value:
            metered = True
            break
    return NetworkState(connected, metered, connectivity, tuple(d for _, d in active))


def wait_for_network(
    settings: Settings,
    status: StatusWriter,
    *,
    probe: Callable[[], NetworkState] = probe_network,
    sleep: Callable[[float], None] = time_module.sleep,
    clock: Callable[[], float] = time_module.monotonic,
) -> NetworkState:
    deadline = clock() + settings.network_wait_seconds
    delay = 2.0
    while True:
        state = probe()
        if state.connected:
            return state
        remaining = deadline - clock()
        if remaining <= 0:
            return state
        wait = min(delay, remaining)
        status.update(
            "waiting-network", 0, 0,
            connectivity=state.connectivity,
            retry_in_seconds=round(wait, 1),
        )
        sleep(wait)
        delay = min(30.0, delay * 2)
