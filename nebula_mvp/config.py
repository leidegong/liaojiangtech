from dataclasses import asdict, dataclass
import math


@dataclass
class LinkConfig:
    mode: str = "fusion"
    capacity_bps: int = 1_500_000
    delay_ms: float = 20.0
    jitter_ms: float = 5.0
    loss: float = 0.0
    loss_burst: float = 1.0  # Mean run of consecutive lost packets; 1 keeps losses independent.
    data_ttl_ms: float = 500.0
    control_ttl_ms: float = 100.0
    video_wait_ttl_ms: float = 350.0
    video_active_ttl_ms: float = 1500.0
    data_queue_limit: int = 100
    video_pending_limit: int = 2
    fifo_byte_limit: int = 4 * 1024 * 1024

    def public(self):
        return asdict(self)

    def updated(self, values):
        allowed = {"mode", "capacity_bps", "delay_ms", "jitter_ms", "loss", "loss_burst"}
        if not isinstance(values, dict) or set(values) - allowed:
            raise ValueError("Unsupported link parameter")
        result = self.public()
        result.update(values)
        if result["mode"] not in ("naive", "fusion"):
            raise ValueError("mode must be naive or fusion")
        for key, low, high in (("capacity_bps", 100_000, 10_000_000),
                               ("delay_ms", 0, 500), ("jitter_ms", 0, 200),
                               ("loss", 0, 0.25), ("loss_burst", 1, 50)):
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} must be numeric")
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be in [{low}, {high}]")
        result["capacity_bps"] = int(result["capacity_bps"])
        return LinkConfig(**result)
