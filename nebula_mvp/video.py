"""Camera access and JPEG work run in a worker thread, outside asyncio."""
import time
import cv2
import numpy as np

FULL_SIZE = (640, 360)
BASE_SIZE = (160, 90)
# Full-resolution layer rungs below the top (--quality) one, as
# (width, height, JPEG quality). Each step is about 0.75x the bytes of the one
# above on the synthetic scene: 22.8 KB at q50 down to 3.8 KB.
LOWER_RUNGS = ((640, 360, 35), (640, 360, 22), (480, 270, 30), (480, 270, 18),
               (320, 180, 30), (320, 180, 20))
FRAME_SIZES = {FULL_SIZE, BASE_SIZE, *(rung[:2] for rung in LOWER_RUNGS)}
PROMOTE_HEADROOM = 0.8  # Climb a rung only if its fresh encode uses <= 80% of the budget.
PROBE_FRAMES = 30       # Re-measure the rung above after this many frames.


class VideoSource:
    def __init__(self, source="synthetic", camera=0, quality=50, seed=7, base_quality=40):
        self.requested = source
        self.camera_index = camera
        self.quality = quality
        self.base_quality = base_quality
        self.ladder = [(*FULL_SIZE, quality)] + [
            rung for rung in LOWER_RUNGS if rung[:2] != FULL_SIZE or rung[2] < quality]
        self.rung = 0  # Index into ladder; len(ladder) means the full layer is paused.
        self.measured = [None] * len(self.ladder)  # (jpeg bytes, frame id) per rung
        self.cap = None
        self.label = "Synthetic test scene"
        self.warning = ""
        self.texture = np.random.default_rng(seed).integers(0, 256, (360, 640, 3), dtype=np.uint8)
        # Keep single-layer 15 FPS (about 2.8 Mbps) well inside the 5 Mbps baseline.
        self.texture = cv2.GaussianBlur(self.texture, (11, 11), 0)

    def open(self):
        if self.requested in ("camera", "auto"):
            self.cap = cv2.VideoCapture(self.camera_index)
            if self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
                self.label = f"Camera {self.camera_index}"
            else:
                self.warning = "Camera unavailable; using synthetic scene."
                self.close()

    def _frame(self, frame_id):
        stamp = time.perf_counter_ns()
        frame = None
        if self.cap:
            ok, frame = self.cap.read()
            if not ok:
                self.warning = "Camera read failed; using synthetic scene."
                self.close()
                self.label = "Synthetic test scene"
                frame = None
        if frame is None:
            frame = np.roll(self.texture, frame_id * 3 % 640, axis=1).copy()
            for x in range(0, 640, 80):
                cv2.line(frame, (x, 0), (x, 360), (65, 90, 100), 1)
            for y in range(0, 360, 60):
                cv2.line(frame, (0, y), (640, y), (65, 90, 100), 1)
            x = 60 + (frame_id * 6) % 520
            cv2.circle(frame, (x, 180), 32, (40, 220, 190), -1)
            cv2.rectangle(frame, (18, 20), (380, 82), (20, 25, 33), -1)
            cv2.putText(frame, f"AIR / FRAME {frame_id:06d}", (30, 49),
                        cv2.FONT_HERSHEY_SIMPLEX, .65, (230, 240, 240), 1, cv2.LINE_AA)
            cv2.putText(frame, "SYNTHETIC CAMERA - 640 x 360", (30, 70),
                        cv2.FONT_HERSHEY_SIMPLEX, .42, (130, 195, 180), 1, cv2.LINE_AA)
        return stamp, cv2.resize(frame, FULL_SIZE)

    @staticmethod
    def _encode(frame, width, height, quality):
        if (width, height) != FULL_SIZE:
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        return encoded.tobytes()

    def capture(self, frame_id):
        """Single-layer mode: one full-resolution JPEG at the configured quality."""
        stamp, frame = self._frame(frame_id)
        return stamp, self._encode(frame, *self.ladder[0])

    def capture_layers(self, frame_id, budget):
        """Base layer plus the largest full-resolution rung within `budget` JPEG bytes.

        budget=None means the link reports no budget: send the top rung. Returns
        (stamp, base_jpeg, full_jpeg or None when the full layer is paused).
        """
        stamp, frame = self._frame(frame_id)
        base = self._encode(frame, *BASE_SIZE, self.base_quality)
        return stamp, base, self._full_within(frame_id, frame, budget)

    def _full_within(self, frame_id, frame, budget):
        # Moving down is immediate. Moving up is one rung per frame and must be
        # confirmed by a fresh encode with headroom, so sizes near the budget
        # cannot flap between rungs.
        if budget is None:
            self.rung = 0
            return self._encode(frame, *self.ladder[0])
        above = self.rung - 1
        if above >= 0:
            last = self.measured[above]
            if last is None or frame_id - last[1] > PROBE_FRAMES or last[0] <= budget * PROMOTE_HEADROOM:
                jpeg = self._measure(frame_id, frame, above)
                if len(jpeg) <= budget * PROMOTE_HEADROOM:
                    self.rung = above
                    return jpeg
        for index in range(self.rung, len(self.ladder)):
            jpeg = self._measure(frame_id, frame, index)
            if len(jpeg) <= budget:
                self.rung = index
                return jpeg
        self.rung = len(self.ladder)
        return None

    def _measure(self, frame_id, frame, index):
        jpeg = self._encode(frame, *self.ladder[index])
        self.measured[index] = (len(jpeg), frame_id)
        return jpeg

    def close(self):
        if self.cap:
            self.cap.release()
            self.cap = None


def valid_jpeg(jpeg):
    """Decoded (width, height) if it is a size the sender produces, else None."""
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    size = (image.shape[1], image.shape[0])
    return size if size in FRAME_SIZES else None
