#!/usr/bin/env python3
"""
WinRemote client - thin wrapper around the winremote-agent HTTP API.

Usage:
    from winremote import WinRemote
    agent = WinRemote("100.69.141.114", token="...")
    agent.health()
    shot = agent.screenshot()          # PNG bytes
    els = agent.find(window="MedRecPlus", name="Password", control_type="edit")
    agent.set_value(els[0]["handle"], "secret")
    agent.invoke(els[0]["handle"])
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request


class WinRemoteError(Exception):
    pass


class WinRemote:
    def __init__(self, host, port=8765, token="", timeout=30, proxy=None):
        self.base = f"http://{host}:{port}"
        self.token = token
        self.timeout = timeout
        # For Tailscale IPs from the Hatch VM, route via the egress proxy
        # that supports CONNECT (same one SSH uses). Default: hatch-egress-proxy:3130
        if proxy is None and host.startswith("100."):
            proxy = "http://hatch-egress-proxy:3130"
        if proxy:
            self.opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            self.opener = urllib.request.build_opener()

    def _req(self, method, path, body=None, raw_response=False):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        })
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                if raw_response:
                    return resp.read(), resp.headers.get("Content-Type", "")
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}")
            except Exception:
                detail = {}
            raise WinRemoteError(f"HTTP {e.code}: {detail.get('error', e.reason)}")
        except Exception as e:
            raise WinRemoteError(str(e))

    # -- API --

    def health(self):
        return self._req("GET", "/health")

    def screenshot(self, monitor=1, window=None, format="png", quality=75, scale=1.0):
        """Returns (image_bytes, content_type).
        window: crop to this window's rect (substring name match).
        format: 'png' or 'jpeg'. quality: jpeg quality 1-100. scale: downscale factor.
        """
        return self._req("POST", "/screenshot", {
            "monitor": monitor, "window": window,
            "format": format, "quality": quality, "scale": scale,
        }, raw_response=True)

    def tree(self, window=None, max_depth=5):
        return self._req("POST", "/uia/tree", {"window": window, "max_depth": max_depth})

    def find(self, window=None, name=None, control_type=None,
             automation_id=None, scope="descendants", limit=50):
        return self._req("POST", "/uia/find", {
            "window": window, "name": name, "control_type": control_type,
            "automation_id": automation_id, "scope": scope, "limit": limit,
        })["elements"]

    def invoke(self, handle):
        return self._req("POST", "/uia/invoke", {"handle": handle})

    def set_value(self, handle, value):
        """Set text on an edit control. Works on WebView2 inputs via ValuePattern."""
        return self._req("POST", "/uia/set_value", {"handle": handle, "value": value})

    def set_value_at(self, x, y, value):
        """Set text on element at screen coordinates via ValuePattern.
        Bypasses find — uses UIA FromPoint directly."""
        return self._req("POST", "/uia/set_value_at", {"x": x, "y": y, "value": value})

    def paste_text(self, text):
        """Paste text at current focus via clipboard. Click to focus first."""
        return self._req("POST", "/input/paste_text", {"text": text})

    def type_text(self, text):
        """Type text via ctypes SendInput (Unicode). No PowerShell.
        Direct Windows API, reliable for WebView2."""
        return self._req("POST", "/input/type_text", {"text": text})

    def press_key(self, key):
        """Press special key via ctypes SendInput. No PowerShell.
        key: 'enter'|'tab'|'escape'|'backspace'|'delete'|'ctrl_a'|'ctrl_c'|'ctrl_v'"""
        return self._req("POST", "/input/press_key", {"key": key})

    def js_execute(self, script, window="MedRecPlus"):
        """Execute JavaScript in WebView2 via COM. 100% reliable.
        Bypasses all synthetic-input limitations."""
        return self._req("POST", "/js/execute", {"script": script, "window": window})

    def js_eval(self, script):
        """Execute JS via Tauri app's local HTTP eval server.
        100% reliable DOM access. No PowerShell, no UIA."""
        return self._req("POST", "/js/eval", {"script": script})

    def expand(self, handle, action="expand"):
        """Expand/collapse a dropdown via ExpandCollapsePattern.
        action: "expand"|"collapse"|"toggle"."""
        return self._req("POST", "/uia/expand", {"handle": handle, "action": action})

    def select(self, handle):
        """Select an option via SelectionItemPattern."""
        return self._req("POST", "/uia/select", {"handle": handle})

    def select_dropdown_option(self, dropdown_name, option_name, window="MedRecPlus"):
        """High-level: open a Joy UI Select dropdown by name, click the option.
        Uses real coordinate clicks (click_at) because WebView2 doesn't expose
        ExpandCollapsePattern for custom dropdowns. The dropdown button is
        located via UIA, clicked to open, then the option is found and clicked.
        Returns (dropdown_rect, option_rect)."""
        import time
        # Find the dropdown (the visible text element)
        dropdowns = self.find(window=window, name=dropdown_name, limit=5)
        if not dropdowns:
            raise RuntimeError(f"Dropdown '{dropdown_name}' not found")
        drect = dropdowns[0].get("rect")
        if not drect:
            raise RuntimeError(f"Dropdown '{dropdown_name}' has no rect")
        # Click the dropdown button (offset right from text to hit the button area)
        dx = drect["x"] + 200
        dy = drect["y"] + drect["h"] // 2
        self.click_at(dx, dy)
        time.sleep(1.0)
        # Find the option
        options = self.find(window=window, name=option_name, limit=10)
        for opt in options:
            orect = opt.get("rect")
            if orect and orect["w"] > 0:
                ox = orect["x"] + orect["w"] // 2
                oy = orect["y"] + orect["h"] // 2
                self.click_at(ox, oy)
                time.sleep(0.5)
                return drect, orect
        raise RuntimeError(f"Option '{option_name}' not found in dropdown '{dropdown_name}'")

    def focus(self, handle):
        return self._req("POST", "/uia/focus", {"handle": handle})

    def click_at(self, x, y):
        """Real mouse click at screen coordinates."""
        return self._req("POST", "/uia/click_at", {"x": x, "y": y})

    def double_click_at(self, x, y):
        """Real mouse double-click. Must be server-side: two click_at calls
        are seconds apart over HTTP and miss the double-click window."""
        return self._req("POST", "/uia/double_click_at", {"x": x, "y": y})

    def proc_start(self, path, args=None, cwd=None):
        """Start a process in the agent's interactive session. Returns pid."""
        return self._req("POST", "/proc/start",
                         {"path": path, "args": args or [], "cwd": cwd})

    def proc_list(self, name=""):
        """List running processes, optionally filtered by name substring."""
        return self._req("POST", "/proc/list", {"name": name})

    def sendkeys(self, keys):
        """SendKeys string, e.g. '{ENTER}', '^v' (Ctrl+V)."""
        return self._req("POST", "/input/sendkeys", {"keys": keys})

    def activate(self, window):
        return self._req("POST", "/window/activate", {"window": window})

    # -- file transfer --

    def _req_raw(self, method, path, data=None, headers=None):
        """Raw-byte request. Returns (status, body_bytes, headers_dict)."""
        url = self.base + path
        h = {"Authorization": f"Bearer {self.token}"}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}")
            except Exception:
                detail = {}
            raise WinRemoteError(f"HTTP {e.code}: {detail.get('error', e.reason)}")
        except Exception as e:
            raise WinRemoteError(str(e))

    def file_info(self, remote_path):
        """{'size', 'mtime', 'is_dir'} for a file on the Windows machine."""
        return self._req("GET", "/file/info?path=" +
                         urllib.parse.quote(remote_path, safe=""))

    def list_dir(self, remote_path):
        """List a directory on the Windows machine (dirs first)."""
        return self._req("GET", "/file/list?path=" +
                         urllib.parse.quote(remote_path, safe=""))

    def update_check(self):
        """Ask the agent to check GitHub for a new version right now."""
        return self._req("POST", "/update/check", {})

    def upload(self, local_path, remote_path, chunk_size=1024 * 1024):
        """Upload a local file to the Windows machine, chunked + resumable.

        If the remote file already exists, the transfer resumes from its
        current size (the common case after a dropped Tailscale connection).
        """
        size = os.path.getsize(local_path)
        try:
            offset = self.file_info(remote_path).get("size", 0)
        except WinRemoteError:
            offset = 0
        if offset > size:
            offset = 0  # remote is bigger than local: start over
        with open(local_path, "rb") as f:
            f.seek(offset)
            while offset < size:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                status, body, _ = self._req_raw(
                    "POST", "/file/upload", data=chunk, headers={
                        "Content-Type": "application/octet-stream",
                        "X-File-Path": urllib.parse.quote(remote_path, safe=""),
                        "X-File-Offset": str(offset),
                        "X-File-Size": str(size),
                    })
                if status != 200:
                    raise WinRemoteError(f"upload failed at offset {offset}: HTTP {status}")
                resp = json.loads(body or b"{}")
                offset = resp.get("bytes_written", offset + len(chunk))
        return {"ok": True, "path": remote_path, "bytes_written": offset}

    def download(self, remote_path, local_path, chunk_size=1024 * 1024):
        """Download a file from the Windows machine, chunked + resumable.

        If local_path already exists, the transfer resumes from its current
        size instead of starting over.
        """
        total = self.file_info(remote_path)["size"]
        offset = os.path.getsize(local_path) if os.path.exists(local_path) else 0
        if offset > total:
            offset = 0
        with open(local_path, "r+b" if offset else "wb") as f:
            f.seek(offset)
            while offset < total:
                length = min(chunk_size, total - offset)
                status, data, _ = self._req_raw(
                    "GET", "/file/download?path=%s&offset=%d&length=%d" % (
                        urllib.parse.quote(remote_path, safe=""), offset, length))
                if status != 200 or not data:
                    raise WinRemoteError(f"download failed at offset {offset}: HTTP {status}")
                f.write(data)
                offset += len(data)
        return local_path

    # -- convenience --

    def click_button(self, window, name):
        """Find a button by (sub)name and invoke it."""
        els = self.find(window=window, name=name, control_type="button", limit=5)
        if not els:
            raise WinRemoteError(f"button not found: {name!r}")
        return self.invoke(els[0]["handle"])

    def fill(self, window, name, value, control_type="edit"):
        """Find an edit control by (sub)name and set its value."""
        els = self.find(window=window, name=name, control_type=control_type, limit=5)
        if not els:
            raise WinRemoteError(f"field not found: {name!r}")
        return self.set_value(els[0]["handle"], value)
