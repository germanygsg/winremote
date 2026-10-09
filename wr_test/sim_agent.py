"""Repro of WinRemote's _restart_into_new: ThreadingHTTPServer(daemon_threads=True)
+ Timer thread -> server.shutdown() -> sleep(1) -> os.execv(pythonw, marker).
Evidence goes to files because pythonw.exe has no console."""
import os
import sys
import threading
import time
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer

TAG = sys.argv[1] if len(sys.argv) > 1 else "sim1"
BASE = r"C:\temp\wr_test"
LOG = os.path.join(BASE, TAG + ".log")
DO_SHUTDOWN = os.environ.get("WR_DO_SHUTDOWN", "1") == "1"


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{time.time():.1f} {msg}\n")


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


class S(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


_server = None


def _restart_into_new():
    log("restart_fn entered, do_shutdown=%s" % DO_SHUTDOWN)
    if _server is not None and DO_SHUTDOWN:
        try:
            _server.shutdown()
            log("shutdown() ok")
        except Exception as e:
            log("shutdown() raised: %r" % e)
    time.sleep(1)
    log("calling os.execv")
    try:
        os.execv(sys.executable,
                 [sys.executable, os.path.join(BASE, "marker_%s.py" % TAG)])
    except Exception as e:
        log("execv raised: %r" % e)
    log("execv RETURNED (should be impossible)")


def main():
    global _server
    port = 48791 if TAG == "sim1" else 48792
    server = S(("127.0.0.1", port), H)
    _server = server
    threading.Timer(3.0, _restart_into_new).start()
    log("serving on %d" % port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    log("serve_forever returned; main() exiting")


if __name__ == "__main__":
    main()
