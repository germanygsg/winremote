"""Isolate WHY os.execv dies silently: direct vs Timer-thread vs plain-thread."""
import os
import sys
import threading
import time

mode = sys.argv[1]
BASE = r"C:\temp\wr_test"
donefile = os.path.join(BASE, "done_exper_%s.txt" % mode)


def log(m):
    with open(os.path.join(BASE, "exper_%s.log" % mode), "a") as f:
        f.write("%.1f %s\n" % (time.time(), m))


target = os.path.join(BASE, "marker_exper_%s.py" % mode)
with open(target, "w") as f:
    f.write("open(r'%s', 'w').write('execv ok')\n" % donefile)

log("sys.executable=%r" % sys.executable)
args = [sys.executable, target]
log("execv args=%r" % args)


def do_execv():
    cur = threading.current_thread()
    log("do_execv on thread %r is_main=%s" % (cur.name, cur is threading.main_thread()))
    try:
        os.execv(sys.executable, args)
    except Exception as e:
        log("execv raised: %r" % e)
    log("execv RETURNED (impossible if it worked)")


if mode == "direct":
    log("calling execv on main thread")
    do_execv()
elif mode == "timer":
    threading.Timer(2.0, do_execv).start()
    log("timer armed; main sleeping 12s")
    time.sleep(12)
    log("main sleep done")
elif mode == "thread":
    t = threading.Thread(target=do_execv)
    t.start()
    log("plain thread started; main joining")
    t.join()
    log("thread joined")
log("end of script reached")
