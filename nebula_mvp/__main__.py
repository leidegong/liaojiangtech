import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import time
import webbrowser
from .app import Application
from .config import LinkConfig
from .clock import PreciseEventLoop


async def run(args):
    config = LinkConfig().updated({"mode": args.mode, "capacity_bps": int(args.mbps * 1e6),
                                   "delay_ms": args.delay, "jitter_ms": args.jitter, "loss": args.loss / 100,
                                   "loss_burst": args.burst})
    output = args.output or str(Path("artifacts") / datetime.now().strftime("run-%Y%m%d-%H%M%S"))
    app = Application(config, source=args.source, camera=args.camera, fps=args.fps,
                      quality=args.quality, output_dir=output, layered=args.layered,
                      base_quality=args.base_quality, fec=args.fec)
    try:
        await app.start(None if args.headless else args.port)
        print(f"UDP link: {app.air.link_address}; mode={config.mode}; capacity={args.mbps} Mbps; "
              f"video={'layered' if args.layered else 'single-layer'}", flush=True)
        print(f"Logs: {Path(output).resolve()}", flush=True)
        if app.server:
            url = f"http://127.0.0.1:{app.server.server_port}"
            print(f"Dashboard: {url}", flush=True)
            if args.open:
                webbrowser.open(url)
        start = time.perf_counter()
        while not args.duration or time.perf_counter() - start < args.duration:
            await asyncio.sleep(.25)
            app.check_tasks()
    finally:
        await app.close()


def main():
    parser = argparse.ArgumentParser(description="Video + telemetry + control over one emulated UDP link")
    parser.add_argument("--mode", choices=["naive", "fusion"], default="fusion")
    parser.add_argument("--source", choices=["synthetic", "camera", "auto"], default="synthetic")
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--fps", type=int, choices=range(1, 31), default=15)
    parser.add_argument("--quality", type=int, choices=range(10, 96), default=50,
                        help="JPEG quality of the top full-resolution rung")
    parser.add_argument("--layered", action=argparse.BooleanOptionalAction, default=True,
                        help="160x90 base layer plus an adaptive full-resolution layer "
                             "(--no-layered restores the fixed single layer)")
    parser.add_argument("--base-quality", type=int, choices=range(10, 96), default=40)
    parser.add_argument("--fec", action=argparse.BooleanOptionalAction, default=True,
                        help="Reed-Solomon parity on the full-resolution layer, sized to the "
                             "measured packet loss (layered fusion mode only)")
    parser.add_argument("--mbps", type=float, default=1.5)
    parser.add_argument("--delay", type=float, default=20)
    parser.add_argument("--jitter", type=float, default=5)
    parser.add_argument("--loss", type=float, default=0, help="Packet loss percent, 0..25")
    parser.add_argument("--burst", type=float, default=1,
                        help="Mean run of consecutive lost packets, 1..50 (1 = independent loss)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--duration", type=float, default=0, help="Seconds; 0 runs until Ctrl+C")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--open", action="store_true", help="Open dashboard in default browser")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.duration < 0:
        parser.error("duration must be nonnegative")
    try:
        with asyncio.Runner(loop_factory=PreciseEventLoop) as runner:
            runner.run(run(args))
    except KeyboardInterrupt:
        print("Stopped. Metrics saved.")


if __name__ == "__main__":
    main()
