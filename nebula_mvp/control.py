from dataclasses import asdict, dataclass
import math


@dataclass
class Control:
    throttle: float = 0.5
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0

    def public(self):
        return asdict(self)

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {"throttle", "yaw", "pitch", "roll"}:
            raise ValueError("Expected four control axes")
        for key, number in value.items():
            low = 0 if key == "throttle" else -1
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ValueError("Control must be numeric")
            if not math.isfinite(number) or not low <= number <= 1:
                raise ValueError("Control out of range")
        return cls(**value)
