"""Small stdlib-only logind watcher for the user's graphical session."""

from __future__ import annotations

import signal
import subprocess
import sys


def main() -> int:
    try:
        monitor = subprocess.Popen(
            [
                "/usr/bin/dbus-monitor",
                "--system",
                "type='signal',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        print(f"Morning Bar sleep watcher could not start dbus-monitor: {exc}", flush=True)
        return 1

    def stop_child(_signum: int, _frame: object) -> None:
        if monitor.poll() is None:
            monitor.terminate()

    signal.signal(signal.SIGTERM, stop_child)
    signal.signal(signal.SIGINT, stop_child)
    saw_sleep_signal = False
    assert monitor.stdout is not None
    try:
        for line in monitor.stdout:
            if "member=PrepareForSleep" in line:
                saw_sleep_signal = True
            elif saw_sleep_signal and "boolean false" in line:
                saw_sleep_signal = False
                result = subprocess.run(
                    ["/usr/bin/systemctl", "--user", "start", "--no-block", "morningbar-trigger.service"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                print(f"Morning Bar resume trigger requested (systemctl={result.returncode}).", flush=True)
            elif saw_sleep_signal and "boolean true" in line:
                saw_sleep_signal = False
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Morning Bar sleep watcher failed: {type(exc).__name__}", flush=True)
        return 1
    finally:
        if monitor.poll() is None:
            monitor.terminate()
            try:
                monitor.wait(timeout=2)
            except subprocess.TimeoutExpired:
                monitor.kill()
    return monitor.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
