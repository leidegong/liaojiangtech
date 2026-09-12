"""High-resolution asyncio clock used on all platforms.

PreciseEventLoop always binds loop.time() to perf_counter. On Windows the
Application also requests a 1 ms multimedia timer and starts WindowsTimerWakeup;
on Linux/macOS the selector loop alone is enough for the emulator's pacing.
"""
import asyncio
import sys
import threading
import time


BaseLoop = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.SelectorEventLoop


class PreciseEventLoop(BaseLoop):
    def __init__(self):
        super().__init__()
        # CPython uses this internal field to coalesce timers. Windows' default
        # GetTickCount64 resolution (15.6 ms) otherwise permits timers to run
        # early even with timeBeginPeriod(1). Match it to our actual time source.
        self._clock_resolution = time.get_clock_info("perf_counter").resolution

    def time(self):
        return time.perf_counter()


class WindowsTimerWakeup:
    """Keep background Windows IOCP timers from rounding every packet to 16 ms.

    Used only on win32 (see Application.start). Python 3.11+ time.sleep uses a
    high-resolution waitable timer. The worker only wakes the asyncio loop; it
    never accesses queues or generates packets.
    """
    def __init__(self, loop):
        self.loop = loop
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="link-timer")
        self.thread.start()

    @staticmethod
    def noop():
        pass

    def run(self):
        while not self.stopped.is_set():
            time.sleep(.001)
            try:
                self.loop.call_soon_threadsafe(self.noop)
            except RuntimeError:
                return

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=1)
