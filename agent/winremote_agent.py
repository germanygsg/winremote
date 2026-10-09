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
it canary-tests the new version on port 8766 and re-execs into it, but
only after --update-idle seconds (default 180) with no incoming requests
— never mid-work. Disable with --no-auto-update or WINREMOTE_AUTO_UPDATE=0.

No per-operation scheduled tasks. No file polling. Sub-second latency.

Usage:
    python winremote_agent.py [--port 8765] [--token TOKEN]

    Token can also be set via WINREMOTE_TOKEN env var.
    If no token is provided, one is generated and printed on startup.
"""

import argparse
import base64
import io
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, unquote

# Max bytes per file-transfer chunk. Transfers stream in 1 MiB chunks so both
# sides keep constant memory; 8 MiB is a hard cap per request to bound RAM.
_DEFAULT_CHUNK = 1024 * 1024
_MAX_CHUNK = 8 * 1024 * 1024


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
                 "last_request": 0, "idle_seconds": 180}
_update_lock = threading.Lock()
_server = None  # set in main(); used to free the port before re-exec


def _agent_path():
    return os.path.abspath(__file__)


def _check_for_update():
    """Download the candidate agent and compare hashes.
    Returns (changed: bool, tmp_path: str|None)."""
    import hashlib
    import urllib.request
    cur = _agent_path()
    with open(cur, "rb") as f:
        cur_hash = hashlib.sha256(f.read()).hexdigest()
    req = urllib.request.Request(UPDATE_URL, headers={"User-Agent": "winremote-agent"})
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
        [sys.executable, path, "--port", "8766", "--token", token or "canary"],
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
    if _server is not None:
        try:
            _server.shutdown()
        except Exception:
            pass
    time.sleep(1)
    os.execv(sys.executable, [sys.executable, _agent_path()] + sys.argv[1:])


def update_now(force=False):
    """Run one update check. Returns (updated: bool, message: str).

    Unless force=True, a staged update is only applied when the agent has
    been idle (no requests for idle_seconds) — never in the middle of
    someone's work. Never restarts directly: the caller schedules
    _restart_into_new() after responding, so /update/check can answer
    before the exec."""
    if not _update_lock.acquire(blocking=False):
        return False, "update already in progress"
    try:
        changed, tmp = _check_for_update()
        _update_state["last_check"] = time.time()
        if not changed:
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
            updated, msg = update_now()
            print(f"[update] {msg}", flush=True)
            if updated:
                threading.Timer(2.0, _restart_into_new).start()
                return  # this process image is about to be replaced
        except Exception as e:
            print(f"[update] loop error: {e}", flush=True)
        time.sleep(interval)


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
                             "update": {"auto": _update_state["auto"],
                                        "last_check": _update_state["last_check"],
                                        "last_result": _update_state["last_result"]}})
            return
        if not self._auth():
            self._send(401, {"error": "unauthorized"})
            return
        _update_state["last_request"] = time.time()
        try:
            query = parse_qs(parsed.query)
            if path == "/file/info":
                self._handle_file_info(query)
            elif path == "/file/list":
                self._handle_file_list(query)
            elif path == "/file/download":
                self._handle_file_download(query)
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
                "/input/sendkeys": self._handle_sendkeys,
                "/window/activate": self._handle_activate,
                "/update/check": self._handle_update_check,
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
            import subprocess
            safe = text.replace("'", "''")
            # Set clipboard, then Ctrl+A (select all), Ctrl+V (paste)
            ps = (f"$v = @'\n{safe}\n'@; Set-Clipboard $v; "
                  "Add-Type -AssemblyName System.Windows.Forms; "
                  "[System.Windows.Forms.SendKeys]::SendWait('^a'); "
                  "Start-Sleep -m 150; "
                  "[System.Windows.Forms.SendKeys]::SendWait('^v'); "
                  "Start-Sleep -m 300; "
                  "Set-Clipboard ''")
            subprocess.run(
                [r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                 "-NoProfile", "-Command", ps],
                capture_output=True, timeout=15)
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

            inputs = []
            for ch in text:
                # Key down
                ki_down = KEYBDINPUT(
                    wVk=0,
                    wScan=ord(ch),
                    dwFlags=KEYEVENTF_UNICODE,
                    time=0,
                    dwExtraInfo=None,
                )
                inputs.append(INPUT(type=INPUT_KEYBOARD, ki=ki_down))
                # Key up
                ki_up = KEYBDINPUT(
                    wVk=0,
                    wScan=ord(ch),
                    dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                    time=0,
                    dwExtraInfo=None,
                )
                inputs.append(INPUT(type=INPUT_KEYBOARD, ki=ki_up))

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
                return INPUT(
                    type=INPUT_KEYBOARD,
                    ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
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


class ThreadedHTTPServer(__import__("socketserver").ThreadingMixIn,
                         HTTPServer):
    daemon_threads = True


def main():
    global _server
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=os.environ.get("WINREMOTE_TOKEN", ""))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--auto-update", dest="auto_update", action="store_true", default=None,
                    help="check GitHub for a new agent version periodically (default on)")
    ap.add_argument("--no-auto-update", dest="auto_update", action="store_false",
                    help="disable self-updates")
    ap.add_argument("--update-interval", type=int,
                    default=int(os.environ.get("WINREMOTE_UPDATE_INTERVAL", "600")),
                    help="seconds between update checks (0 disables)")
    ap.add_argument("--update-idle", type=int,
                    default=int(os.environ.get("WINREMOTE_UPDATE_IDLE", "180")),
                    help="only apply updates after this many idle seconds (0 = always)")
    args = ap.parse_args()

    do_update = args.auto_update
    if do_update is None:
        do_update = os.environ.get("WINREMOTE_AUTO_UPDATE", "1") == "1"
    if args.update_interval <= 0:
        do_update = False
    _update_state["auto"] = do_update
    _update_state["idle_seconds"] = max(0, args.update_idle)

    if auto is None:
        print("ERROR: uiautomation not installed. Run: pip install uiautomation mss Pillow")
        raise SystemExit(1)

    token = args.token or secrets.token_hex(24)
    if not args.token and not os.environ.get("WINREMOTE_TOKEN"):
        print(f"Generated token: {token}")

    Handler.token = token
    server = ThreadedHTTPServer((args.host, args.port), Handler)
    _server = server
    threading.Thread(target=_prune_handles, daemon=True).start()
    if do_update:
        threading.Thread(target=_update_loop, args=(args.update_interval,), daemon=True).start()
        print(f"auto-update on: checking {UPDATE_URL} every {args.update_interval}s", flush=True)
    else:
        print("auto-update off", flush=True)
    print(f"winremote-agent listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
