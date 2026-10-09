"""Replicate the PRODUCTION restart exactly (empty argv, env token) but with
logging + child stderr capture, so a child crash leaves evidence."""
import os
import subprocess
import sys
import threading
import time

BASE = r"C:\temp\wr_test"
os.environ["WINREMOTE_TOKEN"] = "testtok3"


def log(m):
    with open(os.path.join(BASE, "e2e3.log"), "a") as f:
        f.write("%.1f %s\n" % (time.time(), m))


sys.path.insert(0, r"C:\winremote\winremote-main\agent")
import winremote_agent as A

srv = A.ThreadedHTTPServer(("127.0.0.1", 48794), A.Handler)
A._server = srv


def restart_logged():
    log("restart: shutdown")
    try:
        A._server.shutdown()
        log("shutdown ok")
    except Exception as e:
        log("shutdown raised %r" % e)
    try:
        A._server.server_close()
        log("server_close ok")
    except Exception as e:
        log("server_close raised %r" % e)
    time.sleep(1)
    # production: sys.argv == ['winremote_agent.py'] -> argv[1:] == []
    args = [sys.executable, A._agent_path()]
    log("popen args=%r" % (args,))
    log("sys.executable=%r" % sys.executable)
    try:
        errf = open(os.path.join(BASE, "e2e3_child_err.txt"), "w")
        p = subprocess.Popen(
            args,
            cwd=os.path.dirname(A._agent_path()),
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=errf,
            creationflags=0x00000008,
            close_fds=False,
        )
        log("popen ok, child pid=%s" % p.pid)
    except Exception as e:
        log("popen raised %r" % e)
    log("parent os._exit(0)")
    os._exit(0)


threading.Timer(2.0, restart_logged).start()
log("serving test server")
srv.serve_forever()
