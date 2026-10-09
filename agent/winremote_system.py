#!/usr/bin/env python3
"""
WinRemote System Service - Lock screen handler.

Runs as SYSTEM (via scheduled task WinRemoteSystem). Companion to
winremote_agent.py which runs in the user's interactive session.

A user-mode app cannot touch the Winlogon secure desktop (Windows
blocks it by design). A SYSTEM service can: it opens the Winlogon
desktop, switches its thread to it, captures the screen, and injects
input. This is the same privilege level StarDeskService uses.

Listens on 127.0.0.1:8767 (localhost only). The main agent proxies
via /system/* endpoints (it holds the bearer token).

Endpoints:
- GET  /health
- GET  /system/lock/state       -> {locked: bool}
- GET  /system/lock/screenshot  -> PNG bytes
- POST /system/lock/type   {text} -> type text on lock screen
- POST /system/lock/unlock {pin}  -> type PIN + Enter

Usage:
    python winremote_system.py [--port 8767]

    Token via WINREMOTE_TOKEN env var, or --token. If none given,
    auth is disabled (localhost-only is the gate).
"""

import argparse
import base64
import ctypes
import io
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

# --- Windows API setup -------------------------------------------------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

# Desktop access rights
DESKTOP_READOBJECTS = 0x0001
DESKTOP_ENUMERATE = 0x0040

# UOI_NAME = 2
UOI_NAME = 2

user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.OpenDesktopW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenDesktopW.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.GetThreadDesktop.restype = wintypes.HANDLE
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.SetThreadDesktop.restype = wintypes.BOOL
user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE, wintypes.INT, wintypes.LPVOID,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
user32.GetUserObjectInformationW.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [wintypes.INT]
user32.GetSystemMetrics.restype = wintypes.INT
user32.GetDesktopWindow.argtypes = []
user32.GetDesktopWindow.restype = wintypes.HWND
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype = wintypes.INT

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, wintypes.INT, wintypes.INT]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [
    wintypes.HDC, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT,
    wintypes.HDC, wintypes.INT, wintypes.INT, wintypes.DWORD]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    wintypes.LPVOID, ctypes.c_void_p, wintypes.UINT]
gdi32.GetDIBits.restype = wintypes.INT
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL

# SendInput structures (40 bytes, fixed in winremote 785a812)
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG))]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), wintypes.INT]
user32.SendInput.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = wintypes.SHORT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_RETURN = 0x0D
SM_CXSCREEN = 0
SM_CYSCREEN = 1


# --- Lock screen operations --------------------------------------------

def _open_winlogon_desktop():
    """Open the Winlogon (secure) desktop. Returns handle or None."""
    return user32.OpenDesktopW(
        "Winlogon", 0, False,
        DESKTOP_READOBJECTS | DESKTOP_ENUMERATE)


def is_locked():
    """True if the workstation is locked (input desktop is Winlogon)."""
    h = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not h:
        return True
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        ok = user32.GetUserObjectInformationW(
            h, UOI_NAME, buf, 256, ctypes.byref(needed))
        if not ok:
            return True
        return buf.value.lower() != "default"
    finally:
        user32.CloseDesktop(h)


class _DesktopSwitcher:
    """Context manager: switch current thread to the Winlogon desktop."""

    def __init__(self):
        self.h_winlogon = None
        self.h_old = None

    def __enter__(self):
        self.h_winlogon = _open_winlogon_desktop()
        if not self.h_winlogon:
            raise RuntimeError("cannot open Winlogon desktop")
        tid = kernel32.GetCurrentThreadId()
        self.h_old = user32.GetThreadDesktop(tid)
        if not user32.SetThreadDesktop(self.h_winlogon):
            user32.CloseDesktop(self.h_winlogon)
            self.h_winlogon = None
            raise RuntimeError("SetThreadDesktop failed")
        return self

    def __exit__(self, *exc):
        if self.h_old:
            user32.SetThreadDesktop(self.h_old)
        if self.h_winlogon:
            user32.CloseDesktop(self.h_winlogon)


def capture_lock_screen():
    """Screenshot the lock screen. Returns PNG bytes or None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with _DesktopSwitcher():
            w = user32.GetSystemMetrics(SM_CXSCREEN)
            h = user32.GetSystemMetrics(SM_CYSCREEN)
            hwnd = user32.GetDesktopWindow()
            hdc_screen = user32.GetWindowDC(hwnd)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
            gdi32.SelectObject(hdc_mem, hbmp)
            # SRCCOPY = 0x00CC0020
            ok = gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, 0, 0, 0x00CC0020)
            user32.ReleaseDC(hwnd, hdc_screen)
            if not ok:
                gdi32.DeleteDC(hdc_mem)
                gdi32.DeleteObject(hbmp)
                return None

            # BITMAPINFOHEADER for 32-bit
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD),
                            ("biWidth", wintypes.LONG),
                            ("biHeight", wintypes.LONG),
                            ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD),
                            ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD),
                            ("biXPelsPerMeter", wintypes.LONG),
                            ("biYPelsPerMeter", wintypes.LONG),
                            ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB
            buf_len = w * h * 4
            buf = (ctypes.c_ubyte * buf_len)()
            lines = gdi32.GetDIBits(hdc_mem, hbmp, 0, h,
                                    buf, ctypes.byref(bmi), 0)
            gdi32.DeleteDC(hdc_mem)
            gdi32.DeleteObject(hbmp)
            if not lines:
                return None
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1)
            out = io.BytesIO()
            img.convert("RGB").save(out, format="PNG")
            return out.getvalue()
    except Exception:
        return None


def _send_unicode_char(ch):
    """Send a single Unicode character via SendInput."""
    inp_down = INPUT()
    inp_down.type = INPUT_KEYBOARD
    inp_down.u.ki.wVk = 0
    inp_down.u.ki.wScan = ord(ch)
    inp_down.u.ki.dwFlags = KEYEVENTF_UNICODE
    inp_down.u.ki.time = 0
    inp_down.u.ki.dwExtraInfo = None
    inp_up = INPUT()
    inp_up.type = INPUT_KEYBOARD
    inp_up.u.ki.wVk = 0
    inp_up.u.ki.wScan = ord(ch)
    inp_up.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
    inp_up.u.ki.time = 0
    inp_up.u.ki.dwExtraInfo = None
    user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
    user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))


def _press_enter():
    inp_down = INPUT()
    inp_down.type = INPUT_KEYBOARD
    inp_down.u.ki.wVk = VK_RETURN
    inp_down.u.ki.wScan = user32.MapVirtualKeyW(VK_RETURN, 0)
    inp_down.u.ki.dwFlags = 0
    inp_up = INPUT()
    inp_up.type = INPUT_KEYBOARD
    inp_up.u.ki.wVk = VK_RETURN
    inp_up.u.ki.wScan = inp_down.u.ki.wScan
    inp_up.u.ki.dwFlags = KEYEVENTF_KEYUP
    user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
    user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))


def type_on_lock_screen(text, press_enter=False):
    """Type text on the lock screen. Must be called from a fresh thread
    (SetThreadDesktop fails if the thread has windows)."""
    import time as _time
    result = {}

    def _worker():
        try:
            with _DesktopSwitcher():
                _time.sleep(0.3)
                for ch in text:
                    _send_unicode_char(ch)
                    _time.sleep(0.02)
                if press_enter:
                    _time.sleep(0.3)
                    _press_enter()
                result["ok"] = True
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"

    # Run on a dedicated thread: SetThreadDesktop requires a thread
    # with no windows or hooks.
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=30)
    if "ok" not in result:
        raise RuntimeError(result.get("error", "timed out"))
    return True


# --- HTTP server --------------------------------------------------------

_TOKEN = os.environ.get("WINREMOTE_TOKEN", "")

class _Handler(BaseHTTPRequestHandler):
    server_version = "WinRemoteSystem/1.0"

    def _auth(self):
        if not _TOKEN:
            return True  # localhost-only is the gate
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {_TOKEN}"

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, {"ok": True, "service": "winremote-system"})
            return
        if not self._auth():
            self._send(401, {"error": "unauthorized"})
            return
        if path == "/system/lock/state":
            self._send(200, {"ok": True, "locked": is_locked()})
        elif path == "/system/lock/screenshot":
            png = capture_lock_screen()
            if png:
                self._send(200, raw=png, ctype="image/png")
            else:
                self._send(500, {"error": "capture failed"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if not self._auth():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        body = {}
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
        if path == "/system/lock/type":
            text = body.get("text", "")
            if not text:
                self._send(400, {"error": "text required"})
                return
            try:
                type_on_lock_screen(text, press_enter=False)
                self._send(200, {"ok": True})
            except Exception as e:
                self._send(500, {"error": str(e)})
        elif path == "/system/lock/unlock":
            pin = body.get("pin", "")
            if not pin:
                self._send(400, {"error": "pin required"})
                return
            try:
                type_on_lock_screen(pin, press_enter=True)
                self._send(200, {"ok": True})
            except Exception as e:
                self._send(500, {"error": str(e)})
        else:
            self._send(404, {"error": "not found"})


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global _TOKEN
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8767)
    ap.add_argument("--token", default="")
    args = ap.parse_args()
    if args.token:
        _TOKEN = args.token
    if not _TOKEN:
        # Try the shared token file (written by installer, ACL'd to
        # SYSTEM + Administrators).
        for p in (r"C:\ProgramData\WinRemote\token.txt",
                  os.path.join(os.environ.get("APPDATA", ""),
                               "winremote", "token.txt")):
            try:
                with open(p) as f:
                    _TOKEN = f.read().strip()
                    if _TOKEN:
                        break
            except Exception:
                pass
    srv = _Server(("127.0.0.1", args.port), _Handler)
    print(f"winremote-system on 127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
