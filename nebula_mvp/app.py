import asyncio
import ctypes
import json
from pathlib import Path
import socket
import sys
import time
from .air_node import AirNode
from .config import LinkConfig
from .ground_node import GroundNode
from .link_emulator import LinkEmulator
from .metrics import Metrics
from .protocol import Kind
from .server import start_server
from .video import BASE_SIZE, VideoSource
from .clock import WindowsTimerWakeup


class Application:
    def __init__(self, config=None, source="synthetic", camera=0, fps=15, quality=50,
                 output_dir=None, seed=7, layered=True, base_quality=40, fec=True):
        self.config = config or LinkConfig()
        self.output_dir = Path(output_dir) if output_dir else None
        self.metrics = Metrics(self.output_dir)
        self.link = LinkEmulator(self.config, self.metrics, seed)
        self.video_source = VideoSource(source, camera, quality, seed, base_quality)
        self.air = AirNode(self.link, self.metrics, self.video_source, fps, layered, fec)
        self.ground = GroundNode(self.link, self.metrics)
        self.server = None
        self.running = False
        self.tasks = []
        self.high_resolution_timer = False
        self.timer_wakeup = None
        self.timeline = None
        self.summary = None
        self.last_state = None

    async def start(self, port=None):
        if sys.platform == "win32":
            self.high_resolution_timer = ctypes.windll.winmm.timeBeginPeriod(1) == 0
        await asyncio.to_thread(self.video_source.open)
        loop = asyncio.get_running_loop()
        if sys.platform == "win32":
            self.timer_wakeup = WindowsTimerWakeup(loop)
        for node in (self.link, self.air, self.ground):
            transport, _ = await loop.create_datagram_endpoint(lambda node=node: node,
                                                               local_addr=("127.0.0.1", 0))
            # Local transport should not be the experimental bottleneck.
            transport.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        self.air.link_address = self.ground.link_address = self.link.transport.get_extra_info("sockname")
        self.link.air_address = self.air.transport.get_extra_info("sockname")
        self.link.ground_address = self.ground.transport.get_extra_info("sockname")
        self.running = True
        self.link.task = asyncio.create_task(self.link.run(), name="link")
        self.tasks = [asyncio.create_task(self.air.video_loop(), name="video"),
                      asyncio.create_task(self.air.telemetry_loop(), name="telemetry"),
                      asyncio.create_task(self.ground.control_loop(), name="control"),
                      asyncio.create_task(self.ground.decode_loop(), name="decode")]
        if self.output_dir:
            self.timeline = (self.output_dir / "timeline.jsonl").open("w", encoding="utf-8")
        self.tasks.append(asyncio.create_task(self.housekeeping(), name="housekeeping"))
        if port is not None:
            self.server = start_server(self, port)
        return self

    async def state(self):
        now = time.perf_counter_ns()
        return {"config": self.link.config.public(), "metrics": self.metrics.snapshot(),
                "queues": self.link.scheduler.snapshot(), "telemetry": self.ground.telemetry,
                "desired_control": self.ground.desired.public(),
                "source": self.air.source.label, "source_warning": self.air.source.warning,
                "source_requested": self.air.source.requested,
                "video": self.video_state(),
                "frame_id": self.ground.frame_id,
                "frame_age_ms": (now - self.ground.frame_generated_ns) / 1e6 if self.ground.frame_generated_ns else None,
                "telemetry_age_ms": (now - self.ground.telemetry_generated_ns) / 1e6 if self.ground.telemetry_generated_ns else None,
                "output_dir": str(self.output_dir.resolve()) if self.output_dir else None,
                "udp": {"link": self.air.link_address, "air": self.link.air_address,
                        "ground": self.link.ground_address}}

    def video_state(self):
        source, layered = self.air.source, self.air.layered
        if not layered:
            rung = source.ladder[0]  # Single-layer mode always sends the top rung.
        elif source.rung < len(source.ladder):
            rung = source.ladder[source.rung]
        else:
            rung = None  # Full-resolution layer paused.
        return {"layered": layered, "full_rung": list(rung) if rung else None,
                "base_rung": [*BASE_SIZE, source.base_quality] if layered else None,
                "frame_layer": {Kind.VIDEO: "full", Kind.VIDEO_BASE: "base"}.get(self.ground.frame_kind),
                "frame_size": list(self.ground.frame_size) if self.ground.frame_size else None,
                "fec": self.air.fec, "fec_parity": self.air.last_parity,
                "loss_estimate": self.link.loss_estimate(),
                "full_wait_ms": round(self.ground.selector.wait_ns() / 1e6) if layered else None}

    async def command(self, path, value):
        if path == "/api/link":
            self.link.update(value)
        elif path == "/api/control":
            self.ground.set_control(value)
        elif path == "/api/video":
            if (not isinstance(value, dict) or not value or set(value) - {"layered", "fec"}
                    or not all(isinstance(v, bool) for v in value.values())):
                raise ValueError("expected layered and/or fec as true or false")
            self.air.layered = value.get("layered", self.air.layered)
            self.air.fec = value.get("fec", self.air.fec)
        elif path == "/api/source":
            if not isinstance(value, dict) or set(value) != {"source"} or value["source"] not in ("synthetic", "camera"):
                raise ValueError("source must be synthetic or camera")
            async with self.air.video_lock:
                old = self.air.source
                await asyncio.to_thread(old.close)
                self.air.source = VideoSource(value["source"], old.camera_index, old.quality,
                                              base_quality=old.base_quality)
                await asyncio.to_thread(self.air.source.open)
        return {"ok": True}

    async def housekeeping(self):
        while True:
            self.link.housekeeping()
            for assembler in self.ground.assemblers.values():
                assembler.expire(time.perf_counter_ns())
            self.metrics.housekeeping()
            self.last_state = await self.state()
            if self.timeline:
                self.timeline.write(json.dumps(self.last_state, allow_nan=False) + "\n")
                self.timeline.flush()
            await asyncio.sleep(1)

    def check_tasks(self):
        for task in [*self.tasks, self.link.task]:
            if task and task.done() and not task.cancelled():
                error = task.exception()
                if error:
                    raise RuntimeError(f"Background task {task.get_name()} failed") from error

    async def close(self):
        self.running = False
        if self.server:
            await asyncio.to_thread(self.server.shutdown)
            self.server.server_close()
        self.summary = await self.state()
        if self.output_dir:
            (self.output_dir / "summary.json").write_text(json.dumps(self.summary, indent=2, allow_nan=False), encoding="utf-8")
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.link.close()
        for node in (self.air, self.ground):
            if node.transport:
                node.transport.abort()
        await asyncio.sleep(0)
        await asyncio.to_thread(self.air.source.close)
        if self.timeline:
            self.timeline.close()
        self.metrics.close()
        if self.timer_wakeup:
            self.timer_wakeup.close()
        if self.high_resolution_timer:
            ctypes.windll.winmm.timeEndPeriod(1)
