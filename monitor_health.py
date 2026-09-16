"""Small state shared between the capture worker and the GUI watchdog."""

from __future__ import annotations

from dataclasses import dataclass


MAX_CAPTURE_FAILURES = 3


@dataclass
class MonitorHealth:
    last_progress: float
    capture_failures: int = 0
    storage_warning: bool = False
    unknown_warned: bool = False
    watchdog_warned: bool = False
    watchdog_notified: bool = False
    worker_death_notified: bool = False

    def warning(self) -> bool:
        return bool(self.capture_failures or self.storage_warning or self.unknown_warned
                    or self.watchdog_warned
                    or self.worker_death_notified)


def watchdog_timeout(interval: float) -> float:
    return max(8.0, interval * 3 + 5.0)
