"""Simulation time (guide step 2.2).

The clock only says where simulated time should be; the session decides what
that time reveals. Every frame (50 ms of real time) the clock moves forward
by the real time that actually elapsed multiplied by `speed`. At speed 60 one
real second plays one market minute.
"""

from __future__ import annotations

from dataclasses import dataclass

FRAME_SECONDS = 0.05

#: "max" plays one market day in about three real seconds.
MAX_SPEED = 28_800.0

#: Selector label -> market seconds per real second.
SPEEDS: dict[str, float] = {
    "1x": 1.0,
    "10x": 10.0,
    "60x": 60.0,
    "300x": 300.0,
    "1800x": 1800.0,
    "max": MAX_SPEED,
}

#: A stalled event loop must not turn into a jump of hours on resume.
MAX_FRAME_GAP_SECONDS = 0.25


@dataclass
class Clock:
    speed: float = 60.0
    playing: bool = False

    def set_speed(self, label_or_value: str | float) -> float:
        if isinstance(label_or_value, str):
            if label_or_value not in SPEEDS:
                raise ValueError(f"Unknown speed {label_or_value!r}")
            value = SPEEDS[label_or_value]
        else:
            value = float(label_or_value)
        if not (0 < value <= MAX_SPEED):
            raise ValueError("Speed must be positive and at most max")
        self.speed = value
        return value

    def market_ns(self, real_seconds: float) -> int:
        """Simulated nanoseconds that `real_seconds` of wall time is worth."""
        real_seconds = min(max(real_seconds, 0.0), MAX_FRAME_GAP_SECONDS)
        return int(real_seconds * self.speed * 1e9)

    def speed_label(self) -> str:
        for label, value in SPEEDS.items():
            if value == self.speed:
                return label
        return f"{self.speed:g}x"
