"""Recognize the first test and subsequent green-to-purple cycles."""

from dataclasses import dataclass


GREEN = "testing"
PURPLE = "credence"
UNKNOWN = "unknown"


def classify_status(bgra: bytes, width: int, height: int, region: tuple[int, int, int, int]) -> tuple[str, float]:
    x1, y1, x2, y2 = region
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError("状态框超出 mstsc 窗口范围")
    green = purple = total = 0
    for row in range(9):
        y = y1 + (y2 - y1) * (2 * row + 1) // 18
        for col in range(17):
            x = x1 + (x2 - x1) * (2 * col + 1) // 34
            offset = (y * width + x) * 4
            b, g, r = bgra[offset:offset + 3]
            total += 1
            if g >= 110 and g >= r + 65 and g >= b + 65:
                green += 1
            elif r >= 65 and b >= 65 and r >= g + 45 and b >= g + 45:
                purple += 1
    if green / total >= 0.55:
        return GREEN, green / total
    if purple / total >= 0.55:
        return PURPLE, purple / total
    return UNKNOWN, max(green, purple) / total


@dataclass
class CompletionLatch:
    green_needed: int = 1
    purple_needed: int = 3
    phase: str = "initial"
    green_count: int = 0
    purple_count: int = 0

    def observe(self, status: str) -> bool:
        if self.phase == "initial":
            if status == GREEN:
                # Starting green: the first purple observation is enough.
                self.phase = "wait_first_purple"
            elif status == PURPLE:
                # Starting purple: wait for a new green test, then three purple samples.
                self.phase = "wait_green"
            return False

        if self.phase == "wait_green":
            # Starting purple and an already alerted test both require new green.
            self.green_count = self.green_count + 1 if status == GREEN else 0
            if self.green_count >= self.green_needed:
                self.phase = "wait_purple"
                self.green_count = 0
            self.purple_count = 0
            return False

        if self.phase == "wait_first_purple":
            if status == PURPLE:
                self.phase = "wait_green"
                return True
            return False

        if status == PURPLE:
            self.purple_count += 1
            if self.purple_count >= self.purple_needed:
                self.phase = "wait_green"
                self.purple_count = 0
                return True
        else:
            self.purple_count = 0
        return False


@dataclass
class UnknownFrameWatch:
    threshold_seconds: float = 30.0
    first_unknown: float | None = None
    warned: bool = False

    def observe(self, status: str, now: float) -> bool:
        if status != UNKNOWN:
            self.first_unknown = None
            self.warned = False
            return False
        if self.first_unknown is None:
            self.first_unknown = now
        if not self.warned and now - self.first_unknown >= self.threshold_seconds:
            self.warned = True
            return True
        return False
