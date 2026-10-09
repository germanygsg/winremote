"""End-to-end test of the fixed _restart_into_new using the REAL agent module.
Spawns the real agent as a child on port 48799 via the real restart fn."""
import sys
import threading

sys.argv = ["winremote_agent.py", "--port", "48799", "--token", "testtok",
            "--no-auto-update"]
sys.path.insert(0, r"C:\winremote\winremote-main\agent")
import winremote_agent as A

A.Handler.token = "testtok"
srv = A.ThreadedHTTPServer(("127.0.0.1", 48799), A.Handler)
A._server = srv
threading.Timer(2.0, A._restart_into_new).start()
srv.serve_forever()
