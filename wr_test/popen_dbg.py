"""Debug the exact Popen from _restart_into_new: does the child spawn? does it live?"""
import os
import subprocess
import sys
import time

BASE = r"C:\temp\wr_test"
ERR = os.path.join(BASE, "child_stderr.txt")


def log(m):
    with open(os.path.join(BASE, "popen_dbg.log"), "a") as f:
        f.write("%.1f %s\n" % (time.time(), m))


agent_path = r"C:\winremote\winremote-main\agent\winremote_agent.py"
args = [sys.executable, agent_path, "--port", "48799", "--token", "testtok",
        "--no-auto-update"]
log("popen args=%r" % (args,))
try:
    errf = open(ERR, "w")
    p = subprocess.Popen(
        args,
        cwd=os.path.dirname(agent_path),
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=errf,
        creationflags=0x00000008,
        close_fds=False,
    )
    log("popen ok pid=%s" % p.pid)
    for i in range(4):
        time.sleep(2)
        log("t+%ds child poll=%s" % ((i + 1) * 2, p.poll()))
    errf.close()
except Exception as e:
    log("popen raised %r" % e)
log("done")
