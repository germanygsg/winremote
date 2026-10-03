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

    def screenshot(self, monitor=1):
        """Returns (png_bytes, content_type)."""
        return self._req("POST", "/screenshot", {"monitor": monitor}, raw_response=True)

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

    def focus(self, handle):
        return self._req("POST", "/uia/focus", {"handle": handle})

    def sendkeys(self, keys):
        """SendKeys string, e.g. '{ENTER}', '^v' (Ctrl+V)."""
        return self._req("POST", "/input/sendkeys", {"keys": keys})

    def activate(self, window):
        return self._req("POST", "/window/activate", {"window": window})

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
