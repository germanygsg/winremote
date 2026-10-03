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
        if urlparse(self.path).path == "/health":
            self._send(200, {"ok": True, "service": "winremote-agent", "time": time.time()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth():
            self._send(401, {"error": "unauthorized"})
            return
        _com_init()
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
                "/uia/focus": self._handle_focus,
                "/input/sendkeys": self._handle_sendkeys,
                "/window/activate": self._handle_activate,
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
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
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

    def _handle_sendkeys(self, body):
        import subprocess
        keys = body.get("keys", "").replace("'", "''")
        ps = ("Add-Type -AssemblyName System.Windows.Forms; "
              f"[System.Windows.Forms.SendKeys]::SendWait('{keys}')")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=15)
        self._send(200, {"ok": True})

    def _handle_activate(self, body):
        el = find_window(body.get("window", ""))
        if not el:
            self._send(404, {"error": "window not found"})
            return
        el.SetFocus()
        self._send(200, {"ok": True})


class ThreadedHTTPServer(__import__("socketserver").ThreadingMixIn,
                         HTTPServer):
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=os.environ.get("WINREMOTE_TOKEN", ""))
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    if auto is None:
        print("ERROR: uiautomation not installed. Run: pip install uiautomation mss Pillow")
        raise SystemExit(1)

    token = args.token or secrets.token_hex(24)
    if not args.token and not os.environ.get("WINREMOTE_TOKEN"):
        print(f"Generated token: {token}")

    Handler.token = token
    server = ThreadedHTTPServer((args.host, args.port), Handler)
    threading.Thread(target=_prune_handles, daemon=True).start()
    print(f"winremote-agent listening on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
