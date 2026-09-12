"""Local-only dashboard. HTTP is observation/configuration, not the air link."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

STATIC = Path(__file__).with_name("static")


def start_server(app, port):
    loop = asyncio.get_running_loop()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def call(self, coroutine):
            future = asyncio.run_coroutine_threadsafe(coroutine, loop)
            try:
                return future.result(timeout=10)
            except TimeoutError:
                future.cancel()
                raise

        def reply(self, status, data, content_type="application/json; charset=utf-8"):
            if isinstance(data, dict):
                data = json.dumps(data, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{server.server_port}",
                                                 f"localhost:{server.server_port}")

        def do_GET(self):
            try:
                if not self.valid_host():
                    self.reply(403, {"error": "Local access only"})
                    return
                path = urlsplit(self.path).path
                if path == "/api/state":
                    self.reply(200, self.call(app.state()))
                elif path == "/frame.jpg":
                    frame = app.ground.jpeg
                    self.reply(200 if frame else 204, frame or b"", "image/jpeg")
                elif path == "/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    last = None
                    last_send = 0
                    while app.running:
                        frame = app.ground.jpeg
                        if frame and (frame is not last or time.monotonic() - last_send > 1):
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                                             str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                            self.wfile.flush()
                            last = frame
                            last_send = time.monotonic()
                        time.sleep(.03)
                else:
                    files = {"/": ("index.html", "text/html; charset=utf-8"),
                             "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                             "/style.css": ("style.css", "text/css; charset=utf-8")}
                    if path not in files:
                        self.reply(404, {"error": "Not found"})
                        return
                    name, mime = files[path]
                    self.reply(200, (STATIC / name).read_bytes(), mime)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            except TimeoutError:
                self.reply(503, {"error": "Application busy"})

        def do_POST(self):
            try:
                expected_origins = (f"http://127.0.0.1:{server.server_port}",
                                    f"http://localhost:{server.server_port}")
                if not self.valid_host() or self.headers.get("Origin", expected_origins[0]) not in expected_origins:
                    self.reply(403, {"error": "Local access only"})
                    return
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    self.reply(415, {"error": "Expected application/json"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8192:
                    raise ValueError("Invalid request size")
                value = json.loads(self.rfile.read(length))
                path = urlsplit(self.path).path
                if path not in ("/api/link", "/api/control", "/api/video", "/api/source"):
                    self.reply(404, {"error": "Not found"})
                    return
                self.reply(200, self.call(app.command(path, value)))
            except (ValueError, TypeError, UnicodeError) as exc:
                self.reply(400, {"error": str(exc)})
            except TimeoutError:
                self.reply(503, {"error": "Application busy"})
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dashboard-http")
    thread.start()
    return server
