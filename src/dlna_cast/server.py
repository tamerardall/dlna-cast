"""HTTP server that fans the MPEG-TS stream out to the TV."""
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dlna_cast.log import log


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # no content length, streams until the connection closes

    def _headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mpeg")
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org",
                         "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def do_HEAD(self):
        self._headers()

    def do_GET(self):
        log("Client connected:", self.client_address[0])
        self._headers()
        q = queue.Queue(maxsize=2400)  # ~760 chunks/s at 8 Mbit/s -> ~3 s buffer
        self.server.add_client(q)
        try:
            while True:
                self.wfile.write(q.get())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.server.remove_client(q)
            log("Client disconnected.")

    def log_message(self, *_):
        pass


class StreamServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port):
        super().__init__(("0.0.0.0", port), _Handler)
        self.clients = []
        self.lock = threading.Lock()
        self.dropped = 0  # TS chunks dropped because the TV fell behind (causes artifacts)

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def add_client(self, q):
        with self.lock:
            self.clients.append(q)

    def remove_client(self, q):
        with self.lock:
            self.clients.remove(q)

    def broadcast(self, data):
        with self.lock:
            for q in self.clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    self.dropped += 1
