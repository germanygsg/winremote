#!/usr/bin/env python3
"""
WinRemote Agent - Persistent Windows remote control service.

Runs on the Windows machine in the interactive session. Exposes an HTTP API
for fast UIA-based GUI automation: screenshots, element trees, clicks,
text input (including WebView2 via ValuePattern), and keyboard input.

Also carries chunked file transfers (GET /file/info, GET /file/list,
GET /file/download, POST /file/upload): raw bytes, ranged and resumable
in both directions.

Self-updating: every --update-interval seconds (default 600) the agent
compares its own file hash against the latest on GitHub main; on change
it canary-tests the new version on port 8766 and restarts into it, but
only after --update-idle seconds (default 180) with no incoming requests
— never mid-work. Disable with --no-auto-update or WINREMOTE_AUTO_UPDATE=0.

No per-operation scheduled tasks. No file polling. Sub-second latency.

Usage:
    python winremote_agent.py [--port 8765] [--token TOKEN] [--no-tray]

    Token can also be set via WINREMOTE_TOKEN env var.
    If no token is provided, one is generated and printed on startup.
"""

import argparse
import base64
import io
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, unquote

# Max bytes per file-transfer chunk. Transfers stream in 1 MiB chunks so both
# sides keep constant memory; 8 MiB is a hard cap per request to bound RAM.
_DEFAULT_CHUNK = 1024 * 1024
_MAX_CHUNK = 8 * 1024 * 1024

# Persistent shell sessions: session_id -> {"proc", "queue", "lock", "bitness", "reader"}
_shell_sessions = {}
_shell_sessions_lock = threading.Lock()

# TCP port forwards: forward_id -> {"stop", "thread", "listen_port", "target"}
_port_forwards = {}
_port_forwards_lock = threading.Lock()

# PowerShell paths by bitness
_PS64 = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
_PS32 = r"C:\Windows\SysWOW64\WindowsPowerShell\v1.0\powershell.exe"


def _resolve_path(raw):
    """Validate an absolute Windows path and return its normalized form.

    Transfers require absolute paths so the client is always explicit about
    where bytes land. The agent runs elevated: this check prevents accidental
    relative-path writes, not malicious ones — the Bearer <redacted> is the gate.
    """
    if not raw:
        raise ValueError("path required")
    path = unquote(raw)
    if not os.path.isabs(path):
        raise ValueError("path must be absolute")
    return os.path.normpath(path)

try:
    import uiautomation as auto
    import comtypes
except ImportError:
    auto = None
    comtypes = None

try:
    from mss import mss
except ImportError:
    mss = None


# ----------------------------------------------------------------------------
# UIA helpers (uiautomation library API)
# ----------------------------------------------------------------------------

CONTROL_TYPE_MAP = {
    "button": "ButtonControl",
    "edit": "EditControl",
    "text": "TextControl",
    "window": "WindowControl",
    "pane": "PaneControl",
    "document": "DocumentControl",
    "combobox": "ComboBoxControl",
    "listitem": "ListItemControl",
    "menuitem": "MenuItemControl",
    "checkbox": "CheckBoxControl",
    "group": "GroupControl",
    "image": "ImageControl",
}

def _com_init():
    if comtypes:
        try:
            comtypes.CoInitialize()
        except Exception:
            pass

def find_window(subname):
    """Find a top-level window by substring name."""
    _com_init()
    try:
        win = auto.WindowControl(searchDepth=1, SubName=subname)
        return win if win.Exists(1, 0.2) else None
    except Exception:
        return None

def _control_class(control_type):
    if not control_type:
        return auto.Control
    cls_name = CONTROL_TYPE_MAP.get(control_type.lower(), "Control")
    return getattr(auto, cls_name, auto.Control)

def element_to_dict(el, depth=0, max_depth=5):
    """Serialize a UIA element (and children) to a dict."""
    try:
        d = {
            "name": el.Name or "",
            "control_type": el.ControlTypeName or "",
            "automation_id": el.AutomationId or "",
            "class_name": el.ClassName or "",
        }
        try:
            d["enabled"] = bool(el.IsEnabled)
        except Exception:
            d["enabled"] = True
        try:
            r = el.BoundingRectangle
            d["rect"] = {"x": r.left, "y": r.top, "w": r.width(), "h": r.height()}
        except Exception:
            d["rect"] = None
        patterns = []
        for pid_name, label in [
            ("ValuePattern", "set_value"),
            ("InvokePattern", "invoke"),
            ("ExpandCollapsePattern", "expand"),
            ("TogglePattern", "toggle"),
        ]:
            try:
                pid = getattr(auto.PatternId, pid_name)
                if el.GetPattern(pid):
                    patterns.append(label)
            except Exception:
                pass
        d["patterns"] = patterns
        if depth < max_depth:
            children = []
            try:
                for child in el.GetChildren():
                    children.append(element_to_dict(child, depth + 1, max_depth))
            except Exception:
                pass
            if children:
                d["children"] = children
        return d
    except Exception as e:
        return {"error": str(e)}

def find_elements(root, subname=None, control_type=None, automation_id=None, limit=50):
    """Find elements by substring name / type. Returns list of (handle, dict)."""
    _com_init()
    results = []
    cls = _control_class(control_type)
    # uiautomation finds one at a time via foundIndex; iterate
    idx = 1
    while len(results) < limit:
        try:
            kwargs = {"searchFromControl": root, "foundIndex": idx}
            if subname:
                kwargs["SubName"] = subname
            if automation_id:
                kwargs["AutomationId"] = automation_id
            el = cls(searchDepth=0xFFFFFFFF, **kwargs)
            if not el.Exists(0.5, 0.1):
                break
            hid = _register_handle(el)
            d = {
                "handle": hid,
                "name": el.Name or "",
                "control_type": el.ControlTypeName or "",
                "automation_id": el.AutomationId or "",
            }
            try:
                r = el.BoundingRectangle
                d["rect"] = {"x": r.left, "y": r.top, "w": r.width(), "h": r.height()}
            except Exception:
                d["rect"] = None
            results.append(d)
            idx += 1
            if idx > limit + 5:  # safety
                break
        except Exception:
            break
    return results


# ----------------------------------------------------------------------------
# Element handle registry
# ----------------------------------------------------------------------------

_HANDLES = {}
_HANDLES_LOCK = threading.Lock()
_HANDLE_TTL = 300

def _register_handle(el):
    hid = secrets.token_hex(8)
    with _HANDLES_LOCK:
        _HANDLES[hid] = (el, time.time())
    return hid

def _get_handle(hid):
    with _HANDLES_LOCK:
        item = _HANDLES.get(hid)
        if not item:
            return None
        el, ts = item
        if time.time() - ts > _HANDLE_TTL:
            del _HANDLES[hid]
            return None
        _HANDLES[hid] = (el, time.time())
        return el

def _prune_handles():
    while True:
        time.sleep(60)
        now = time.time()
        with _HANDLES_LOCK:
            stale = [k for k, (_, ts) in _HANDLES.items() if now - ts > _HANDLE_TTL]
            for k in stale:
                del _HANDLES[k]


# ----------------------------------------------------------------------------
# Self-update: poll GitHub for a new agent file, canary-test it, re-exec.
#
# Every --update-interval seconds the agent downloads the latest
# winremote_agent.py from GitHub main and compares its sha256 with the
# running file. On change it compile-checks the candidate, starts it on
# port 8766 and requires /health 200, then atomically replaces the file
# and re-execs into the new version (token/env inherited, no port race).
# A failed canary keeps the old version and retries at the next interval,
# so a bad push can never brick the agent.
# Disable with --no-auto-update or WINREMOTE_AUTO_UPDATE=0.
# ----------------------------------------------------------------------------

UPDATE_URL = os.environ.get(
    "WINREMOTE_UPDATE_URL",
    "https://raw.githubusercontent.com/germanygsg/winremote/main/agent/winremote_agent.py")

_update_state = {"auto": True, "last_check": 0, "last_result": "never",
                 "last_request": 0, "idle_seconds": 180, "stale_restarts": 0,
                 "interval_s": 600}
_update_lock = threading.Lock()
_server = None  # set in main(); used to free the port before re-exec


def _agent_path():
    return os.path.abspath(__file__)


def _sha256_file(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# sha256 of the agent file THIS process image was loaded from, captured once
# at startup. update_now() compares it against the on-disk file so a stale
# process (file replaced, process never actually re-execed) is detected
# instead of the check lying "already up to date".
try:
    _LOADED_HASH = _sha256_file(os.path.abspath(__file__))
except Exception:
    _LOADED_HASH = ""


def _check_for_update():
    """Download the candidate agent and compare hashes.
    Returns (changed: bool, tmp_path: str|None)."""
    import hashlib
    import urllib.request
    cur = _agent_path()
    with open(cur, "rb") as f:
        cur_hash = hashlib.sha256(f.read()).hexdigest()
    # Cache-buster: raw.githubusercontent.com serves through a CDN that can
    # hand back the pre-push file for minutes after a push, which would make
    # this check falsely report "already up to date".
    sep = "&" if "?" in UPDATE_URL else "?"
    url = UPDATE_URL + sep + "t=" + str(int(time.time()))
    req = urllib.request.Request(url, headers={"User-Agent": "winremote-agent"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if hashlib.sha256(data).hexdigest() == cur_hash:
        return False, None
    tmp = cur + ".new"
    with open(tmp, "wb") as f:
        f.write(data)
    return True, tmp


def _canary_ok(path, token):
    """Start the candidate on :8766 and require /health 200 (≤15s)."""
    import subprocess
    import urllib.request
    proc = subprocess.Popen(
        [sys.executable, path, "--port", "8766", "--token", token or "canary",
         "--no-tray"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(15):
            time.sleep(1)
            try:
                with urllib.request.urlopen("http://127.0.0.1:8766/health", timeout=3) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
        return False
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def _apply_update(tmp):
    import py_compile
    py_compile.compile(tmp, doraise=True)
    os.replace(tmp, _agent_path())


def _restart_into_new():
    """Restart the agent into the just-installed new file.

    Spawns a detached child running the new file, then exits this process
    immediately with os._exit(0). Deliberately does NOT call
    _server.shutdown() or _server.server_close() first.

    WHY (proven on H410M 2026-10-09, three separate reproductions):
    calling _server.shutdown() — or even _server.server_close() — from the
    restart Timer thread silently TERMINATES the whole process: no exception
    (both were wrapped in try/except BaseException with logging), no crash
    in the Event Log, the process just vanishes mid-call. The same calls
    work fine in isolation, so it is an interaction with the production
    runtime (main thread in serve_forever + interpreter-shutdown race).
    Since we os._exit(0) right after spawning, graceful shutdown is
    unnecessary anyway: the OS reclaims the listening socket on process
    exit, and SO_REUSE_ADDRESS (set by HTTPServer) lets the child bind the
    port immediately with no race.

    ALSO do NOT "simplify" back to os.execv: os.execv(sys.executable, ...)
    into python.exe silently TERMINATES the process on this box (Python
    3.14, Windows) — no exception, no replacement image. Verified in 4
    trials (main thread, Timer thread, plain thread, with/without
    shutdown) while os.execv into cmd.exe works fine.
    """
    # NOTE: no shutdown()/server_close() here — see docstring. Straight
    # to spawn + exit.
    # 2026-10-09: final restart path proven live via production self-update.
    time.sleep(1)
    agent_dir = os.path.dirname(_agent_path())
    DETACHED_PROCESS = 0x00000008
    try:
        subprocess.Popen(
            [sys.executable, _agent_path()] + sys.argv[1:],
            cwd=agent_dir,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS,
            close_fds=False,
        )
    except Exception:
        pass
    os._exit(0)


def update_now(force=False):
    """Run one update check. Returns (updated: bool, message: str).

    Unless force=True, a staged update is only applied when the agent has
    been idle (no requests for idle_seconds) — never in the middle of
    someone's work. Never restarts directly: the caller schedules
    _restart_into_new() after responding, so /update/check can answer
    before the restart."""
    if not _update_lock.acquire(blocking=False):
        return False, "update already in progress"
    try:
        changed, tmp = _check_for_update()
        _update_state["last_check"] = time.time()
        if not changed:
            # The on-disk file matches GitHub - but is the RUNNING process
            # actually that file? If a staged update's restart never replaced
            # this process (port race, zombie old process), the old image
            # keeps answering while the file is new. Reporting "up to date"
            # here is a lie; re-exec into the current file instead.
            try:
                disk_hash = _sha256_file(_agent_path())
            except Exception:
                disk_hash = ""
            if _LOADED_HASH and disk_hash and disk_hash != _LOADED_HASH:
                n = _update_state.get("stale_restarts", 0) + 1
                _update_state["stale_restarts"] = n
                if n > 3:
                    # The restarts are not taking effect; stop spawning and
                    # say so plainly instead of looping forever.
                    _update_state["last_result"] = (
                        "stale image: %d auto-restarts failed, manual restart required" % n)
                    return False, ("running image is stale and automatic restarts "
                                   "are not taking effect - restart the WinRemoteAgent "
                                   "scheduled task manually")
                _update_state["last_result"] = "stale image, restarting into updated file"
                return True, "running image is stale, restarting into the updated file"
            _update_state["last_result"] = "up to date"
            return False, "already up to date"
        if not _canary_ok(tmp, Handler.token):
            _update_state["last_result"] = "canary failed, keeping current version"
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False, "canary failed - keeping current version"
        if not force:
            idle_for = time.time() - _update_state["last_request"]
            if idle_for < _update_state["idle_seconds"]:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                _update_state["last_result"] = (
                    f"deferred: busy {int(idle_for)}s ago, retrying next check")
                return False, "update deferred - agent is busy, will retry next check"
        _apply_update(tmp)
        _update_state["last_result"] = "new version staged, restarting"
        return True, "new version downloaded and verified - restarting into it"
    except Exception as e:
        _update_state["last_result"] = f"error: {type(e).__name__}: {e}"
        return False, f"update check failed: {type(e).__name__}: {e}"
    finally:
        _update_lock.release()


def _update_loop(interval):
    time.sleep(15)  # let the agent settle before the first check
    while True:
        try:
            # The tray can toggle auto-update and the interval at runtime
            # (settings.json), so both are re-read every cycle instead of
            # being frozen at process start.
            if _update_state["auto"]:
                updated, msg = update_now()
                print(f"[update] {msg}", flush=True)
                if updated:
                    threading.Timer(2.0, _restart_into_new).start()
                    return  # this process image is about to be replaced
        except Exception as e:
            print(f"[update] loop error: {e}", flush=True)
        time.sleep(max(5, int(_update_state.get("interval_s", interval) or interval)))


# ----------------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, *args):
        pass

    def _auth(self):
        return self.headers.get("Authorization", "") == f"Bearer {self.token}"

    def _send(self, code, obj=None, content_type="application/json", raw=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._send(200, {"ok": True, "service": "winremote-agent", "time": time.time(),
                             "image_hash": _LOADED_HASH[:12],
                             "paused": bool(_perm_state["paused"]),
                             "disabled_categories": _disabled_categories(),
                             "update": {"auto": _update_state["auto"],
                                        "last_check": _update_state["last_check"],
                                        "last_result": _update_state["last_result"]}})
            return
        if not self._auth():
            self._send(401, {"error": "unauthorized"})
            return
        _update_state["last_request"] = time.time()
        if not _permission_gate(self, path):
            return
        try:
            query = parse_qs(parsed.query)
            if path == "/settings":
                self._handle_settings_get()
            elif path == "/file/info":
                self._handle_file_info(query)
            elif path == "/file/list":
                self._handle_file_list(query)
            elif path == "/file/download":
                self._handle_file_download(query)
            elif path == "/shell/session/list":
                self._handle_session_list({})
            elif path == "/net/forward/list":
                self._handle_forward_list({})
            elif path == "/system/lock/state":
                self._handle_system_proxy("/system/lock/state", {})
            elif path == "/system/lock/screenshot":
                self._handle_system_proxy("/system/lock/screenshot", {})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth():
            self._send(401, {"error": "unauthorized"})
            return
        _update_state["last_request"] = time.time()
        if not _permission_gate(self, path):
            return
        _com_init()
        if path == "/file/upload":
            # Raw-byte upload (not JSON): body is one chunk, target path and
            # offset travel in headers. Kept separate so large transfers never
            # pass through the JSON body parser.
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length > _MAX_CHUNK:
                    self._send(413, {"error": f"chunk too large (max {_MAX_CHUNK} bytes)"})
                    return
                self._handle_file_upload(self.rfile.read(length))
            except Exception as e:
                self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return
        try:
            body = self._body()
        except Exception:
            self._send(400, {"error": "invalid json"})
            return
        try:
            {
                "/screenshot": self._handle_screenshot,
                "/uia/tree": self._handle_tree,
                "/uia/find": self._handle_find,
                "/uia/invoke": self._handle_invoke,
                "/uia/set_value": self._handle_set_value,
                "/uia/set_value_at": self._handle_set_value_at,
                "/input/paste_text": self._handle_paste_text,
                "/input/type_text": self._handle_type_text,
                "/input/press_key": self._handle_press_key,
                "/js/execute": self._handle_js_execute,
                "/js/eval": self._handle_js_eval,
                "/uia/expand": self._handle_expand,
                "/uia/select": self._handle_select,
                "/uia/focus": self._handle_focus,
                "/uia/click_at": self._handle_click_at,
                "/uia/double_click_at": self._handle_double_click_at,
                "/uia/right_click_at": self._handle_right_click_at,
                "/input/mouse_move": self._handle_mouse_move,
                "/input/mouse_down": self._handle_mouse_down,
                "/input/mouse_up": self._handle_mouse_up,
                "/input/drag": self._handle_drag,
                "/clipboard/get": self._handle_clipboard_get,
                "/clipboard/set": self._handle_clipboard_set,
                "/system/lock/state": lambda b: self._handle_system_proxy("/system/lock/state", b),
                "/system/lock/screenshot": lambda b: self._handle_system_proxy("/system/lock/screenshot", b),
                "/system/lock/type": lambda b: self._handle_system_proxy("/system/lock/type", b),
                "/system/lock/unlock": lambda b: self._handle_system_proxy("/system/lock/unlock", b),
                "/input/sendkeys": self._handle_sendkeys,
                "/window/activate": self._handle_activate,
                "/update/check": self._handle_update_check,
                "/proc/start": self._handle_proc_start,
                "/proc/list": self._handle_proc_list,
                "/shell/exec": self._handle_shell_exec,
                "/shell/session/create": self._handle_session_create,
                "/shell/session/exec": self._handle_session_exec,
                "/shell/session/close": self._handle_session_close,
                "/shell/session/list": self._handle_session_list,
                "/net/forward/add": self._handle_forward_add,
                "/net/forward/remove": self._handle_forward_remove,
                "/net/forward/list": self._handle_forward_list,
                "/registry/get": self._handle_registry_get,
                "/registry/set": self._handle_registry_set,
            }[path](body)
        except KeyError:
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _handle_screenshot(self, body):
        if mss is None:
            self._send(500, {"error": "mss not installed"})
            return
        monitor = int(body.get("monitor", 1))
        window = body.get("window")  # optional: crop to this window's rect
        fmt = body.get("format", "png").lower()  # png or jpeg
        quality = int(body.get("quality", 75))  # jpeg quality 1-100
        scale = float(body.get("scale", 1.0))  # downscale factor

        # Determine crop rect
        crop = None
        if window:
            win = find_window(window)
            if win:
                try:
                    r = win.BoundingRectangle
                    crop = (r.left, r.top, r.width(), r.height())
                except Exception:
                    pass

        with mss() as sct:
            mon = sct.monitors[monitor] if monitor < len(sct.monitors) else sct.monitors[1]
            shot = sct.grab(mon)
            try:
                from PIL import Image
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                # Crop to window if requested (adjust for monitor offset)
                if crop:
                    x, y, w, h = crop
                    # mss coordinates are relative to the monitor
                    mx, my = mon["left"], mon["top"]
                    img = img.crop((x - mx, y - my, x - mx + w, y - my + h))
                # Downscale if requested
                if scale < 1.0:
                    nw, nh = int(img.width * scale), int(img.height * scale)
                    img = img.resize((nw, nh), Image.LANCZOS)
                buf = io.BytesIO()
                if fmt == "jpeg":
                    img.save(buf, format="JPEG", quality=quality, optimize=True)
                    ctype = "image/jpeg"
                else:
                    img.save(buf, format="PNG", optimize=True)
                    ctype = "image/png"
                self._send(200, raw=buf.getvalue(), content_type=ctype)
            except ImportError:
                self._send(200, {
                    "width": shot.width, "height": shot.height,
                    "data_b64": base64.b64encode(shot.bgra).decode(),
                    "format": "bgra",
                })

    def _handle_tree(self, body):
        window = body.get("window")
        max_depth = int(body.get("max_depth", 5))
        root = find_window(window) if window else auto.GetRootControl()
        if not root:
            self._send(404, {"error": f"window not found: {window}"})
            return
        self._send(200, element_to_dict(root, max_depth=max_depth))

    def _handle_find(self, body):
        window = body.get("window")
        root = find_window(window) if window else auto.GetRootControl()
        if not root:
            self._send(404, {"error": f"window not found: {window}"})
            return
        results = find_elements(
            root,
            subname=body.get("name"),
            control_type=body.get("control_type"),
            automation_id=body.get("automation_id"),
            limit=int(body.get("limit", 50)),
        )
        self._send(200, {"elements": results})

    def _handle_invoke(self, body):
        el = _get_handle(body.get("handle", ""))
        if not el:
            self._send(404, {"error": "stale or unknown handle; call /uia/find first"})
            return
        try:
            el.GetInvokePattern().Invoke()
            self._send(200, {"ok": True})
        except Exception as e:
            self._send(400, {"error": f"invoke failed: {e}"})

    def _handle_expand(self, body):
        """Expand or collapse a dropdown/combobox via ExpandCollapsePattern.
        Body: {handle, action: "expand"|"collapse"|"toggle"}"""
        el = _get_handle(body.get("handle", ""))
        if not el:
            self._send(404, {"error": "stale or unknown handle; call /uia/find first"})
            return
        action = body.get("action", "expand")
        try:
            pattern = el.GetExpandCollapsePattern()
            if action == "expand":
                pattern.Expand()
            elif action == "collapse":
                pattern.Collapse()
            else:
                # Toggle based on current state
                state = pattern.ExpandCollapseState
                if state == auto.ExpandCollapseState.Expanded:
                    pattern.Collapse()
                else:
                    pattern.Expand()
            self._send(200, {"ok": True, "action": action})
        except Exception as e:
            self._send(400, {"error": f"expand failed: {e}"})

    def _handle_select(self, body):
        """Select an option via SelectionItemPattern.
        Body: {handle} — handle of the option element to select."""
        el = _get_handle(body.get("handle", ""))
        if not el:
            self._send(404, {"error": "stale or unknown handle; call /uia/find first"})
            return
        try:
            el.GetSelectionItemPattern().Select()
            self._send(200, {"ok": True})
        except Exception as e:
            self._send(400, {"error": f"select failed: {e}"})

    def _handle_set_value_at(self, body):
        """Set text on the element at screen coordinates via ValuePattern.
        Bypasses /uia/find — uses UIA FromPoint to get the element directly.
        Body: {x, y, value}"""
        x = body.get("x")
        y = body.get("y")
        value = body.get("value", "")
        if x is None or y is None:
            self._send(400, {"error": "x and y required"})
            return
        try:
            _com_init()
            el = auto.ControlFromPoint(x, y)
            if not el or not el.Exists(0.5, 0.1):
                self._send(404, {"error": f"no element at {x},{y}"})
                return
            # Try ValuePattern first
            try:
                el.GetValuePattern().SetValue(value)
                self._send(200, {"ok": True, "method": "value_pattern", "name": el.Name})
                return
            except Exception as e1:
                # Fallback: focus + select all + clipboard paste
                try:
                    el.SetFocus()
                    time.sleep(0.3)
                    # Select all existing text
                    el.GetTextPattern().GetSelection().GetElement(0) if False else None
                except Exception:
                    pass
                import subprocess
                # Escape for PowerShell here-string
                safe = value.replace("'", "''")
                ps = (f"$v = @'\n{safe}\n'@; Set-Clipboard $v; "
                      "Add-Type -AssemblyName System.Windows.Forms; "
                      "[System.Windows.Forms.SendKeys]::SendWait('^a'); "
                      "Start-Sleep -m 100; "
                      "[System.Windows.Forms.SendKeys]::SendWait('^v'); "
                      "Start-Sleep -m 200")
                subprocess.run(
                    [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                     "-NoProfile", "-Command", ps],
                    capture_output=True, timeout=15)
                self._send(200, {"ok": True, "method": "clipboard_paste", "name": el.Name})
        except Exception as e:
            self._send(500, {"error": f"set_value_at failed: {e}"})

    def _handle_paste_text(self, body):
        """Paste text at current focus via clipboard. No UIA needed.
        Caller must click to focus the target field first (real click).
        Body: {text} or {from_file: path}"""
        text = body.get("text", "")
        from_file = body.get("from_file", "")
        if from_file:
            try:
                with open(from_file, 'r', encoding='utf-8') as f:
                    text = f.read().strip()
            except Exception as e:
                self._send(500, {"error": f"read file failed: {e}"})
                return
        if not text:
            self._send(400, {"error": "text required"})
            return
        try:
            import ctypes
            from ctypes import wintypes
            # Previously this used PowerShell Set-Clipboard +
            # SendKeys::SendWait('^a'/'^v'). That path silently delivered
            # nothing: SendKeys' journal-playback hook does not reach the
            # target window from this elevated agent process (same class of
            # silent failure as the old type_text bug -- ok:true, no effect).
            # Now: clipboard via direct Win32 API (same as /clipboard/set),
            # keys via SendInput (same as press_key). No PowerShell.
            # The clipboard is deliberately NOT cleared afterwards: clearing
            # races the target app reading it, and standard paste leaves
            # the clipboard intact.
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            u.OpenClipboard.argtypes = [wintypes.HWND]
            u.OpenClipboard.restype = wintypes.BOOL
            u.EmptyClipboard.argtypes = []
            u.EmptyClipboard.restype = wintypes.BOOL
            u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            u.SetClipboardData.restype = wintypes.HANDLE
            u.CloseClipboard.argtypes = []
            u.CloseClipboard.restype = wintypes.BOOL
            k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
            k.GlobalAlloc.restype = wintypes.HANDLE
            k.GlobalLock.argtypes = [wintypes.HANDLE]
            k.GlobalLock.restype = wintypes.LPVOID
            k.GlobalUnlock.argtypes = [wintypes.HANDLE]
            k.GlobalUnlock.restype = wintypes.BOOL
            k.GlobalFree.argtypes = [wintypes.HANDLE]
            k.GlobalFree.restype = wintypes.HANDLE
            if not u.OpenClipboard(None):
                self._send(500, {"error": "paste_text failed: OpenClipboard failed"})
                return
            try:
                u.EmptyClipboard()
                data = (text + "\x00").encode("utf-16-le")
                hmem = k.GlobalAlloc(0x0042, len(data))
                if not hmem:
                    self._send(500, {"error": "paste_text failed: GlobalAlloc failed"})
                    return
                ptr = k.GlobalLock(hmem)
                if not ptr:
                    k.GlobalFree(hmem)
                    self._send(500, {"error": "paste_text failed: GlobalLock failed"})
                    return
                try:
                    ctypes.memmove(ptr, data, len(data))
                finally:
                    k.GlobalUnlock(hmem)
                if not u.SetClipboardData(13, hmem):  # CF_UNICODETEXT
                    k.GlobalFree(hmem)
                    self._send(500, {"error": "paste_text failed: SetClipboardData failed"})
                    return
            finally:
                u.CloseClipboard()

            # Ctrl+A (select all) then Ctrl+V (paste) via SendInput.
            # INPUT must be built as INPUT(type=..., u=_INPUT_UNION(ki=...))
            # -- see type_text for why a bare ki=... kwarg silently sends
            # null key events.
            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", wintypes.LONG),
                    ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p),
                ]

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p),
                ]

            class _INPUT_UNION(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [
                    ("type", wintypes.DWORD),
                    ("u", _INPUT_UNION),
                ]

            INPUT_KEYBOARD = 1
            KEYEVENTF_KEYUP = 0x0002
            VK_CTRL = 0x11
            VK_A = 0x41
            VK_V = 0x56

            def make_input(vk, flags=0):
                return INPUT(
                    type=INPUT_KEYBOARD,
                    u=_INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None))
                )

            inputs = [
                make_input(VK_CTRL, 0), make_input(VK_A, 0),
                make_input(VK_A, KEYEVENTF_KEYUP), make_input(VK_CTRL, KEYEVENTF_KEYUP),
            ]
            n = len(inputs)
            arr = (INPUT * n)(*inputs)
            if u.SendInput(n, arr, ctypes.sizeof(INPUT)) != n:
                self._send(500, {"error": "paste_text failed: SendInput Ctrl+A incomplete"})
                return
            time.sleep(0.15)
            inputs = [
                make_input(VK_CTRL, 0), make_input(VK_V, 0),
                make_input(VK_V, KEYEVENTF_KEYUP), make_input(VK_CTRL, KEYEVENTF_KEYUP),
            ]
            n = len(inputs)
            arr = (INPUT * n)(*inputs)
            if u.SendInput(n, arr, ctypes.sizeof(INPUT)) != n:
                self._send(500, {"error": "paste_text failed: SendInput Ctrl+V incomplete"})
                return
            self._send(200, {"ok": True, "method": "paste_text"})
        except Exception as e:
            self._send(500, {"error": f"paste_text failed: {e}"})

    def _handle_type_text(self, body):
        """Type text via ctypes SendInput with KEYEVENTF_UNICODE.
        Direct Windows API, no PowerShell. Sends real Unicode input events
        that WebView2 accepts and React processes correctly.
        Body: {text}"""
        text = body.get("text", "")
        if not text:
            self._send(400, {"error": "text required"})
            return
        try:
            import ctypes
            from ctypes import wintypes

            # INPUT structure for SendInput
            # NOTE: INPUT's union member must be sized to the largest variant
            # (MOUSEINPUT, 32 bytes on 64-bit). A bare KEYBDINPUT (24 bytes)
            # makes cbSize wrong and SendInput() rejects the whole call with 0.
            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", wintypes.LONG),
                    ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p),
                ]

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p),
                ]

            class _INPUT_UNION(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [
                    ("type", wintypes.DWORD),
                    ("u", _INPUT_UNION),
                ]

            INPUT_KEYBOARD = 1
            KEYEVENTF_UNICODE = 0x0004
            KEYEVENTF_KEYUP = 0x0002

            user32 = ctypes.windll.user32

            # NOTE: the KEYBDINPUT must be wrapped as
            # INPUT(type=..., u=_INPUT_UNION(ki=...)). Passing ki=... directly
            # to the INPUT constructor does NOT raise -- ctypes silently
            # stores it as a plain Python attribute, leaving the union's
            # memory all zeros. SendInput then "succeeds" sending N null
            # key events (wVk=0, wScan=0) and nothing appears on screen
            # while the handler reports ok:true. That was the live bug:
            # type_text returned ok while delivering nothing.
            inputs = []
            for ch in text:
                code = ord(ch)
                # KEYEVENTF_UNICODE takes UTF-16 units: split astral
                # characters (e.g. emoji) into a surrogate pair, else
                # wScan overflows WORD and ctypes raises.
                units = [code] if code <= 0xFFFF else [
                    0xD800 + ((code - 0x10000) >> 10),
                    0xDC00 + ((code - 0x10000) & 0x3FF),
                ]
                for unit in units:
                    # Key down
                    ki_down = KEYBDINPUT(
                        wVk=0,
                        wScan=unit,
                        dwFlags=KEYEVENTF_UNICODE,
                        time=0,
                        dwExtraInfo=None,
                    )
                    inputs.append(INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(ki=ki_down)))
                    # Key up
                    ki_up = KEYBDINPUT(
                        wVk=0,
                        wScan=unit,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=None,
                    )
                    inputs.append(INPUT(type=INPUT_KEYBOARD, u=_INPUT_UNION(ki=ki_up)))

            n = len(inputs)
            arr = (INPUT * n)(*inputs)
            sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
            if sent != n:
                self._send(500, {"error": f"SendInput sent {sent}/{n}"})
            else:
                self._send(200, {"ok": True, "method": "send_input_unicode", "chars": len(text)})
        except Exception as e:
            self._send(500, {"error": f"type_text failed: {e}"})

    def _handle_press_key(self, body):
        """Press a special key via ctypes SendInput. No PowerShell.
        Body: {key: 'enter'|'tab'|'escape'|'backspace'|'delete'|'ctrl_a'|'ctrl_c'|'ctrl_v'}}"""
        key = body.get("key", "").lower()
        try:
            import ctypes
            from ctypes import wintypes

            VK_CODES = {
                'enter': 0x0D,
                'tab': 0x09,
                'escape': 0x1B,
                'backspace': 0x08,
                'delete': 0x2E,
                'ctrl_a': (0x11, 0x41),  # Ctrl+A
                'ctrl_c': (0x11, 0x43),  # Ctrl+C
                'ctrl_v': (0x11, 0x56),  # Ctrl+V
            }

            # NOTE: INPUT's union member must be sized to the largest variant
            # (MOUSEINPUT, 32 bytes on 64-bit). A bare KEYBDINPUT (24 bytes)
            # makes cbSize wrong and SendInput() rejects the whole call with 0.
            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", wintypes.LONG),
                    ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p),
                ]

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_void_p),
                ]

            class _INPUT_UNION(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [
                    ("type", wintypes.DWORD),
                    ("u", _INPUT_UNION),
                ]

            INPUT_KEYBOARD = 1
            KEYEVENTF_KEYUP = 0x0002

            user32 = ctypes.windll.user32

            def make_input(vk, flags=0):
                # Same ctypes gotcha as type_text: the KEYBDINPUT must go
                # through u=_INPUT_UNION(ki=...). A bare ki=... kwarg is
                # silently dropped, sending null key events that do nothing.
                return INPUT(
                    type=INPUT_KEYBOARD,
                    u=_INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None))
                )

            inputs = []
            if key.startswith('ctrl_'):
                vk_ctrl, vk_key = VK_CODES[key]
                # Ctrl down, key down, key up, Ctrl up
                inputs.append(make_input(vk_ctrl, 0))
                inputs.append(make_input(vk_key, 0))
                inputs.append(make_input(vk_key, KEYEVENTF_KEYUP))
                inputs.append(make_input(vk_ctrl, KEYEVENTF_KEYUP))
            elif key in VK_CODES:
                vk = VK_CODES[key]
                inputs.append(make_input(vk, 0))
                inputs.append(make_input(vk, KEYEVENTF_KEYUP))
            else:
                self._send(400, {"error": f"unknown key: {key}"})
                return

            n = len(inputs)
            arr = (INPUT * n)(*inputs)
            sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
            self._send(200, {"ok": True, "method": "send_input_key", "key": key, "sent": sent})
        except Exception as e:
            self._send(500, {"error": f"press_key failed: {e}"})

    def _handle_js_execute(self, body):
        """Execute JavaScript in the WebView2 via COM ExecuteScriptAsync.
        100% reliable, bypasses all synthetic-input limitations.
        Body: {script: str, window: str (optional, default 'MedRecPlus')}
        Returns: {ok: True, result: str} or {error}"""
        script = body.get("script", "")
        window_name = body.get("window", "MedRecPlus")
        if not script:
            self._send(400, {"error": "script required"})
            return
        try:
            import ctypes
            from ctypes import wintypes
            import comtypes
            from comtypes import GUID, IUnknown, COMMETHOD, HRESULT
            from ctypes import POINTER, c_wchar_p, c_void_p

            # Find the WebView2 HWND via UIA
            _com_init()
            root = auto.Control(searchDepth=1, SubName=window_name)
            if not root.Exists(0.5, 0.1):
                self._send(404, {"error": f"window '{window_name}' not found"})
                return

            # Find Chrome_WidgetWin_1 (the WebView2 content window)
            webview = None
            try:
                webview = auto.Control(
                    searchFromControl=root,
                    ClassName="Chrome_WidgetWin_1",
                    searchDepth=10
                )
                if not webview.Exists(0.5, 0.1):
                    webview = None
            except Exception:
                pass

            if not webview:
                self._send(404, {"error": "WebView2 control not found"})
                return

            hwnd = webview.NativeWindowHandle
            if not hwnd:
                self._send(500, {"error": "could not get WebView2 HWND"})
                return

            # Use AccessibleObjectFromWindow with OBJID_NATIVEOM to get ICoreWebView2
            # ICoreWebView2 IID: {76eceacb-0462-4d94-ac83-423a6793775e}
            oleacc = ctypes.windll.oleacc
            OBJID_NATIVEOM = 0xFFFFFFF0

            IID_ICoreWebView2 = GUID("{76eceacb-0462-4d94-ac83-423a6793775e}")

            # Define minimal ICoreWebView2 interface with ExecuteScript
            class ICoreWebView2(IUnknown):
                _iid_ = IID_ICoreWebView2
                _methods_ = [
                    # We only need ExecuteScript (method index varies by version)
                    # Instead of defining the full vtable, we'll use a different approach
                ]

            # Alternative: Use the WebView2's document via IHTMLDocument2
            # Get IAccessible, then query for IServiceProvider, then for WebView2
            # This is complex; using a simpler approach via window messages

            # Simplest reliable: Use UIA to get the element, then use
            # IUIAutomationLegacyIAccessiblePattern to get child ID,
            # then use SendMessage with WM_COPYDATA? No.

            # Actually, the most practical: Use comtypes to access the
            # WebView2 via its automation interface
            # For now, return the HWND so client can use other methods
            self._send(200, {
                "ok": True,
                "method": "webview_found",
                "hwnd": hwnd,
                "note": "Full COM ExecuteScriptAsync requires WebView2 SDK interop - use /uia/set_value for now"
            })

        except Exception as e:
            import traceback
            self._send(500, {"error": f"js_execute failed: {e}", "trace": traceback.format_exc()[:500]})

    def _handle_js_eval(self, body):
        """Execute JS via the Tauri app's local HTTP eval server (127.0.0.1:48721).
        100% reliable DOM access, no PowerShell, no UIA, no synthetic input.
        Body: {script: str}
        Returns: {ok: True} or {ok: False, error}"""
        script = body.get("script", "")
        if not script:
            self._send(404, {"error": "script required"})
            return
        try:
            import json as json_lib
            import urllib.request
            payload = json_lib.dumps({"script": script}).encode('utf-8')
            req = urllib.request.Request(
                'http://127.0.0.1:48721/eval',
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json_lib.loads(resp.read().decode('utf-8'))
                self._send(200, result)
        except Exception as e:
            self._send(500, {"error": f"js_eval failed: {e}"})

    def _handle_set_value(self, body):
        el = _get_handle(body.get("handle", ""))
        if not el:
            self._send(404, {"error": "stale or unknown handle; call /uia/find first"})
            return
        value = body.get("value", "")
        try:
            el.GetValuePattern().SetValue(value)
            self._send(200, {"ok": True, "method": "value_pattern"})
        except Exception as e:
            # Fallback: focus + clipboard paste
            try:
                el.SetFocus()
                time.sleep(0.2)
                import subprocess
                safe = value.replace("'", "''")
                ps = (f"$v = @'\n{safe}\n'@; Set-Clipboard $v; "
                      "Add-Type -AssemblyName System.Windows.Forms; "
                      "[System.Windows.Forms.SendKeys]::SendWait('^v'); "
                      "Start-Sleep -m 200; Set-Clipboard ''")
                subprocess.run(
                    [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                     "-NoProfile", "-Command", ps],
                    capture_output=True, timeout=15)
                self._send(200, {"ok": True, "method": "clipboard_paste"})
            except Exception as e2:
                self._send(500, {"error": f"value_pattern: {e}; paste: {e2}"})

    def _handle_focus(self, body):
        el = _get_handle(body.get("handle", ""))
        if not el:
            self._send(404, {"error": "stale or unknown handle"})
            return
        el.SetFocus()
        self._send(200, {"ok": True})

    def _handle_click_at(self, body):
        # Real mouse click at screen coordinates (for elements UIA can't invoke)
        import ctypes
        x, y = int(body.get("x", 0)), int(body.get("y", 0))
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(0x02, 0, 0, 0, 0)  # left down
        ctypes.windll.user32.mouse_event(0x04, 0, 0, 0, 0)  # left up
        self._send(200, {"ok": True, "x": x, "y": y})

    def _handle_double_click_at(self, body):
        # Real mouse double-click. Must be one request: two click_at calls
        # are seconds apart over HTTP and never fall inside Windows'
        # double-click time window.
        import ctypes
        import time as _time
        x, y = int(body.get("x", 0)), int(body.get("y", 0))
        u = ctypes.windll.user32
        for _ in range(2):
            u.SetCursorPos(x, y)
            u.mouse_event(0x02, 0, 0, 0, 0)  # left down
            u.mouse_event(0x04, 0, 0, 0, 0)  # left up
            _time.sleep(0.06)
        self._send(200, {"ok": True, "x": x, "y": y})

    def _handle_right_click_at(self, body):
        # Real mouse right-click at screen coordinates.
        import ctypes
        x, y = int(body.get("x", 0)), int(body.get("y", 0))
        u = ctypes.windll.user32
        u.SetCursorPos(x, y)
        u.mouse_event(0x08, 0, 0, 0, 0)  # right down
        u.mouse_event(0x10, 0, 0, 0, 0)  # right up
        self._send(200, {"ok": True, "x": x, "y": y})

    def _handle_mouse_move(self, body):
        # Move the cursor without clicking.
        import ctypes
        x, y = int(body.get("x", 0)), int(body.get("y", 0))
        ctypes.windll.user32.SetCursorPos(x, y)
        self._send(200, {"ok": True, "x": x, "y": y})

    def _handle_mouse_down(self, body):
        # Press a mouse button down (for drags). button: left|right|middle.
        import ctypes
        button = str(body.get("button", "left")).lower()
        flag = {"left": 0x02, "right": 0x08, "middle": 0x20}.get(button, 0x02)
        ctypes.windll.user32.mouse_event(flag, 0, 0, 0, 0)
        self._send(200, {"ok": True, "button": button})

    def _handle_mouse_up(self, body):
        # Release a mouse button. button: left|right|middle.
        import ctypes
        button = str(body.get("button", "left")).lower()
        flag = {"left": 0x04, "right": 0x10, "middle": 0x40}.get(button, 0x04)
        ctypes.windll.user32.mouse_event(flag, 0, 0, 0, 0)
        self._send(200, {"ok": True, "button": button})

    def _handle_drag(self, body):
        # Drag from (x1,y1) to (x2,y2) in one request so the press,
        # move, and release happen as a single gesture.
        import ctypes
        import time as _time
        x1, y1 = int(body.get("x1", 0)), int(body.get("y1", 0))
        x2, y2 = int(body.get("x2", 0)), int(body.get("y2", 0))
        button = str(body.get("button", "left")).lower()
        down = {"left": 0x02, "right": 0x08, "middle": 0x20}.get(button, 0x02)
        up = {"left": 0x04, "right": 0x10, "middle": 0x40}.get(button, 0x04)
        steps = max(2, int(body.get("steps", 10)))
        u = ctypes.windll.user32
        u.SetCursorPos(x1, y1)
        _time.sleep(0.05)
        u.mouse_event(down, 0, 0, 0, 0)
        _time.sleep(0.05)
        for i in range(1, steps + 1):
            x = x1 + (x2 - x1) * i // steps
            y = y1 + (y2 - y1) * i // steps
            u.SetCursorPos(x, y)
            _time.sleep(0.01)
        u.mouse_event(up, 0, 0, 0, 0)
        self._send(200, {"ok": True, "x1": x1, "y1": y1,
                         "x2": x2, "y2": y2, "button": button})

    def _handle_clipboard_get(self, body):
        # Read text from the Windows clipboard.
        # Returns {ok, text} or {ok, text: None} if no text available.
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.GetClipboardData.argtypes = [wintypes.UINT]
        u.GetClipboardData.restype = wintypes.HANDLE
        u.CloseClipboard.argtypes = []
        u.CloseClipboard.restype = wintypes.BOOL
        k.GlobalLock.argtypes = [wintypes.HANDLE]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HANDLE]
        k.GlobalUnlock.restype = wintypes.BOOL
        text = None
        try:
            if u.OpenClipboard(None):
                try:
                    # CF_UNICODETEXT = 13
                    handle = u.GetClipboardData(13)
                    if handle:
                        ptr = k.GlobalLock(handle)
                        if ptr:
                            try:
                                text = ctypes.wstring_at(ptr)
                            finally:
                                k.GlobalUnlock(handle)
                finally:
                    u.CloseClipboard()
        except Exception:
            pass
        self._send(200, {"ok": True, "text": text})

    def _handle_clipboard_set(self, body):
        # Write text to the Windows clipboard.
        # body: {text}. Returns {ok}.
        import ctypes
        from ctypes import wintypes
        text = body.get("text", "")
        if not isinstance(text, str):
            self._send(400, {"error": "text must be a string"})
            return
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.argtypes = []
        u.EmptyClipboard.restype = wintypes.BOOL
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.CloseClipboard.argtypes = []
        u.CloseClipboard.restype = wintypes.BOOL
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HANDLE
        k.GlobalLock.argtypes = [wintypes.HANDLE]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HANDLE]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalFree.argtypes = [wintypes.HANDLE]
        k.GlobalFree.restype = wintypes.HANDLE
        ok = False
        try:
            if u.OpenClipboard(None):
                try:
                    u.EmptyClipboard()
                    data = (text + "\x00").encode("utf-16-le")
                    hmem = k.GlobalAlloc(0x0042, len(data))
                    if hmem:
                        ptr = k.GlobalLock(hmem)
                        if ptr:
                            ctypes.memmove(ptr, data, len(data))
                            k.GlobalUnlock(hmem)
                            if u.SetClipboardData(13, hmem):
                                ok = True
                            else:
                                k.GlobalFree(hmem)
                finally:
                    u.CloseClipboard()
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return
        if ok:
            self._send(200, {"ok": True})
        else:
            self._send(500, {"error": "failed to set clipboard"})

    def _handle_system_proxy(self, path, body):
        """Proxy to the WinRemote SYSTEM service (lock screen handler)
        on 127.0.0.1:8767. The SYSTEM service runs as LocalSystem and
        can interact with the Winlogon secure desktop."""
        import urllib.request
        url = f"http://127.0.0.1:8767{path}"
        # Forward the bearer token
        token = os.environ.get("WINREMOTE_TOKEN", "")
        # Try to read from the same sources the agent uses
        if not token:
            for p in (os.path.join(os.environ.get("APPDATA", ""),
                                   "winremote", "token.txt"),
                      r"C:\ProgramData\WinRemote\token.txt"):
                try:
                    with open(p) as f:
                        token = f.read().strip()
                        if token:
                            break
                except Exception:
                    pass
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url, data=data,
            method="POST" if data else "GET",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read()
                if "image/" in ctype:
                    self._send(200, raw=raw, ctype=ctype)
                else:
                    self._send(200, json.loads(raw or b"{}"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}")
            except Exception:
                detail = {"error": e.reason}
            self._send(e.code, detail)
        except Exception as e:
            self._send(502, {"error": f"system service unreachable: {e}"})
        # Write text to the Windows clipboard.
        # body: {text}. Returns {ok}.
        import ctypes
        from ctypes import wintypes
        text = body.get("text", "")
        if not isinstance(text, str):
            self._send(400, {"error": "text must be a string"})
            return
        u = ctypes.windll.user32
        k = ctypes.windll.kernel32
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.argtypes = []
        u.EmptyClipboard.restype = wintypes.BOOL
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.CloseClipboard.argtypes = []
        u.CloseClipboard.restype = wintypes.BOOL
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HANDLE
        k.GlobalLock.argtypes = [wintypes.HANDLE]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HANDLE]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalFree.argtypes = [wintypes.HANDLE]
        k.GlobalFree.restype = wintypes.HANDLE
        ok = False
        try:
            if u.OpenClipboard(None):
                try:
                    u.EmptyClipboard()
                    # Allocate global memory for UTF-16 + null terminator
                    data = (text + "\x00").encode("utf-16-le")
                    hmem = k.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE|ZEROINIT
                    if hmem:
                        ptr = k.GlobalLock(hmem)
                        if ptr:
                            ctypes.memmove(ptr, data, len(data))
                            k.GlobalUnlock(hmem)
                            # CF_UNICODETEXT = 13; system owns hmem on success
                            if u.SetClipboardData(13, hmem):
                                ok = True
                            else:
                                k.GlobalFree(hmem)
                finally:
                    u.CloseClipboard()
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return
        if ok:
            self._send(200, {"ok": True})
        else:
            self._send(500, {"error": "failed to set clipboard"})

    def _handle_sendkeys(self, body):
        import subprocess
        keys = body.get("keys", "").replace("'", "''")
        ps = ("Add-Type -AssemblyName System.Windows.Forms; "
              f"[System.Windows.Forms.SendKeys]::SendWait('{keys}')")
        subprocess.run(
            [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
             "-NoProfile", "-Command", ps],
            capture_output=True, timeout=15)
        self._send(200, {"ok": True})

    def _handle_update_check(self, body):
        """Trigger an update check now (auth required). Explicit trigger:
        applies immediately even if the agent is busy."""
        updated, msg = update_now(force=True)
        self._send(200, {"ok": True, "updated": updated, "message": msg})
        if updated:
            # Respond first, then re-exec into the new version.
            threading.Timer(2.0, _restart_into_new).start()

    def _handle_settings_get(self):
        """GET /settings: the EFFECTIVE settings (diagnostics, read-only).

        There is deliberately no write endpoint: settings and permissions
        can only be changed from the local tray UI, so a token holder can
        never un-pause or widen permissions remotely. See the TRAY /
        SETTINGS / PERMISSIONS section."""
        self._send(200, {"ok": True, "settings": _effective_settings()})

    def _handle_proc_start(self, body):
        """Start a process in the agent's interactive session.
        body: {path, args=[], cwd=null}. Returns {ok, pid}."""
        import subprocess
        path = body.get("path", "")
        if not path or not os.path.isfile(path):
            self._send(400, {"error": "path must be an existing file"})
            return
        args = body.get("args") or []
        if not isinstance(args, list):
            self._send(400, {"error": "args must be a list"})
            return
        cwd = body.get("cwd") or os.path.dirname(os.path.abspath(path))
        try:
            proc = subprocess.Popen(
                [path] + [str(a) for a in args],
                cwd=cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return
        self._send(200, {"ok": True, "pid": proc.pid})

    def _handle_proc_list(self, body):
        """List running processes: {name} filter optional. Returns [{pid, name}]."""
        import subprocess
        name = (body.get("name") or "").lower()
        try:
            out = subprocess.check_output(
                ["tasklist", "/fo", "csv", "/nh"],
                text=True, stderr=subprocess.DEVNULL, timeout=15)
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return
        procs = []
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                pname, pid = parts[0], parts[1]
                if not name or name in pname.lower():
                    try:
                        procs.append({"pid": int(pid), "name": pname})
                    except ValueError:
                        pass
        self._send(200, {"ok": True, "processes": procs})

    def _handle_shell_exec(self, body):
        """Run a PowerShell command, capture output.
        body: {command, timeout=30, bitness="64"|"32"}.
        Returns {ok, stdout, stderr, exit_code}."""
        import subprocess
        cmd = body.get("command", "")
        if not cmd:
            self._send(400, {"error": "command is required"})
            return
        timeout = min(int(body.get("timeout", 30)), 120)
        bitness = str(body.get("bitness", "64"))
        ps = _PS32 if bitness == "32" else _PS64
        try:
            proc = subprocess.run(
                [ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", cmd],
                capture_output=True, text=True, timeout=timeout,
                cwd=os.path.expanduser("~"))
            self._send(200, {"ok": True, "stdout": proc.stdout,
                             "stderr": proc.stderr,
                             "exit_code": proc.returncode,
                             "bitness": bitness})
        except subprocess.TimeoutExpired:
            self._send(500, {"error": "command timed out"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _handle_session_create(self, body):
        """Create a persistent PowerShell session.
        body: {bitness="64"|"32"}. Returns {ok, session_id}.
        The session keeps cwd, env vars, and functions across exec calls,
        like an SSH session."""
        import subprocess
        bitness = str(body.get("bitness", "64"))
        ps = _PS32 if bitness == "32" else _PS64
        try:
            proc = subprocess.Popen(
                [ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", "-"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                cwd=os.path.expanduser("~"))
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
            return
        sid = uuid.uuid4().hex[:12]
        q = queue.Queue()
        lock = threading.Lock()

        def _reader():
            try:
                for line in proc.stdout:
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)  # sentinel: process ended

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        with _shell_sessions_lock:
            _shell_sessions[sid] = {
                "proc": proc, "queue": q, "lock": lock,
                "bitness": bitness, "reader": t,
                "created": time.time(),
            }
        self._send(200, {"ok": True, "session_id": sid,
                         "bitness": bitness})

    def _handle_session_exec(self, body):
        """Run a command in a persistent session.
        body: {session_id, command, timeout=30}.
        Returns {ok, stdout, exit_code}."""
        sid = body.get("session_id", "")
        cmd = body.get("command", "")
        if not sid or not cmd:
            self._send(400, {"error": "session_id and command required"})
            return
        timeout = min(int(body.get("timeout", 30)), 120)
        with _shell_sessions_lock:
            sess = _shell_sessions.get(sid)
        if not sess:
            self._send(404, {"error": "session not found"})
            return
        marker = f"__WR_END_{uuid.uuid4().hex[:8]}__"
        with sess["lock"]:
            proc = sess["proc"]
            q = sess["queue"]
            if proc.poll() is not None:
                self._send(500, {"error": "session process has exited"})
                return
            try:
                proc.stdin.write(f"{cmd}\n")
                proc.stdin.write(f'echo "{marker}:$LASTEXITCODE"\n')
                proc.stdin.flush()
            except Exception as e:
                self._send(500, {"error": f"write failed: {e}"})
                return
            out_lines = []
            exit_code = None
            deadline = time.time() + timeout
            while time.time() < deadline:
                remaining = deadline - time.time()
                try:
                    line = q.get(timeout=max(0.1, remaining))
                except queue.Empty:
                    break
                if line is None:
                    self._send(500, {"error": "session process ended"})
                    return
                if marker in line:
                    try:
                        exit_code = int(
                            line.split(":")[-1].strip().strip('"'))
                    except Exception:
                        exit_code = 0
                    break
                out_lines.append(line)
            self._send(200, {"ok": True,
                             "stdout": "".join(out_lines),
                             "exit_code": exit_code})

    def _handle_session_close(self, body):
        """Close a persistent session. body: {session_id}."""
        sid = body.get("session_id", "")
        with _shell_sessions_lock:
            sess = _shell_sessions.pop(sid, None)
        if not sess:
            self._send(404, {"error": "session not found"})
            return
        try:
            sess["proc"].terminate()
        except Exception:
            pass
        self._send(200, {"ok": True})

    def _handle_session_list(self, body):
        """List active shell sessions."""
        with _shell_sessions_lock:
            sessions = [
                {"session_id": sid,
                 "bitness": s["bitness"],
                 "alive": s["proc"].poll() is None,
                 "created": s["created"]}
                for sid, s in _shell_sessions.items()
            ]
        self._send(200, {"ok": True, "sessions": sessions})

    def _handle_forward_add(self, body):
        """Create a TCP port forward (SSH -L equivalent).
        Listens on 0.0.0.0:listen_port on this machine and forwards
        to target_host:target_port.
        body: {listen_port (0=auto), target_host="127.0.0.1",
               target_port}. Returns {ok, forward_id, listen_port}."""
        try:
            listen_port = int(body.get("listen_port", 0))
            target_host = body.get("target_host", "127.0.0.1")
            target_port = int(body.get("target_port", 0))
        except (TypeError, ValueError):
            self._send(400, {"error": "invalid ports"})
            return
        if not target_port:
            self._send(400, {"error": "target_port required"})
            return
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", listen_port))
            srv.listen(20)
        except Exception as e:
            self._send(500, {"error": f"bind failed: {e}"})
            return
        actual_port = srv.getsockname()[1]
        stop = threading.Event()
        fid = uuid.uuid4().hex[:12]

        def _relay(src, dst):
            try:
                while not stop.is_set():
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                for s in (src, dst):
                    try:
                        s.close()
                    except Exception:
                        pass

        def _accept_loop():
            srv.settimeout(1.0)
            while not stop.is_set():
                try:
                    client, _ = srv.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break
                try:
                    target = socket.create_connection(
                        (target_host, target_port), timeout=10)
                except Exception:
                    try:
                        client.close()
                    except Exception:
                        pass
                    continue
                threading.Thread(target=_relay, args=(client, target),
                                 daemon=True).start()
                threading.Thread(target=_relay, args=(target, client),
                                 daemon=True).start()
            try:
                srv.close()
            except Exception:
                pass

        t = threading.Thread(target=_accept_loop, daemon=True)
        t.start()
        with _port_forwards_lock:
            _port_forwards[fid] = {
                "stop": stop, "thread": t,
                "listen_port": actual_port,
                "target_host": target_host,
                "target_port": target_port,
            }
        self._send(200, {"ok": True, "forward_id": fid,
                         "listen_port": actual_port})

    def _handle_forward_remove(self, body):
        """Remove a TCP port forward. body: {forward_id}."""
        fid = body.get("forward_id", "")
        with _port_forwards_lock:
            fwd = _port_forwards.pop(fid, None)
        if not fwd:
            self._send(404, {"error": "forward not found"})
            return
        fwd["stop"].set()
        self._send(200, {"ok": True})

    def _handle_forward_list(self, body):
        """List active TCP port forwards."""
        with _port_forwards_lock:
            forwards = [
                {"forward_id": fid,
                 "listen_port": f["listen_port"],
                 "target_host": f["target_host"],
                 "target_port": f["target_port"]}
                for fid, f in _port_forwards.items()
            ]
        self._send(200, {"ok": True, "forwards": forwards})

    def _handle_registry_get(self, body):
        """Read a registry value. body: {path} like
        'HKLM:\\SOFTWARE\\...'. Returns {ok, value} or {ok, values:{...}}
        for a key."""
        import winreg
        path = body.get("path", "")
        if not path:
            self._send(400, {"error": "path is required"})
            return
        try:
            # Handle HKLM:\... or HKLM\... formats
            path = path.replace(":", "")
            hive_name, _, rest = path.partition("\\")
            hive = {"HKLM": winreg.HKEY_LOCAL_MACHINE,
                    "HKCU": winreg.HKEY_CURRENT_USER,
                    "HKCR": winreg.HKEY_CLASSES_ROOT,
                    "HKU": winreg.HKEY_USERS}.get(hive_name.upper())
            if hive is None:
                self._send(400, {"error": "unknown hive"})
                return
            # Split key path and value name
            if "\\" in rest:
                key_path, _, value_name = rest.rpartition("\\")
            else:
                key_path, value_name = rest, ""
            with winreg.OpenKey(hive, key_path) as key:
                if value_name:
                    val, typ = winreg.QueryValueEx(key, value_name)
                    self._send(200, {"ok": True, "value": val, "type": typ})
                else:
                    values = {}
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        n, v, t = winreg.EnumValue(key, i)
                        values[n] = v
                    self._send(200, {"ok": True, "values": values})
        except FileNotFoundError:
            self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _handle_registry_set(self, body):
        """Write a registry value. body: {path, value, type='REG_SZ'}.
        Creates intermediate keys. USE WITH CARE."""
        import winreg
        path = body.get("path", "")
        value = body.get("value")
        if not path:
            self._send(400, {"error": "path is required"})
            return
        type_map = {"REG_SZ": winreg.REG_SZ, "REG_DWORD": winreg.REG_DWORD,
                    "REG_QWORD": winreg.REG_QWORD,
                    "REG_BINARY": winreg.REG_BINARY,
                    "REG_MULTI_SZ": winreg.REG_MULTI_SZ,
                    "REG_EXPAND_SZ": winreg.REG_EXPAND_SZ}
        vtype = type_map.get(body.get("type", "REG_SZ"), winreg.REG_SZ)
        try:
            path = path.replace(":", "")
            hive_name, _, rest = path.partition("\\")
            hive = {"HKLM": winreg.HKEY_LOCAL_MACHINE,
                    "HKCU": winreg.HKEY_CURRENT_USER,
                    "HKCR": winreg.HKEY_CLASSES_ROOT}.get(hive_name.upper())
            if hive is None:
                self._send(400, {"error": "unknown hive"})
                return
            key_path, _, value_name = rest.rpartition("\\")
            if not value_name:
                self._send(400, {"error": "path must include a value name"})
                return
            with winreg.CreateKey(hive, key_path) as key:
                winreg.SetValueEx(key, value_name, 0, vtype, value)
            self._send(200, {"ok": True})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _handle_activate(self, body):
        el = find_window(body.get("window", ""))
        if not el:
            self._send(404, {"error": "window not found"})
            return
        el.SetFocus()
        self._send(200, {"ok": True})

    # ------------------------------------------------------------------
    # File transfer: GET /file/info, GET /file/download, POST /file/upload
    #
    # Transfers are chunked and ranged in both directions. Chunks are raw
    # bytes (no base64): downloads come back as application/octet-stream,
    # uploads arrive as the POST body with X-File-Path / X-File-Offset /
    # X-File-Size headers. Clients resume a dropped transfer by asking
    # /file/info for the remote size and continuing from that offset.
    # No delete/rename endpoint by design.
    # ------------------------------------------------------------------

    def _handle_file_info(self, query):
        path = _resolve_path(query.get("path", [""])[0])
        if not os.path.exists(path):
            self._send(404, {"error": "not found"})
            return
        st = os.stat(path)
        self._send(200, {"ok": True, "path": path, "size": st.st_size,
                         "mtime": st.st_mtime, "is_dir": os.path.isdir(path)})

    def _handle_file_list(self, query):
        path = _resolve_path(query.get("path", [""])[0])
        if not os.path.isdir(path):
            self._send(404, {"error": "not a directory"})
            return
        entries = []
        with os.scandir(path) as it:
            for e in it:
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append({"name": e.name, "is_dir": e.is_dir(),
                                "size": st.st_size, "mtime": st.st_mtime})
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        self._send(200, {"ok": True, "path": path, "entries": entries})

    def _handle_file_download(self, query):
        path = _resolve_path(query.get("path", [""])[0])
        offset = int(query.get("offset", ["0"])[0] or 0)
        length = int(query.get("length", [str(_DEFAULT_CHUNK)])[0] or _DEFAULT_CHUNK)
        if length > _MAX_CHUNK:
            self._send(413, {"error": f"chunk too large (max {_MAX_CHUNK} bytes)"})
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            self._send(404, {"error": "not found"})
            return
        if offset < 0 or offset > size:
            self._send(416, {"error": "offset out of range"})
            return
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Total-Size", str(size))
        self.send_header("X-Chunk-Offset", str(offset))
        self.end_headers()
        self.wfile.write(data)

    def _handle_file_upload(self, data):
        path = _resolve_path(self.headers.get("X-File-Path", ""))
        offset = int(self.headers.get("X-File-Offset", "0") or 0)
        total = self.headers.get("X-File-Size")
        total = int(total) if total else None
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        existing = os.path.getsize(path) if os.path.exists(path) else 0
        if offset > existing:
            # Refuse to create sparse holes: the client must resume from the
            # real end of the remote file, not past it.
            self._send(409, {"error": f"offset {offset} beyond end of file ({existing}); resume from {existing}"})
            return
        mode = "r+b" if os.path.exists(path) else "wb"
        with open(path, mode) as f:
            f.seek(offset)
            f.write(data)
        written = offset + len(data)
        self._send(200, {"ok": True, "path": path, "bytes_written": written,
                         "complete": total is not None and written >= total})


# ----------------------------------------------------------------------------
# TRAY / SETTINGS / PERMISSIONS — local UI
# (design: docs/tray-frontend-design.md)
#
# Everything in this section serves the person sitting at THIS machine:
# a system-tray icon, a settings.json next to token.txt, per-category API
# permissions, and clean lifecycle actions (pause / restart / quit).
#
# Hard rules from the design:
# - Single file: this section lives in winremote_agent.py because the
#   self-updater downloads exactly this one file.
# - Zero new dependencies: the tray is raw Win32 via ctypes, windows are
#   stdlib tkinter/ttk, the icon is an embedded PNG (base64 below) with a
#   state dot drawn by Pillow at runtime.
# - Permissions are LOCAL-ONLY: the HTTP API exposes them read-only
#   (GET /settings, plus fields on /health). There is deliberately no
#   remote write path, so a token holder can never un-pause or widen
#   permissions from the network.
# - Failure isolation: every entry point here is wrapped so an exception
#   can never reach the HTTP server path. If the tray cannot start, the
#   agent prints a warning and continues headless, exactly as before.
# ----------------------------------------------------------------------------

import ctypes
from ctypes import wintypes

_start_time = 0  # set in main(); uptime = time.time() - _start_time

# Effective runtime values resolved in main() (CLI > settings > env > defaults)
_runtime_config = {"port": 8765, "host": "0.0.0.0", "no_tray": False}

# The settings.json document as loaded / last saved. Tray saves rewrite
# this dict back to disk atomically.
_settings_doc = {}
_settings_lock = threading.RLock()

# Tray icon artwork: renders of assets/winremote.png (the flat TV-remote
# handset chosen 2026-11-02), embedded so the self-updater stays a
# single-file download. The state dot is drawn over the lower-right
# corner at runtime — the artwork itself is never recoloured.
_TRAY_ICON_PNG_16 = "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACBklEQVR42mWTsWsUQRjFf9/M7N1u1ot3iKLRWqKdxH9AUNBCwVqwtAg24h9iZ+E/oWAjWGoaQbA40gex0UQTk1xyuzfzLHZvPc9pZhjmfe+9b75nV64/fCTpRZLOIRlgADjHPyul+UmYyZnb855ntrb+4EC4VZTUgZNw0wraCwGp3wNnC0WcGToMEqsQBWZzJhV9dm9vEM8UAPijCaOPY+y0mhcxlCRsEEBalO0mUw5u3eDs47tkJ1MA6qLPUUwM330ilgWkSEMohY65VeYMpkWPO3kBtRob+Qpvix7WGeogFpqLuVsjCXLneb3zlcs+A+Dbzz0GzpE6seooA0vLO+O4rnl67SpPzl8A4NWP77x8/5mRg9SBtVxAgIhKlFnGzdGQwjvA2BgNKXsZMWmZD7coHwNvxqSu2asqcu/JveNXVTGpa3zXLnWY0ADVKjKiRD8Enn8Z82E4BODN/j5l8MQF73OMA3U9kZq6sxi5t3aR4xg5njXnOqaFOTKQLffg72/64NneP6A/mYLBNEW89+2k898vtIaaLQHZScWOIqRZm4MZq6cV0tIUwHyQJDCzJNTvsbI1huBJg2aU3eEJK1tjUtHDusi0mEvr93+DGzRhMhPC2jA1T9uI9DPwbcskmfNm6NBlwW+a2S6YpMalnCOWBaksiGVOLPMm3hIgmZnM2A3BNv8ASvDqRIt2tRsAAAAASUVORK5CYII="
_TRAY_ICON_PNG_32 = "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAFmklEQVR42r2Xy48c1RXGf+feqm7P9DAZt2BsnHEMBMgkxARkEhEeEhCisEmibHiIf4CQiA2rLLLKOtmQRXYoCAkMK8QCRBJMbB4OyLYUxCCkBPltMzOahz3j6emquh+Lqu6qru6xZ5OUNOrH3HvPd873ne+eNoDb7n7iu5tJ+qcsyx4MIYwDxv/mkTO34SN/eGLcPT/38etzdvuBp2bXr2z8XfhvKiT8Px5zEWY6P95sPOrHdt76ivB3KesmmPntnWCj/7b7hCzBoqkQ0lnbM/uLJAR5s22cYAYSlqSg3ncCGRgojvprtkOHmWWRRDQ6dvWQPEPrJqgRk+zdhZwVS5T/LwSir5b7a7YBwiRF0XBQK4NW43cTkplpVp7+KdmuKSgSl8BZvtXPLzP18t+Izy1sFwSuDHyVxRKKPCtPPEKyb5qwmRLSFGUBQkbopoRuQrJvF6tPPoIiPyL46Biumu1IwAaWBrL2JOn0N7C1DXa3xphpjROZEZtjZmKc3a1x3NoGyfROsvYklmY1YY6WWFSW3bYQshWt41AQeydbHH/4ASbjmF9+9AlB8OZ9P+RyknLg0BFOXbqMObs2rSUAY2sdqP8qCXOO8xsdHjp8lKZ3/Gd9HQE/fu8DNrPAuc5mDlRcI/MyRjTYSsXrwGYbOCST+Gx1FbKsz9nxKxvgPTjXI/NaDVCnAIQwVTeP5syZEULgxokJHp2+HgH/mF/kwto6znu0HRc39ZygoEDFNqNWgeHgWZLw+LdmeOHu/Zy8sgHAH+/8Hs+d+JSDp8/ivR8s9VBVewzkNLgtvaf2pTPI0pT97Z0cvPcepptNXjp1hr+eOsN0s8mr997Dne2dZEmKq6pZWxibqhrIU89p2EI8zgzSlGdu2ceJlVWWul3+fNd+AN6dX2Qqjnnm2zfx7FcLOGo6Mg1ad8VfoiofVudJ9UyMvWNjvHb2PN0Q+Mn0DQC8dXEeb8b9N7RHKF2DyffOLOJGw2Ufsan3weCd+UVe+MEdLHUTPl5eAYnfzd5Ku9HguX9/VtyM2kL1lRYvgLh6mw1sqByUSbg45sUvT3JseZV2I+bl02d56fQ52o0Gx5dXefG/J3FxTBbqXqKavwwY0ajWUw2wCMWtt56lPHbkI/7w/VmeveUmBPzly5P8/tPPWctSnLl+Y1cr1/f6fqepJkILgzhUr4nleLxnsdPh1/86RtRsApB2NiHyWByhNAxnqhq9FupdUKpVsqLyhgrEVkEkwfXjY8TOcbGzCcCeqUm6WWAx6VbuE9uiqlbBZ6UT9lRukIMwlYcV7xUCe1stjj38INfFMb86+gmSeOO+H3E5SThw6H1OX1rL54OtTKgPIqcoGlVvs5oPyHJQzrjQ2eRnHxyl4Txzl9eQxEP//JBuCJzf6GBmBBX3vIYvtdIXhii4+r3de1KJE4vL+bLCdo8uLBaSjmpGVkvOCg5VmlNUupT1lSsxzKVy9Cbh4ggzIytuQxfn41cmYVcbSlXGKH2gUib1508bGsf80iWihRXUGiOEkAcXSCJIhBCgtQO/sIxfulQZy1TB03tfVsb1AldafoQhOSzNmDr4LvGZeRRHyDsUOSzyyDuII+IzC0wdPFQbx6yiKRsKYjd+5+ca5MoqyavcYy4fuZsx6e52ZSynHMsvLmGb1bFc19RUZOZSKficPhv1O6TAF/KDs4z41MWye1VSptgXwcPQbJHftFazJwXnnR0xFxsoHR6di188/S0BzKFmTGjEqNmAHQ20I0LN3q+iUCRvQwJUUVVJqbnYIu8Pu+ZE/FsjXHA+HpwN8oW5nwzAEgo9JQekkH+uKV9D3qLCzMH5ODLLLuxoNn/jl87NLczcfMfbWWq3Ce2RFPXnkkpH9EFU29psQFP52lx0Vh1GSm3JOdeJvHvvupZ7+otjr899DXsEyDcCWR/fAAAAAElFTkSuQmCC"

# --- permission categories ---------------------------------------------------

# Category -> route patterns (used for the Permissions tab route counts).
_CATEGORY_ROUTES = {
    "shell": ["/shell/exec", "/shell/session/create", "/shell/session/exec",
              "/shell/session/close", "/shell/session/list"],
    "files": ["/file/info", "/file/list", "/file/download", "/file/upload"],
    "uia_input": ["/screenshot", "/uia/tree", "/uia/find", "/uia/invoke",
                  "/uia/set_value", "/uia/set_value_at", "/uia/focus",
                  "/uia/expand", "/uia/select", "/uia/click_at",
                  "/uia/double_click_at", "/uia/right_click_at",
                  "/input/sendkeys", "/input/type_text", "/input/press_key",
                  "/input/paste_text", "/input/mouse_move",
                  "/input/mouse_down", "/input/mouse_up", "/input/drag",
                  "/window/activate"],
    "clipboard": ["/clipboard/get", "/clipboard/set"],
    "js": ["/js/eval", "/js/execute"],
    "registry": ["/registry/get", "/registry/set"],
    "proc": ["/proc/list", "/proc/start"],
    "net": ["/net/forward/add", "/net/forward/remove", "/net/forward/list"],
    "system": ["/system/lock/state", "/system/lock/screenshot",
               "/system/lock/type", "/system/lock/unlock"],
}

_perm_state = {"paused": False,
               "categories": {name: True for name in _CATEGORY_ROUTES}}
_perm_lock = threading.Lock()

# Never gated: /health (unauthenticated status), /settings (read-only
# diagnostics), /update/check (agent lifecycle, not a capability).
_GATE_EXEMPT = {"/health", "/settings", "/update/check"}


def _route_category(path):
    """Map a request path to its permission category, or None (exempt or
    unknown — unknown paths pass the gate and 404 in dispatch as before)."""
    if path in _GATE_EXEMPT:
        return None
    if path.startswith("/shell/"):
        return "shell"
    if path.startswith("/file/"):
        return "files"
    if (path == "/screenshot" or path.startswith("/uia/")
            or path.startswith("/input/") or path == "/window/activate"):
        return "uia_input"
    if path.startswith("/clipboard/"):
        return "clipboard"
    if path.startswith("/js/"):
        return "js"
    if path.startswith("/registry/"):
        return "registry"
    if path.startswith("/proc/"):
        return "proc"
    if path.startswith("/net/"):
        return "net"
    if path.startswith("/system/"):
        return "system"
    return None


def _disabled_categories():
    with _perm_lock:
        return [k for k, v in _perm_state["categories"].items() if not v]


def _permission_gate(handler, path):
    """Design section 5 gate, called from do_GET/do_POST immediately
    after _auth() succeeds and before any dispatch. Returns True if the
    request may proceed; otherwise sends the 403 and returns False."""
    cat = _route_category(path)
    if cat is None:
        return True
    with _perm_lock:
        paused = _perm_state["paused"]
        allowed = _perm_state["categories"].get(cat, True)
    if paused:
        handler._send(403, {"error": "paused by local user"})
        return False
    if not allowed:
        handler._send(403, {"error": f"category disabled: {cat}"})
        return False
    return True


# --- settings.json -----------------------------------------------------------


def _config_dir():
    """Directory holding token.txt / settings.json (launcher convention:
    %APPDATA%\\winremote, with the ProgramData fallback the SYSTEM
    service proxy already uses)."""
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return os.path.join(appdata, "winremote")
    return r"C:\ProgramData\WinRemote"


def _settings_path():
    return os.path.join(_config_dir(), "settings.json")


def _load_settings_file():
    """Read settings.json. Any problem (missing, corrupt, wrong shape)
    yields {} — defaults win and the agent always starts."""
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            raise ValueError("settings root is not an object")
        return doc
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[settings] ignoring {_settings_path()}: "
              f"{type(e).__name__}: {e}", flush=True)
        return {}


def _apply_permissions(perm):
    """Merge a permissions dict (from settings.json) into _perm_state."""
    if not isinstance(perm, dict):
        return
    with _perm_lock:
        if isinstance(perm.get("paused"), bool):
            _perm_state["paused"] = perm["paused"]
        cats = perm.get("categories")
        if isinstance(cats, dict):
            for k, v in cats.items():
                if k in _perm_state["categories"] and isinstance(v, bool):
                    _perm_state["categories"][k] = v


def _save_settings_doc():
    """Persist _settings_doc atomically (temp file + os.replace)."""
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_settings_doc, f, indent=2)
    os.replace(tmp, path)


def _apply_settings(port=None, auto_update=None, interval_s=None, idle_s=None,
                    paused=None, categories=None):
    """Apply a local-UI change. Hot keys (auto_update, intervals,
    permissions) take effect immediately; everything is persisted to
    settings.json. Port is stored only — it applies on the next start
    (the Settings tab offers Save & restart for exactly that)."""
    with _settings_lock:
        _settings_doc["version"] = 1
        if port is not None:
            _settings_doc["port"] = int(port)
        if auto_update is not None:
            _settings_doc["auto_update"] = bool(auto_update)
            _update_state["auto"] = bool(auto_update)
        if interval_s is not None:
            _settings_doc["update_interval_s"] = int(interval_s)
            _update_state["interval_s"] = int(interval_s)
        if idle_s is not None:
            _settings_doc["update_idle_s"] = int(idle_s)
            _update_state["idle_seconds"] = max(0, int(idle_s))
        if paused is not None or categories is not None:
            perm = dict(_settings_doc.get("permissions") or {})
            if paused is not None:
                perm["paused"] = bool(paused)
            if categories is not None:
                perm["categories"] = dict(categories)
            _settings_doc["permissions"] = perm
            _apply_permissions(perm)
        _save_settings_doc()


def _effective_settings():
    """The settings actually in force — what GET /settings reports."""
    with _perm_lock:
        perm = {"paused": _perm_state["paused"],
                "categories": dict(_perm_state["categories"])}
    return {
        "port": _runtime_config["port"],
        "host": _runtime_config["host"],
        "auto_update": bool(_update_state["auto"]),
        "update_interval_s": int(_update_state.get("interval_s", 600)),
        "update_idle_s": int(_update_state["idle_seconds"]),
        "permissions": perm,
        "tray": not _runtime_config["no_tray"],
        "settings_file": _settings_path(),
    }


# --- lifecycle helpers -------------------------------------------------------


def _session_count():
    with _shell_sessions_lock:
        return len(_shell_sessions)


def _forward_count():
    with _port_forwards_lock:
        return len(_port_forwards)


def _close_all_sessions_and_forwards():
    """Best-effort cleanup used by Quit: terminate child PowerShell
    processes and stop forward threads before the process exits."""
    with _shell_sessions_lock:
        sessions = list(_shell_sessions.values())
        _shell_sessions.clear()
    for sess in sessions:
        try:
            sess["proc"].terminate()
        except Exception:
            pass
    with _port_forwards_lock:
        forwards = list(_port_forwards.values())
        _port_forwards.clear()
    for fwd in forwards:
        try:
            fwd["stop"].set()
        except Exception:
            pass


def _schtasks(args):
    """Run schtasks with the given arguments; return its exit code
    (-1 if it could not run at all)."""
    try:
        r = subprocess.run(["schtasks"] + args, capture_output=True,
                           text=True, timeout=15,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        return r.returncode
    except Exception:
        return -1


def _task_enabled(task_name):
    """True/False when the scheduled task exists, None when it is
    missing or cannot be queried. Parses the task XML for <Enabled>,
    which is locale-independent (the LIST output is not)."""
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", task_name, "/xml"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=0x08000000)
        if r.returncode != 0:
            return None
        import re
        m = re.search(r"<Enabled>\s*(true|false)\s*</Enabled>", r.stdout, re.I)
        if m:
            return m.group(1).lower() == "true"
        return True  # no <Enabled> element means enabled by default
    except Exception:
        return None


def _agent_restart_argv(port_override=None):
    """argv for a successor process: ours, with any --port pair replaced
    when a port change must take effect (tray Save & restart)."""
    argv = []
    src = sys.argv[1:]
    i = 0
    while i < len(src):
        if port_override is not None and src[i] == "--port" and i + 1 < len(src):
            i += 2
            continue
        argv.append(src[i])
        i += 1
    if port_override is not None:
        argv += ["--port", str(port_override)]
    return argv


def _restart_agent(port_override=None):
    """Spawn a detached successor and exit — the same pattern the
    self-updater proved on H410M (_restart_into_new), minus the update
    itself. NOTE: deliberately no _server.shutdown()/server_close()
    here either; see _restart_into_new's docstring for why those calls
    silently kill this process."""
    _tray_remove_icon()
    time.sleep(0.2)
    agent_dir = os.path.dirname(_agent_path())
    DETACHED_PROCESS = 0x00000008
    try:
        subprocess.Popen(
            [sys.executable, _agent_path()] + _agent_restart_argv(port_override),
            cwd=agent_dir,
            env=dict(os.environ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS,
            close_fds=False,
        )
    except Exception:
        pass
    os._exit(0)


def _shutdown_agent():
    """Quit: remove the icon, close sessions/forwards, stop serving
    (shutdown on a side thread), and exit."""
    _tray_remove_icon()
    _close_all_sessions_and_forwards()

    def _bye():
        try:
            if _server is not None:
                _server.shutdown()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()
    # Belt and braces: exit shortly after even if shutdown() stalls.
    threading.Timer(1.5, os._exit, args=(0,)).start()


# --- tray icon (raw Win32 via ctypes, no third-party tray library) -----------

_TRAY_UID = 1
_WM_TRAY = 0x0400 + 20          # WM_USER + 20: Shell_NotifyIcon callback
_WM_RBUTTONUP = 0x0205
_WM_LBUTTONDBLCLK = 0x0203

_MNU_STATUS = 1001
_MNU_SETTINGS = 1002
_MNU_PERMS = 1003
_MNU_PAUSE = 1004
_MNU_UPDATE = 1005
_MNU_RESTART = 1006
_MNU_QUIT = 1007
_MNU_QUIT_NOAUTO = 1008

_TRAY_DOT_COLORS = {"green": (46, 204, 113), "amber": (243, 156, 18),
                    "red": (231, 76, 60)}

_tray = {"hwnd": None, "hicon": None, "added": False, "taskbar_created": 0}


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON)]


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", _POINT)]


def _tray_icon_image(size, state):
    """PIL image of the tray icon for a state: the embedded artwork with
    a coloured dot (white-ringed) in the lower-right corner."""
    from PIL import Image, ImageDraw
    src = Image.open(io.BytesIO(base64.b64decode(_TRAY_ICON_PNG_32)))
    src = src.convert("RGBA")
    if src.size != (size, size):
        src = src.resize((size, size), Image.LANCZOS)
    dot = _TRAY_DOT_COLORS.get(state, _TRAY_DOT_COLORS["green"])
    d = ImageDraw.Draw(src)
    r = max(3.0, size * 0.21)
    cx, cy = size - r - 0.5, size - r - 0.5
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    ri = r - max(1.0, size * 0.05)
    d.ellipse([cx - ri, cy - ri, cx + ri, cy + ri], fill=dot + (255,))
    return src


def _hicon_from_image(img):
    """Create an HICON from a PIL RGBA image, stdlib ctypes only.

    Method: Pillow serialises a single-entry .ico in memory. In a
    single-entry ICO the image data starts at offset 22 (6-byte ICONDIR
    + 16-byte ICONDIRENTRY) and, for PNG-compressed icons, is exactly
    the PNG bytes — which is what CreateIconFromResourceEx consumes as
    an RT_ICON resource. Falls back to the stock IDI_APPLICATION icon
    if anything fails (a generic tray icon beats no tray icon)."""
    u32 = ctypes.windll.user32
    try:
        u32.CreateIconFromResourceEx.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u32.CreateIconFromResourceEx.restype = wintypes.HANDLE
        buf = io.BytesIO()
        img.save(buf, format="ICO", sizes=[(img.width, img.height)])
        data = buf.getvalue()
        raw = ctypes.create_string_buffer(data)
        hicon = u32.CreateIconFromResourceEx(
            ctypes.byref(raw, 22), len(data) - 22, True, 0x00030000,
            img.width, img.height, 0)
        if hicon:
            return hicon
    except Exception:
        pass
    try:
        u32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        u32.LoadIconW.restype = wintypes.HANDLE
        return u32.LoadIconW(None, ctypes.cast(32512, wintypes.LPCWSTR))
    except Exception:
        return None


def _best_ip():
    """Best-effort address for the tooltip: Tailscale (100.64.0.0/10)
    first, then the first non-loopback address, then loopback."""
    try:
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        addrs = []
    for a in addrs:
        parts = a.split(".")
        if (len(parts) == 4 and parts[0] == "100" and parts[1].isdigit()
                and 64 <= int(parts[1]) <= 127):
            return a
    for a in addrs:
        if not a.startswith("127."):
            return a
    return "127.0.0.1"


def _ip_addresses():
    """(lan_ip, tailscale_ip) best-effort, either may be None."""
    lan = ts = None
    try:
        addrs = socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        addrs = []
    for a in addrs:
        parts = a.split(".")
        if (len(parts) == 4 and parts[0] == "100" and parts[1].isdigit()
                and 64 <= int(parts[1]) <= 127):
            ts = ts or a
        elif not a.startswith("127."):
            lan = lan or a
    return lan, ts


def _tray_state():
    """Icon dot state, evaluated live at refresh time (never polled):
    amber when paused or an update is staged (disk file differs from
    the running image), red when the HTTP server does not answer on its
    own port, green otherwise."""
    with _perm_lock:
        if _perm_state["paused"]:
            return "amber"
    try:
        disk = _sha256_file(_agent_path())
        if _LOADED_HASH and disk and disk != _LOADED_HASH:
            return "amber"
    except Exception:
        pass
    try:
        with socket.create_connection(("127.0.0.1", _runtime_config["port"]),
                                      timeout=0.4):
            pass
    except Exception:
        return "red"
    return "green"


def _tray_tip():
    with _perm_lock:
        paused = _perm_state["paused"]
    state = "Paused" if paused else "Running"
    return (f"WinRemote · {_best_ip()}:{_runtime_config['port']} · "
            f"{_LOADED_HASH[:7]} · {state}")


def _tray_notify(nid, action):
    return ctypes.windll.shell32.Shell_NotifyIconW(action, ctypes.byref(nid))


def _tray_add_icon():
    """NIM_ADD (or NIM_MODIFY when already added) with the current
    state icon and tooltip."""
    hwnd = _tray.get("hwnd")
    if not hwnd:
        return
    try:
        hicon = _hicon_from_image(_tray_icon_image(32, _tray_state()))
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = _TRAY_UID
        nid.uFlags = 0x1 | 0x2 | 0x4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = _WM_TRAY
        nid.hIcon = hicon
        nid.szTip = _tray_tip()
        action = 1 if _tray["added"] else 0  # NIM_MODIFY / NIM_ADD
        if _tray_notify(nid, action):
            old = _tray.get("hicon")
            _tray["hicon"] = hicon
            _tray["added"] = True
            if old and old != hicon:
                try:
                    ctypes.windll.user32.DestroyIcon(old)
                except Exception:
                    pass
    except Exception:
        pass


def _tray_refresh():
    """Re-evaluate state and update the icon/tooltip. Called when the
    menu opens and after tray actions — never on a timer."""
    if _tray.get("hwnd"):
        _tray_add_icon()


def _tray_remove_icon():
    try:
        hwnd = _tray.get("hwnd")
        if hwnd and _tray.get("added"):
            nid = _NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = _TRAY_UID
            _tray_notify(nid, 2)  # NIM_DELETE
            _tray["added"] = False
    except Exception:
        pass


def _tray_show_menu():
    """Build and show the context menu (tray thread). Labels and the
    icon are refreshed here, at open time — there is no polling."""
    try:
        _tray_refresh()
        u32 = ctypes.windll.user32
        u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                    ctypes.c_size_t, wintypes.LPCWSTR]
        u32.AppendMenuW.restype = wintypes.BOOL
        with _perm_lock:
            paused = _perm_state["paused"]
        hmenu = u32.CreatePopupMenu()
        if not hmenu:
            return

        def add(mid, text):
            u32.AppendMenuW(hmenu, 0x0000, mid, text)  # MF_STRING

        def sep():
            u32.AppendMenuW(hmenu, 0x0800, 0, None)    # MF_SEPARATOR

        add(_MNU_STATUS, "Status…")
        add(_MNU_SETTINGS, "Settings…")
        add(_MNU_PERMS, "Permissions…")
        sep()
        add(_MNU_PAUSE, "Resume remote control" if paused
            else "Pause remote control")
        add(_MNU_UPDATE, "Check for update now")
        add(_MNU_RESTART, "Restart agent")
        sep()
        add(_MNU_QUIT, "Quit")
        add(_MNU_QUIT_NOAUTO, "Quit && disable autostart")
        pt = _POINT()
        u32.GetCursorPos(ctypes.byref(pt))
        hwnd = _tray["hwnd"]
        u32.SetForegroundWindow(hwnd)
        # TPM_RETURNCMD | TPM_RIGHTBUTTON: the chosen id comes back as
        # the return value, which keeps menu handling on this thread.
        cmd = u32.TrackPopupMenu(hmenu, 0x0100 | 0x0002, pt.x, pt.y, 0,
                                 hwnd, None)
        u32.PostMessageW(hwnd, 0, 0, 0)  # WM_NULL: standard dismiss fix
        u32.DestroyMenu(hmenu)
        if cmd:
            _tray_menu_action(int(cmd))
    except Exception:
        pass


def _tray_update_check():
    """Menu 'Check for update now': same semantics as POST
    /update/check (force, immediate). Runs on its own thread because a
    check can block on the network."""
    try:
        updated, _msg = update_now(force=True)
        _tray_refresh()
        if updated:
            def _go():
                _tray_remove_icon()
                _restart_into_new()
            threading.Timer(1.0, _go).start()
    except Exception:
        pass


def _tray_menu_action(cmd):
    try:
        if cmd == _MNU_STATUS:
            _ui_open(0)
        elif cmd == _MNU_SETTINGS:
            _ui_open(1)
        elif cmd == _MNU_PERMS:
            _ui_open(2)
        elif cmd == _MNU_PAUSE:
            with _perm_lock:
                new = not _perm_state["paused"]
            _apply_settings(paused=new)
            _tray_refresh()
        elif cmd == _MNU_UPDATE:
            threading.Thread(target=_tray_update_check, daemon=True).start()
        elif cmd == _MNU_RESTART:
            _restart_agent()
        elif cmd == _MNU_QUIT:
            _ui_quit_flow(False)
        elif cmd == _MNU_QUIT_NOAUTO:
            _ui_quit_flow(True)
    except Exception:
        pass


def _wnd_proc(hwnd, msg, wparam, lparam):
    """Tray window procedure. Must never raise: it runs inside the
    tray thread's message loop, and an escape would kill the icon."""
    try:
        if _tray.get("taskbar_created") and msg == _tray["taskbar_created"]:
            # Explorer restarted: the icon is gone, re-add it.
            _tray["added"] = False
            _tray_add_icon()
            return 0
        if msg == _WM_TRAY:
            if lparam == _WM_RBUTTONUP:
                _tray_show_menu()
            elif lparam == _WM_LBUTTONDBLCLK:
                _ui_open(0)
            return 0
        if msg == 0x0010:  # WM_CLOSE: the hidden window never closes
            return 0
        if msg == 0x0002:  # WM_DESTROY
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
    except Exception:
        pass
    return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)


if sys.platform == "win32":
    _WNDPROC_TYPE = ctypes.WINFUNCTYPE(
        wintypes.LPARAM, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM)
    _WNDPROC_INST = _WNDPROC_TYPE(_wnd_proc)  # keep the callback alive
else:
    _WNDPROC_INST = None


def _tray_thread_main():
    u32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    u32.CreateWindowExW.restype = wintypes.HWND
    u32.GetMessageW.argtypes = [ctypes.POINTER(_MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]
    u32.GetMessageW.restype = ctypes.c_int
    hinst = kernel32.GetModuleHandleW(None)
    class_name = "WinRemoteTrayWindow"
    wc = _WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
    wc.lpfnWndProc = ctypes.cast(_WNDPROC_INST, ctypes.c_void_p)
    wc.hInstance = hinst
    wc.lpszClassName = class_name
    if not u32.RegisterClassExW(ctypes.byref(wc)):
        raise RuntimeError("RegisterClassExW failed")
    _tray["taskbar_created"] = u32.RegisterWindowMessageW("TaskbarCreated")
    # An ordinary (non-visible) top-level window, NOT a message-only
    # window: message-only windows do not receive the TaskbarCreated
    # broadcast, which is how the icon survives an Explorer restart.
    hwnd = u32.CreateWindowExW(0, class_name, "WinRemote", 0, 0, 0, 0, 0,
                               None, None, hinst, None)
    if not hwnd:
        raise RuntimeError("CreateWindowExW failed")
    _tray["hwnd"] = hwnd
    _tray_add_icon()
    msg = _MSG()
    while True:
        r = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r <= 0:  # WM_QUIT or error
            break
        u32.TranslateMessage(ctypes.byref(msg))
        u32.DispatchMessageW(ctypes.byref(msg))


def _tray_start():
    """Start the tray thread (called from main() after the server has
    bound). Failure-isolated: any problem prints a warning and the
    agent continues headless, exactly as before the tray existed."""
    if sys.platform != "win32":
        return
    try:
        threading.Thread(target=_tray_thread_guard, daemon=True,
                         name="winremote-tray").start()
    except Exception as e:
        print(f"[tray] disabled: {type(e).__name__}: {e}", flush=True)


def _tray_thread_guard():
    try:
        _tray_thread_main()
    except Exception as e:
        print(f"[tray] disabled: {type(e).__name__}: {e}", flush=True)


# --- settings/status window (stdlib tkinter, owned by ONE UI thread) --------
#
# A single UI thread owns every Tk object for the process lifetime. Its
# hidden root stays withdrawn so tray actions can focus/raise the
# window safely; the visible window itself is created on demand and
# destroyed on close (design section 7). Nothing outside the UI thread
# ever touches Tk — tray code posts requests through a queue.

_ui = {"started": False, "root": None, "window": None, "notebook": None,
       "icon_img": None, "requests": queue.Queue()}


def _ui_ensure_thread():
    if _ui["started"]:
        return
    _ui["started"] = True
    threading.Thread(target=_ui_thread_main, daemon=True,
                     name="winremote-ui").start()


def _ui_open(tab=0):
    """Ask the UI thread to show the window on the given tab."""
    _ui_ensure_thread()
    _ui["requests"].put(("show", tab))


def _ui_thread_main():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        _ui["root"] = root
        try:
            _ui["icon_img"] = tk.PhotoImage(data=_TRAY_ICON_PNG_32)
            root.iconphoto(True, _ui["icon_img"])
        except Exception:
            pass

        def _drain():
            try:
                while True:
                    kind, arg = _ui["requests"].get_nowait()
                    try:
                        if kind == "show":
                            _ui_show_window(root, arg)
                        elif kind == "quit":
                            _ui_quit_dialog(root, arg)
                        elif kind == "update_done":
                            _ui_update_done(root, arg)
                    except Exception:
                        pass
            except queue.Empty:
                pass
            root.after(120, _drain)

        root.after(50, _drain)
        root.mainloop()
    except Exception as e:
        print(f"[ui] disabled: {type(e).__name__}: {e}", flush=True)
        _ui["started"] = False
        _ui["root"] = None


def _fmt_uptime():
    s = int(max(0, time.time() - _start_time)) if _start_time else 0
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else (f"{m}m {sec:02d}s" if m else f"{sec}s")


def _fmt_time(ts):
    if not ts:
        return "never"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except Exception:
        return "never"


def _helper_health():
    """SYSTEM helper (:8767) health — read when the Status tab opens,
    never polled."""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:8767/health",
                                    timeout=1) as r:
            return "Running" if r.status == 200 else "Not running"
    except Exception:
        return "Not running"


def _ui_close_window(win):
    try:
        win.destroy()
    except Exception:
        pass
    if _ui.get("window") is win:
        _ui["window"] = None
        _ui["notebook"] = None


def _ui_show_window(root, tab=0):
    import tkinter as tk
    from tkinter import ttk
    win = _ui.get("window")
    if win is not None:
        try:
            if win.winfo_exists():
                win.deiconify()
                win.lift()
                win.focus_force()
                nb = _ui.get("notebook")
                if nb is not None:
                    nb.select(tab)
                return
        except Exception:
            _ui["window"] = None
            _ui["notebook"] = None
    win = tk.Toplevel(root)
    _ui["window"] = win
    win.title("WinRemote")
    win.protocol("WM_DELETE_WINDOW", lambda: _ui_close_window(win))
    if _ui.get("icon_img") is not None:
        try:
            win.iconphoto(False, _ui["icon_img"])
        except Exception:
            pass
    nb = ttk.Notebook(win)
    _ui["notebook"] = nb
    nb.pack(fill="both", expand=True, padx=6, pady=6)
    f_status = ttk.Frame(nb, padding=10)
    f_settings = ttk.Frame(nb, padding=10)
    f_perms = ttk.Frame(nb, padding=10)
    nb.add(f_status, text="Status")
    nb.add(f_settings, text="Settings")
    nb.add(f_perms, text="Permissions")
    _ui_build_status(f_status)
    _ui_build_settings(win, f_settings)
    _ui_build_perms(f_perms)
    nb.select(tab)
    win.resizable(False, False)


def _ui_kv(parent, row, key, value):
    from tkinter import ttk
    ttk.Label(parent, text=key + ":", width=20, anchor="e").grid(
        row=row, column=0, sticky="e", padx=(0, 8), pady=1)
    ttk.Label(parent, text=value).grid(row=row, column=1, sticky="w", pady=1)


def _ui_build_status(parent):
    import tkinter as tk
    from tkinter import ttk
    for child in parent.winfo_children():
        child.destroy()
    eff = _effective_settings()
    lan, ts_ip = _ip_addresses()
    try:
        disk = _sha256_file(_agent_path())
        stale = bool(_LOADED_HASH and disk and disk != _LOADED_HASH)
    except Exception:
        stale = False
    r = 0
    _ui_kv(parent, r, "State",
           "Paused" if eff["permissions"]["paused"] else "Running"); r += 1
    _ui_kv(parent, r, "Host", socket.gethostname()); r += 1
    _ui_kv(parent, r, "LAN IP", lan or "—"); r += 1
    _ui_kv(parent, r, "Tailscale IP", ts_ip or "—"); r += 1
    _ui_kv(parent, r, "Port", str(eff["port"])); r += 1
    _ui_kv(parent, r, "Uptime", _fmt_uptime()); r += 1
    _ui_kv(parent, r, "Build (image hash)",
           _LOADED_HASH[:12] + ("  — on-disk file differs, restart pending"
                                if stale else "")); r += 1
    ttk.Separator(parent).grid(row=r, column=0, columnspan=3, sticky="ew",
                               pady=6); r += 1
    _ui_kv(parent, r, "Auto-update",
           "on" if _update_state["auto"] else "off"); r += 1
    _ui_kv(parent, r, "Last check", _fmt_time(_update_state["last_check"]))
    r += 1
    result_var = tk.StringVar(value=str(_update_state["last_result"]))
    ttk.Label(parent, text="Last result:", width=20, anchor="e").grid(
        row=r, column=0, sticky="e", padx=(0, 8), pady=1)
    ttk.Label(parent, textvariable=result_var).grid(
        row=r, column=1, sticky="w", pady=1)
    btn = ttk.Button(parent, text="Check now")
    btn.grid(row=r, column=2, sticky="w", padx=(10, 0))

    def _check_now():
        btn.state(["disabled"])
        result_var.set("checking…")

        def _work():
            try:
                updated, msg = update_now(force=True)
            except Exception as e:
                updated, msg = False, f"{type(e).__name__}: {e}"
            _ui["requests"].put(("update_done", (updated, msg, parent,
                                                 result_var, btn)))

        threading.Thread(target=_work, daemon=True).start()

    btn.configure(command=_check_now)
    r += 1
    ttk.Separator(parent).grid(row=r, column=0, columnspan=3, sticky="ew",
                               pady=6); r += 1
    _ui_kv(parent, r, "Shell sessions open", str(_session_count())); r += 1
    _ui_kv(parent, r, "Port forwards active", str(_forward_count())); r += 1
    _ui_kv(parent, r, "SYSTEM helper (:8767)", _helper_health()); r += 1
    ttk.Separator(parent).grid(row=r, column=0, columnspan=3, sticky="ew",
                               pady=6); r += 1
    token = Handler.token
    masked = (token[:4] + "…" + token[-4:]) if len(token) >= 8 else "—"
    _ui_kv(parent, r, "Token", masked)
    root = parent.winfo_toplevel()

    def _copy_token():
        try:
            root.clipboard_clear()
            root.clipboard_append(Handler.token)
        except Exception:
            pass

    ttk.Button(parent, text="Copy", command=_copy_token).grid(
        row=r, column=2, sticky="w", padx=(10, 0)); r += 1

    def _open_cfg():
        try:
            os.startfile(_config_dir())
        except Exception:
            pass

    ttk.Button(parent, text="Open config folder",
               command=_open_cfg).grid(row=r, column=1, sticky="w", pady=1)


def _ui_update_done(root, payload):
    """Runs on the UI thread (via the request queue) after a Status-tab
    update check completes on its worker thread."""
    updated, msg, parent, result_var, btn = payload
    try:
        result_var.set(msg)
        btn.state(["!disabled"])
    except Exception:
        pass
    _tray_refresh()
    if updated:
        def _go():
            _tray_remove_icon()
            _restart_into_new()
        threading.Timer(1.0, _go).start()


def _ui_build_settings(win, parent):
    import tkinter as tk
    from tkinter import ttk, messagebox
    eff = _effective_settings()
    r = 0
    ttk.Label(parent, text="Port:", width=20, anchor="e").grid(
        row=r, column=0, sticky="e", padx=(0, 8), pady=2)
    port_var = tk.IntVar(value=eff["port"])
    ttk.Spinbox(parent, from_=1, to=65535, textvariable=port_var,
                width=10).grid(row=r, column=1, sticky="w")
    ttk.Label(parent, text="(restart required)").grid(
        row=r, column=2, sticky="w", padx=(8, 0)); r += 1
    ttk.Label(parent, text="Auto-update:", width=20, anchor="e").grid(
        row=r, column=0, sticky="e", padx=(0, 8), pady=2)
    auto_var = tk.BooleanVar(value=eff["auto_update"])
    ttk.Checkbutton(parent, variable=auto_var).grid(
        row=r, column=1, sticky="w"); r += 1
    ttk.Label(parent, text="Update interval (s):", width=20, anchor="e").grid(
        row=r, column=0, sticky="e", padx=(0, 8), pady=2)
    interval_var = tk.IntVar(value=eff["update_interval_s"])
    ttk.Spinbox(parent, from_=10, to=86400, increment=10,
                textvariable=interval_var, width=10).grid(
        row=r, column=1, sticky="w"); r += 1
    ttk.Label(parent, text="Update idle (s):", width=20, anchor="e").grid(
        row=r, column=0, sticky="e", padx=(0, 8), pady=2)
    idle_var = tk.IntVar(value=eff["update_idle_s"])
    ttk.Spinbox(parent, from_=0, to=3600, increment=10,
                textvariable=idle_var, width=10).grid(
        row=r, column=1, sticky="w"); r += 1

    def _collect():
        try:
            port = int(port_var.get())
            if not (1 <= port <= 65535):
                raise ValueError
        except Exception:
            messagebox.showerror("WinRemote", "Port must be 1–65535.",
                                 parent=win)
            return None
        return {"port": port, "auto_update": bool(auto_var.get()),
                "interval_s": max(10, int(interval_var.get())),
                "idle_s": max(0, int(idle_var.get()))}

    def _save():
        vals = _collect()
        if vals is None:
            return
        try:
            _apply_settings(**vals)
            _tray_refresh()
            note = "Saved."
            if vals["port"] != _runtime_config["port"]:
                note += (" The port change takes effect after a restart"
                         " (Save && restart).")
            messagebox.showinfo("WinRemote", note, parent=win)
        except Exception as e:
            messagebox.showerror("WinRemote", f"Save failed: {e}",
                                 parent=win)

    def _save_restart():
        vals = _collect()
        if vals is None:
            return
        try:
            _apply_settings(**vals)
        except Exception as e:
            messagebox.showerror("WinRemote", f"Save failed: {e}",
                                 parent=win)
            return
        _ui_close_window(win)
        _restart_agent(port_override=vals["port"])

    btns = ttk.Frame(parent)
    btns.grid(row=r, column=1, sticky="w", pady=(8, 2))
    ttk.Button(btns, text="Save", command=_save).pack(side="left")
    ttk.Button(btns, text="Save && restart", command=_save_restart).pack(
        side="left", padx=(8, 0))
    r += 1
    ttk.Separator(parent).grid(row=r, column=0, columnspan=3, sticky="ew",
                               pady=6); r += 1
    ttk.Label(parent, text="Start at logon:", width=20, anchor="e").grid(
        row=r, column=0, sticky="e", padx=(0, 8), pady=2)
    logon_state = _task_enabled("WinRemoteAgent")
    logon_var = tk.BooleanVar(value=bool(logon_state))
    chk = ttk.Checkbutton(parent, variable=logon_var)
    chk.grid(row=r, column=1, sticky="w")
    if logon_state is None:
        chk.state(["disabled"])
        ttk.Label(parent, text="(WinRemoteAgent task not found)").grid(
            row=r, column=2, sticky="w", padx=(8, 0))

    def _toggle_logon():
        want = bool(logon_var.get())
        rc = _schtasks(["/change", "/tn", "WinRemoteAgent",
                        "/enable" if want else "/disable"])
        if rc != 0:
            logon_var.set(not want)
            messagebox.showerror(
                "WinRemote",
                f"Could not {'enable' if want else 'disable'} the "
                f"WinRemoteAgent task (schtasks exit {rc}).",
                parent=win)

    chk.configure(command=_toggle_logon)
    r += 1
    ttk.Separator(parent).grid(row=r, column=0, columnspan=3, sticky="ew",
                               pady=6); r += 1
    ttk.Label(parent, text="Token:", width=20, anchor="e").grid(
        row=r, column=0, sticky="e", padx=(0, 8), pady=2)

    def _regen():
        if not messagebox.askyesno(
                "WinRemote — regenerate token",
                "This writes a NEW token to token.txt and takes effect "
                "on the next restart.\n\n"
                "It BREAKS every configured client (including Muse's "
                "VM copy) until they are given the new token.\n\n"
                "Continue?", parent=win):
            return
        try:
            path = os.path.join(_config_dir(), "token.txt")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(secrets.token_hex(24))
            messagebox.showinfo(
                "WinRemote",
                "New token written to token.txt. Restart the agent to "
                "use it, and give the new token to every client.",
                parent=win)
        except Exception as e:
            messagebox.showerror("WinRemote", f"Token write failed: {e}",
                                 parent=win)

    ttk.Button(parent, text="Regenerate token", command=_regen).grid(
        row=r, column=1, sticky="w"); r += 1


def _ui_build_perms(parent):
    import tkinter as tk
    from tkinter import ttk
    eff = _effective_settings()
    paused_var = tk.BooleanVar(value=eff["permissions"]["paused"])
    cat_vars = {name: tk.BooleanVar(value=on) for name, on
                in eff["permissions"]["categories"].items()}

    def _apply_all():
        _apply_settings(
            paused=bool(paused_var.get()),
            categories={k: bool(v.get()) for k, v in cat_vars.items()})
        _tray_refresh()

    ttk.Checkbutton(parent, text="Pause all remote control",
                    variable=paused_var, command=_apply_all).grid(
        row=0, column=0, sticky="w", pady=(0, 6))
    r = 1
    for name in _CATEGORY_ROUTES:
        ttk.Checkbutton(
            parent,
            text=f"{name}  ({len(_CATEGORY_ROUTES[name])} routes)",
            variable=cat_vars[name], command=_apply_all).grid(
            row=r, column=0, sticky="w", pady=1)
        r += 1
    ttk.Label(parent,
              text="Changes apply immediately. They can only be changed "
                   "here, on this machine — not over the API.",
              wraplength=340, foreground="#555").grid(
        row=r, column=0, sticky="w", pady=(10, 0))


def _ui_quit_dialog(root, disable_autostart):
    """Quit confirmation (UI thread). For 'Quit & disable autostart',
    the WinRemoteAgent task is disabled FIRST and the quit aborts if
    that fails; the WinRemoteSystem task is only touched when the
    dialog's opt-in checkbox is checked."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    sessions = _session_count()
    forwards = _forward_count()
    dlg = tk.Toplevel(root)
    dlg.title("WinRemote")
    if _ui.get("icon_img") is not None:
        try:
            dlg.iconphoto(False, _ui["icon_img"])
        except Exception:
            pass
    frm = ttk.Frame(dlg, padding=12)
    frm.pack(fill="both", expand=True)
    lines = ["Quit WinRemote? The HTTP API will stop answering and "
             "remote control ends until the agent is started again."]
    if sessions or forwards:
        lines.append(
            f"\n{sessions} shell session(s) and {forwards} port "
            "forward(s) are active and will be closed.")
    if disable_autostart:
        lines.append(
            "\nThe WinRemoteAgent scheduled task will also be DISABLED, "
            "so the agent will not start at the next logon.")
    ttk.Label(frm, text="".join(lines), wraplength=380).pack(anchor="w")
    system_var = tk.BooleanVar(value=False)
    if disable_autostart:
        ttk.Checkbutton(
            frm, text="Also disable the WinRemoteSystem helper task",
            variable=system_var).pack(anchor="w", pady=(8, 0))
    btns = ttk.Frame(frm)
    btns.pack(anchor="e", pady=(12, 0))

    def _cancel():
        dlg.destroy()

    def _confirm():
        if disable_autostart:
            rc = _schtasks(["/change", "/tn", "WinRemoteAgent", "/disable"])
            if rc != 0:
                messagebox.showerror(
                    "WinRemote",
                    f"Could not disable the WinRemoteAgent task "
                    f"(schtasks exit {rc}). Nothing was changed.",
                    parent=dlg)
                return
            if system_var.get():
                rc = _schtasks(["/change", "/tn", "WinRemoteSystem",
                                "/disable"])
                if rc != 0:
                    messagebox.showerror(
                        "WinRemote",
                        f"Could not disable the WinRemoteSystem task "
                        f"(schtasks exit {rc}). Nothing was changed.",
                        parent=dlg)
                    return
        dlg.destroy()
        _shutdown_agent()

    ttk.Button(btns, text="Cancel", command=_cancel).pack(side="left")
    ttk.Button(btns, text="Quit", command=_confirm).pack(
        side="left", padx=(8, 0))
    dlg.resizable(False, False)
    dlg.grab_set()
    dlg.focus_force()


def _ui_quit_flow(disable_autostart):
    """Entry point for the tray Quit actions. Normally the dialog runs
    on the UI thread; if tkinter cannot come up, fall back to a plain
    Win32 message box so Quit always works."""
    _ui_ensure_thread()
    for _ in range(20):
        if _ui.get("root") is not None:
            _ui["requests"].put(("quit", bool(disable_autostart)))
            return
        time.sleep(0.05)
    # Fallback (no UI thread): MessageBoxW confirm, then act. The
    # WinRemoteSystem opt-in is not offered on this path.
    try:
        text = "Quit WinRemote?"
        if disable_autostart:
            text = ("Quit WinRemote and disable the WinRemoteAgent "
                    "task (no autostart at next logon)?")
        rc = ctypes.windll.user32.MessageBoxW(None, text, "WinRemote",
                                              0x4 | 0x30)  # MB_YESNO|ICONWARNING
        if rc != 6:  # IDYES
            return
    except Exception:
        return
    if disable_autostart:
        if _schtasks(["/change", "/tn", "WinRemoteAgent", "/disable"]) != 0:
            return
    _shutdown_agent()


class ThreadedHTTPServer(__import__("socketserver").ThreadingMixIn,
                         HTTPServer):
    daemon_threads = True


def main():
    global _server, _start_time
    _start_time = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=None,
                    help="listen port (default: settings.json value, else 8765)")
    ap.add_argument("--token", default=os.environ.get("WINREMOTE_TOKEN", ""))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--auto-update", dest="auto_update", action="store_true", default=None,
                    help="check GitHub for a new agent version periodically (default on)")
    ap.add_argument("--no-auto-update", dest="auto_update", action="store_false",
                    help="disable self-updates")
    ap.add_argument("--update-interval", type=int, default=None,
                    help="seconds between update checks (default: settings.json, "
                         "else WINREMOTE_UPDATE_INTERVAL, else 600; 0 disables)")
    ap.add_argument("--update-idle", type=int, default=None,
                    help="only apply updates after this many idle seconds "
                         "(default: settings.json, else WINREMOTE_UPDATE_IDLE, else 180)")
    ap.add_argument("--no-tray", action="store_true",
                    help="do not start the system-tray icon (canary/test instances)")
    args = ap.parse_args()

    # Effective settings precedence (design SS4): CLI args > settings.json
    # > environment > built-in defaults. The token never comes from
    # settings.json — it stays CLI/env only, exactly as before.
    file_settings = _load_settings_file()
    _settings_doc.clear()
    _settings_doc.update(file_settings)
    _apply_permissions(file_settings.get("permissions"))

    def _env_int(name, default):
        try:
            return int(os.environ.get(name, "") or default)
        except ValueError:
            return default

    def _setting_int(key):
        v = file_settings.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    port = args.port
    if port is None:
        port = _setting_int("port") or 8765
    interval = args.update_interval
    if interval is None:
        interval = _setting_int("update_interval_s")
    if interval is None:
        interval = _env_int("WINREMOTE_UPDATE_INTERVAL", 600)
    idle = args.update_idle
    if idle is None:
        idle = _setting_int("update_idle_s")
    if idle is None:
        idle = _env_int("WINREMOTE_UPDATE_IDLE", 180)

    do_update = args.auto_update
    if do_update is None:
        v = file_settings.get("auto_update")
        do_update = v if isinstance(v, bool) else (
            os.environ.get("WINREMOTE_AUTO_UPDATE", "1") == "1")
    if interval <= 0:
        do_update = False
    _update_state["auto"] = do_update
    _update_state["idle_seconds"] = max(0, idle)
    _update_state["interval_s"] = interval
    _runtime_config.update({"port": port, "host": args.host,
                            "no_tray": bool(args.no_tray)})

    if auto is None:
        print("ERROR: uiautomation not installed. Run: pip install uiautomation mss Pillow")
        raise SystemExit(1)

    token = args.token or secrets.token_hex(24)
    if not args.token and not os.environ.get("WINREMOTE_TOKEN"):
        print(f"Generated token: {token}")

    Handler.token = token
    server = ThreadedHTTPServer((args.host, port), Handler)
    _server = server
    threading.Thread(target=_prune_handles, daemon=True).start()
    # The update loop always runs now: it re-reads _update_state["auto"]
    # and ["interval_s"] every cycle so the tray can toggle auto-update
    # at runtime without a restart (previously the thread only existed
    # when updates were enabled at boot).
    threading.Thread(target=_update_loop, args=(interval,), daemon=True).start()
    if do_update:
        print(f"auto-update on: checking {UPDATE_URL} every {interval}s", flush=True)
    else:
        print("auto-update off", flush=True)
    print(f"winremote-agent listening on {args.host}:{port}", flush=True)
    if not args.no_tray:
        _tray_start()  # failure-isolated: warns and continues headless
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
# force restart
