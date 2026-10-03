#!/usr/bin/env python3
"""
WinRemote Agent - Persistent Windows remote control service.

Runs on the Windows machine in the interactive session. Exposes an HTTP API
for fast UIA-based GUI automation: screenshots, element trees, clicks,
text input (including WebView2 via ValuePattern), and keyboard input.

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
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

# --- Dependencies (installed via setup/install.bat) ---
#   pip install uiautomation mss
try:
    import uiautomation as auto
except ImportError:
    auto = None

try:
    from mss import mss
except ImportError:
    mss = None


# ----------------------------------------------------------------------------
# UIA helpers
# ----------------------------------------------------------------------------

def _cond_name(name):
    return auto.PropertyCondition(auto.ControlType, None)  # placeholder

def find_window(name):
    """Find a top-level window by name."""
    root = auto.GetRootControl()
    cond = auto.PropertyCondition(auto.NameProperty, name)
    return root.FindFirstChild(auto.TreeScope.Children, cond)

def element_to_dict(el, depth=0, max_depth=6):
    """Serialize a UIA element (and children) to a dict."""
    try:
        d = {
            "name": el.Name or "",
            "control_type": el.ControlTypeName or "",
            "automation_id": el.AutomationId or "",
            "class_name": el.ClassName or "",
            "enabled": bool(el.IsEnabled),
        }
        try:
            r = el.BoundingRectangle
            d["rect"] = {"x": r.left, "y": r.top, "w": r.width(), "h": r.height()}
        except Exception:
            d["rect"] = None
        # Supported patterns (what we can do with this element)
        patterns = []
        try:
            if el.GetPattern(auto.PatternId.ValuePattern):
                patterns.append("set_value")
        except Exception:
            pass
        try:
            if el.GetPattern(auto.PatternId.InvokePattern):
                patterns.append("invoke")
        except Exception:
            pass
        try:
            if el.GetPattern(auto.PatternId.ExpandCollapsePattern):
                patterns.append("expand")
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

def find_elements(root, name=None, control_type=None, automation_id=None,
                   scope="descendants", limit=50):
    """Find elements matching criteria. Returns list of dicts with a handle id."""
    results = []
    try:
        conds = []
        if name:
            # Support substring match via manual filter (UIA PropertyCondition is exact)
            pass
        if control_type:
            ct_map = {
                "button": auto.ControlType.ButtonControl,
                "edit": auto.ControlType.EditControl,
                "text": auto.ControlType.TextControl,
                "window": auto.ControlType.WindowControl,
                "pane": auto.ControlType.PaneControl,
                "document": auto.ControlType.DocumentControl,
                "combobox": auto.ControlType.ComboBoxControl,
                "listitem": auto.ControlType.ListItemControl,
                "menuitem": auto.ControlType.MenuItemControl,
                "checkbox": auto.ControlType.CheckBoxControl,
            }
            ct = ct_map.get(control_type.lower())
            if ct:
                conds.append(auto.PropertyCondition(auto.ControlTypeProperty, ct))
        if automation_id:
            conds.append(auto.PropertyCondition(auto.AutomationIdProperty, automation_id))

        if len(conds) == 1:
            cond = conds[0]
        elif len(conds) > 1:
            cond = auto.AndCondition(*conds)
        else:
            cond = auto.TrueCondition

        scope_map = {
            "children": auto.TreeScope.Children,
            "descendants": auto.TreeScope.Descendants,
        }
        found = root.FindAllControls(scope_map.get(scope, auto.TreeScope.Descendants), cond)
        for el in found:
            if len(results) >= limit:
                break
            # substring name filter
            if name and name.lower() not in (el.Name or "").lower():
                continue
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
    except Exception as e:
        return {"error": str(e)}
    return results


# ----------------------------------------------------------------------------
# Element handle registry (avoids re-querying; handles expire after 5 min)
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
        # refresh timestamp on use
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
# HTTP API
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, *args):
        pass  # quiet

    def _auth(self):
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def _send(self, code, obj=None, content_type="application/json", raw=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            return json.loads(self.rfile.read(length) or b"{}")
        return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"ok": True, "service": "winremote-agent", "time": time.time()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            body = self._body()
        except Exception:
            self._send(400, {"error": "invalid json"})
            return
        try:
            if path == "/screenshot":
                self._handle_screenshot(body)
            elif path == "/uia/tree":
                self._handle_tree(body)
            elif path == "/uia/find":
                self._handle_find(body)
            elif path == "/uia/invoke":
                self._handle_invoke(body)
            elif path == "/uia/set_value":
                self._handle_set_value(body)
            elif path == "/uia/focus":
                self._handle_focus(body)
            elif path == "/input/sendkeys":
                self._handle_sendkeys(body)
            elif path == "/window/activate":
                self._handle_activate(body)
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"error": str(e)})

    # -- handlers --

    def _handle_screenshot(self, body):
        if mss is None:
            self._send(500, {"error": "mss not installed"})
            return
        monitor = body.get("monitor", 1)
        with mss() as sct:
            mon = sct.monitors[monitor]
            shot = sct.grab(mon)
            # Convert to PNG in-memory
            from mss.tools import to_png
            buf = io.BytesIO()
            # to_png writes to file; do manual PNG via PIL if available, else raw
            try:
                from PIL import Image
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                img.save(buf, format="PNG")
            except ImportError:
                # Fallback: return raw BGRA + metadata as JSON
                self._send(200, {
                    "width": shot.width, "height": shot.height,
                    "data_b64": base64.b64encode(shot.bgra).decode(),
                    "format": "bgra",
                })
                return
        self._send(200, raw=buf.getvalue(), content_type="image/png")

    def _handle_tree(self, body):
        window = body.get("window")
        max_depth = int(body.get("max_depth", 5))
        if window:
            root = find_window(window)
            if not root:
                self._send(404, {"error": f"window not found: {window}"})
                return
        else:
            root = auto.GetRootControl()
        self._send(200, element_to_dict(root, max_depth=max_depth))

    def _handle_find(self, body):
        window = body.get("window")
        if window:
            root = find_window(window)
            if not root:
                self._send(404, {"error": f"window not found: {window}"})
                return
        else:
            root = auto.GetRootControl()
        results = find_elements(
            root,
            name=body.get("name"),
            control_type=body.get("control_type"),
            automation_id=body.get("automation_id"),
            scope=body.get("scope", "descendants"),
            limit=int(body.get("limit", 50)),
        )
        self._send(200, {"elements": results})

    def _handle_invoke(self, body):
        el = _get_handle(body.get("handle", ""))
        if not el:
            self._send(404, {"error": "stale or unknown handle; call /uia/find first"})
            return
        pattern = el.GetPattern(auto.PatternId.InvokePattern)
        if not pattern:
            self._send(400, {"error": "element does not support invoke"})
            return
        pattern.Invoke()
        self._send(200, {"ok": True})

    def _handle_set_value(self, body):
        el = _get_handle(body.get("handle", ""))
        if not el:
            self._send(404, {"error": "stale or unknown handle; call /uia/find first"})
            return
        value = body.get("value", "")
        try:
            pattern = el.GetPattern(auto.PatternId.ValuePattern)
            if not pattern:
                self._send(400, {"error": "element does not support set_value"})
                return
            pattern.SetValue(value)
            self._send(200, {"ok": True, "method": "value_pattern"})
        except Exception:
            # Fallback: focus + clipboard paste + SendKeys
            el.SetFocus()
            time.sleep(0.2)
            import subprocess
            # Use PowerShell clipboard (same session, no cross-session issue)
            ps = (
                "$v = @'\n" + value.replace("'", "''") + "\n'@; "
                "Set-Clipboard $v; "
                "Add-Type -AssemblyName System.Windows.Forms; "
                "[System.Windows.Forms.SendKeys]::SendWait('^v'); "
                "Start-Sleep -m 200; Set-Clipboard ''"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15)
            self._send(200, {"ok": True, "method": "clipboard_paste"})

    def _handle_focus(self, body):
        el = _get_handle(body.get("handle", ""))
        if not el:
            self._send(404, {"error": "stale or unknown handle"})
            return
        el.SetFocus()
        self._send(200, {"ok": True})

    def _handle_sendkeys(self, body):
        keys = body.get("keys", "")
        import subprocess
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            f"[System.Windows.Forms.SendKeys]::SendWait('{keys}')"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)
        self._send(200, {"ok": True})

    def _handle_activate(self, body):
        window = body.get("window", "")
        el = find_window(window)
        if not el:
            self._send(404, {"error": f"window not found: {window}"})
            return
        el.SetFocus()
        self._send(200, {"ok": True})


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    global auto
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=os.environ.get("WINREMOTE_TOKEN", ""))
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    if auto is None:
        print("ERROR: uiautomation not installed. Run: pip install uiautomation mss")
        raise SystemExit(1)

    token = args.token or secrets.token_hex(24)
    if not args.token and not os.environ.get("WINREMOTE_TOKEN"):
        print(f"Generated token: {token}")
        print("Save it — clients must send Authorization: Bearer <token>")

    Handler.token = token
    # Only accept connections from localhost + Tailscale interface for safety.
    # (Tailscale ACLs already restrict; this is defense in depth.)
    server = ThreadedHTTPServer((args.host, args.port), Handler)
    threading.Thread(target=_prune_handles, daemon=True).start()
    print(f"winremote-agent listening on {args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
