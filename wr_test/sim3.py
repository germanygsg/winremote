"""Repro the shutdown() death: full agent Handler + a real served request,
then Timer -> _restart_into_new. Run with python.exe to see stdout."""
import sys
import threading
import time
import urllib.request

sys.path.insert(0, r"C:\winremote\winremote-main\agent")
import winremote_agent as A

A.Handler.token = "t"
srv = A.ThreadedHTTPServer(("127.0.0.1", 48793), A.Handler)
A._server = srv


def restart():
    print("R: entered", flush=True)
    try:
        print("R: calling shutdown", flush=True)
        A._server.shutdown()
        print("R: shutdown returned", flush=True)
    except BaseException as e:
        print("R: shutdown raised %r" % (e,), flush=True)
    try:
        print("R: calling server_close", flush=True)
        A._server.server_close()
        print("R: server_close returned", flush=True)
    except BaseException as e:
        print("R: server_close raised %r" % (e,), flush=True)
    print("R: done (not exiting, just returning)", flush=True)


def make_request():
    time.sleep(1.0)
    req = urllib.request.Request("http://127.0.0.1:48793/health",
                                 headers={"Authorization": "Bearer t"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            print("C: request status", r.status, flush=True)
    except Exception as e:
        print("C: request failed %r" % (e,), flush=True)


threading.Timer(4.0, restart).start()
threading.Thread(target=make_request, daemon=True).start()
print("M: serve_forever", flush=True)
srv.serve_forever()
print("M: serve_forever returned", flush=True)
