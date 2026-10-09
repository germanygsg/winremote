"""Which restart primitive actually works on this box?"""
import os
import subprocess
import sys
import time

mode = sys.argv[1]
BASE = r"C:\temp\wr_test"


def log(m):
    with open(os.path.join(BASE, "prim_%s.log" % mode), "a") as f:
        f.write("%.1f %s\n" % (time.time(), m))


if mode == "execv_cmd":
    log("execv into cmd.exe")
    try:
        os.execv(r"C:\Windows\System32\cmd.exe",
                 [r"C:\Windows\System32\cmd.exe", "/c",
                  "echo execv-cmd-ok > %s\\done_prim_execv_cmd.txt" % BASE])
    except Exception as e:
        log("raised: %r" % e)
    log("execv returned?!")
elif mode == "popen_then_exit":
    marker = os.path.join(BASE, "marker_prim_popen.py")
    with open(marker, "w") as f:
        f.write("open(r'%s', 'w').write('popen ok')\n"
                % os.path.join(BASE, "done_prim_popen_then_exit.txt"))
    log("popen %r" % sys.executable)
    DETACHED = 0x00000008
    p = subprocess.Popen([sys.executable, marker],
                         cwd=os.path.dirname(marker),
                         creationflags=DETACHED,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    log("child pid=%s; calling os._exit(0)" % p.pid)
    time.sleep(0.5)
    os._exit(0)
log("fell through (bad)")
