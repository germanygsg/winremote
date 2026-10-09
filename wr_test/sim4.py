"""Test if Timer-started-from-DAEMON-thread is the killer.
Same as sim3 but the Timer is armed from a daemon handler-like thread."""
import sys
import threading
import time
import urllib.request

sys.path.insert(0, r"C:\winremote\winremote-main\agent")
import winremote_agent as A

A.Handler.token = "t"
srv = A.ThreadedHTTPServer(("127.0.0.1", 48792), A.Handler)
A._server = srv


def restart():
    print("R: entered", flush=True)
    try:
        print("R: calling shutdown", flush=True)
        A._server.shutdown()
        print("R: shutdown returned", flush=True)
    except BaseException as e:
        print("R: shutdown raised %r" % (e,), flush=True)
    print("R: done", flush=True)


def fake_handler():
    """Simulates the daemon request-handler thread: arms the Timer then dies."""
    print("H: daemon handler arming Timer", flush=True)
    threading.Timer(2.0, restart).start()
    print("H: daemon handler returning (thread will die)", flush=True)


def make_request():
    time.sleep(0.5)
    req = urllib.request.Request("http://127.0.0.1:48792/health",
                                 headers={"Authorization": "Bearer t"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print("C: request status", r.status, flush=True)
    except Exception as e:
        print("C: request failed %r" % (e,), flush=True)
    # now simulate the handler arming the timer from a DAEMON thread
    h = threading.Thread(target=fake_handler, daemon=True)
    h.start()
    h.join()
    print("C: daemon handler thread dead", flush=True)


threading.Thread(target=make_request, daemon=True).start()
print("M: serve_forever", flush=True)
srv.serve_forever()
print("M: serve_forever returned", flush=True)
