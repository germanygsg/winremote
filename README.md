# WinRemote

Seamless remote control for Windows machines. A persistent Python agent on the
Windows box exposes a fast HTTP API for GUI automation, shell access, file
transfer, and system control — no per-operation scheduled tasks, no file
polling, sub-second latency.

Built to replace a slow SSH+scheduled-task automation loop (60–90s per
operation) with direct calls (<1s). It has since grown into a full SSH
replacement (persistent shell sessions, port forwarding) and an RDP-class
remote control surface (screenshots, mouse/keyboard, clipboard, UIA
automation) — all agent-driven, no human looking at the screen.

## How it works

```
[Your machine] --Tailscale--> [Windows agent :8765]
     HTTP REST + bearer token      UIA (uiautomation) + screenshots (mss)
                                   + PowerShell + Win32 APIs (ctypes)
```

- **Text input** — three methods, pick per situation (see "Text input" below):
  UIA `ValuePattern.SetValue()` (works in WebView2/Chromium inputs that ignore
  synthetic keystrokes), `SendInput` Unicode (real key events), or
  clipboard-paste (for large text).
- **Clicks** — UIA `InvokePattern` for real buttons; real mouse clicks at
  coordinates for anything UIA can't invoke.
- **Screenshots** via `mss` (~20ms).
- **Auth** — bearer token (generated at install, stored in
  `%APPDATA%\winremote\token.txt`). Tailscale already restricts network
  access; the token is defense in depth.
- The agent runs **elevated** (highest privileges) so it can drive admin
  windows. It runs in the user's interactive session.

## Layout

- `agent/winremote_agent.py` — the Windows agent (single file, stdlib +
  `uiautomation`/`mss`/`Pillow`)
- `agent/requirements.txt` — `uiautomation`, `mss`, `Pillow`
- `agent/winremote_system.py` — companion SYSTEM service for lock-screen
  *detection* (lock-state only; lock-screen *interaction* is intentionally
  not implemented)
- `client/winremote.py` — thin Python client for the controlling machine
- `setup/install.bat` — one-time setup on Windows (deps, token, autostart task)
- `setup/deploy-update.ps1` — manual update script (fallback; the agent
  self-updates automatically)

## Quick start (Windows machine)

1. Copy this repo to the Windows machine.
2. Run `setup\install.bat` as administrator.
3. Note the token in `%APPDATA%\winremote\token.txt`.
4. The agent starts at every logon via the `WinRemoteAgent` scheduled task
   and listens on port 8765. Open the Windows Firewall for 8765 if needed.

## Quick start (controlling machine)

```python
from client.winremote import WinRemote

agent = WinRemote("100.69.141.114", token="...")
agent.health()

# Screenshot (PNG bytes)
png = agent.screenshot()

# Find + fill a text field (works in WebView2)
fields = agent.find(window="MedRecPlus", name="Password", control_type="edit")
agent.set_value(fields[0]["handle"], "s3cret")

# Click a button
agent.click_button(window="MedRecPlus", name="Invoices")

# Special keys
agent.sendkeys("{ENTER}")

# Persistent shell session (like SSH)
s = agent.session_create()["session_id"]
agent.session_exec(s, "cd C:\\temp")
agent.session_exec(s, "$x = 42; $x * 2")   # -> 84, cwd/env persist

# Clipboard
agent.clipboard_set("hello")
print(agent.clipboard_get())   # -> {"ok": True, "text": "hello"}

# Port forward (SSH -L equivalent)
f = agent.forward_add(listen_port=8080, target_port=3000)
# ... use localhost:8080 on the Windows box ...
agent.forward_remove(f["forward_id"])
```

## API reference

### Health & updates

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check (no auth). Returns `image_hash` (running code identity) and update status |
| POST | `/update/check` | Check GitHub for a new agent version now; canary-tests and restarts into it |

### Screenshots

| Method | Path | Description |
|---|---|---|
| POST | `/screenshot` | PNG screenshot. Body: `{"monitor": 1}` |

### UIA automation

UIA element `handle`s expire after 5 minutes of disuse — call `/uia/find` again.

| Method | Path | Description |
|---|---|---|
| POST | `/uia/tree` | UI tree. Body: `{"window": "Name", "max_depth": 5}` |
| POST | `/uia/find` | Find elements. Body: `{"window", "name", "control_type", "automation_id", "scope", "limit"}` — `name` is substring match |
| POST | `/uia/invoke` | Click via InvokePattern. Body: `{"handle"}` |
| POST | `/uia/set_value` | Set text via ValuePattern. Body: `{"handle", "value"}` — falls back to clipboard paste |
| POST | `/uia/set_value_at` | Set text on the element at screen coordinates (bypasses `/uia/find`). Body: `{"x", "y", "value"}` |
| POST | `/uia/focus` | Focus element. Body: `{"handle"}` |
| POST | `/uia/expand` | Expand/collapse/toggle a dropdown via ExpandCollapsePattern. Body: `{"handle", "action": "expand"\|"collapse"\|"toggle"}` |
| POST | `/uia/select` | Select an option via SelectionItemPattern. Body: `{"handle"}` (handle of the option) |
| POST | `/uia/click_at` | Real mouse click at coordinates (for elements UIA can't invoke). Body: `{"x", "y"}` |
| POST | `/uia/double_click_at` | Real double-click (single request — two separate calls are too slow for the OS double-click window). Body: `{"x", "y"}` |
| POST | `/uia/right_click_at` | Real right-click at coordinates. Body: `{"x", "y"}` |

### Mouse & keyboard

| Method | Path | Description |
|---|---|---|
| POST | `/input/sendkeys` | SendKeys string. Body: `{"keys": "{ENTER}"}` or `"^v"` (Ctrl+V) |
| POST | `/input/type_text` | Type text via ctypes `SendInput` with `KEYEVENTF_UNICODE`. Real Unicode key events — WebView2/React accept them. Body: `{"text"}` |
| POST | `/input/press_key` | Press a special key via ctypes `SendInput` (no PowerShell). Body: `{"key": "enter"\|"tab"\|"escape"\|"backspace"\|"delete"\|"ctrl_a"\|"ctrl_c"\|"ctrl_v"}` |
| POST | `/input/paste_text` | Paste text at current focus via clipboard. Caller must click to focus first. Body: `{"text"}` or `{"from_file": "path"}` |
| POST | `/input/mouse_move` | Move cursor without clicking. Body: `{"x", "y"}` |
| POST | `/input/mouse_down` | Press a mouse button. Body: `{"button": "left"\|"right"\|"middle"}` |
| POST | `/input/mouse_up` | Release a mouse button. Body: `{"button": "left"\|"right"\|"middle"}` |
| POST | `/input/drag` | Single-gesture drag (press, interpolated move, release). Body: `{"x1", "y1", "x2", "y2", "button": "left", "steps": 10}` |

### Clipboard

| Method | Path | Description |
|---|---|---|
| POST | `/clipboard/get` | Read text from the Windows clipboard. Returns `{"ok", "text"}` (`null` if no text) |
| POST | `/clipboard/set` | Write text to the clipboard. Body: `{"text"}` |

### JavaScript execution

| Method | Path | Description |
|---|---|---|
| POST | `/js/execute` | Execute JavaScript in a WebView2 via COM `ExecuteScriptAsync`. 100% reliable DOM access. Body: `{"script", "window": "MedRecPlus"}`. Returns `{"ok", "result"}` |
| POST | `/js/eval` | Execute JS via the Tauri app's local eval server (`127.0.0.1:48721`). Body: `{"script"}` |

### Shell

| Method | Path | Description |
|---|---|---|
| POST | `/shell/exec` | Run a PowerShell command, capture output. Body: `{"command", "timeout": 30, "bitness": "64"\|"32"}`. Returns `{"ok", "stdout", "stderr", "exit_code"}` |
| POST | `/shell/session/create` | Create a persistent PowerShell session (cwd/env/functions persist like SSH). Body: `{"bitness": "64"\|"32"}`. Returns `{"ok", "session_id"}` |
| POST | `/shell/session/exec` | Run a command in a session. Body: `{"session_id", "command", "timeout": 30}` |
| POST | `/shell/session/close` | Close a session. Body: `{"session_id"}` |
| GET/POST | `/shell/session/list` | List open sessions |

### Registry

| Method | Path | Description |
|---|---|---|
| POST | `/registry/get` | Read a registry value. Body: `{"path": "HKLM:\\SOFTWARE\\..."}`. Returns `{"ok", "value"}` or `{"ok", "values": {...}}` for a key |
| POST | `/registry/set` | Write a registry value. Body: `{"path", "value", "type": "SZ"\|"DWORD"\|...}` |

### Processes

| Method | Path | Description |
|---|---|---|
| POST | `/proc/start` | Start a GUI process in the agent's interactive session (visible windows). Body: `{"path", "args": [], "cwd": null}`. Returns `{"ok", "pid"}` |
| GET | `/proc/list` | List processes (tasklist) |

### Files

| Method | Path | Description |
|---|---|---|
| GET | `/file/info?path=...` | File metadata: `{"size", "mtime", "is_dir"}` |
| GET | `/file/list?path=...` | Directory listing: `{"entries": [{"name", "is_dir", "size", "mtime"}]}` |
| GET | `/file/download?path=...&offset=0&length=1048576` | Raw bytes chunk of a file (ranged, resumable) |
| POST | `/file/upload` | Raw bytes chunk in body; headers `X-File-Path`, `X-File-Offset`, `X-File-Size` |

File transfers are chunked (1 MiB default, 8 MiB max per request) and
resumable: the client asks `/file/info` for the remote size and continues
from that offset, so a dropped connection restarts mid-file, not from zero.
Paths must be absolute. There is no delete/rename endpoint by design.

### Network forwarding (SSH `-L` equivalent)

| Method | Path | Description |
|---|---|---|
| POST | `/net/forward/add` | Listen on `0.0.0.0:listen_port` and forward to `target_host:target_port`. Body: `{"listen_port" (0=auto), "target_host": "127.0.0.1", "target_port"}`. Returns `{"ok", "forward_id", "listen_port"}` |
| POST | `/net/forward/remove` | Remove a forward. Body: `{"forward_id"}` |
| GET/POST | `/net/forward/list` | List active forwards |

Forwards are in-memory and lost on agent restart. The listener binds
`0.0.0.0` — access control relies on host firewall plus the API bearer token.

### Lock screen

| Method | Path | Description |
|---|---|---|
| GET | `/system/lock/state` | Whether the workstation is locked. Returns `{"ok", "locked": bool}` |
| GET | `/system/lock/screenshot` | PNG of the lock screen (via the SYSTEM helper) |
| POST | `/system/lock/type` | *Not implemented* — lock-screen input is intentionally unavailable (see Security notes) |
| POST | `/system/lock/unlock` | *Not implemented* — see Security notes |

### Windows

| Method | Path | Description |
|---|---|---|
| POST | `/window/activate` | Bring a window to the foreground. Body: `{"window": "Name"}` |

## Text input: which method to use

Three mechanisms, different tradeoffs:

1. **`/uia/set_value`** (UIA ValuePattern) — best for form fields in native
   and WebView2/Chromium apps. Goes through the accessibility API, so the
   app sees a proper value change. Falls back to clipboard-paste if the
   element doesn't support ValuePattern.
2. **`/input/type_text`** (SendInput Unicode) — best when you need the app
   to see real keystrokes (React inputs, games, apps that validate per-key).
   Sends genuine `KEYEVENTF_UNICODE` events via ctypes — no PowerShell.
3. **`/input/paste_text`** (clipboard) — best for large text. Click to focus
   the field first (real click), then paste. Accepts inline `text` or
   `from_file`.

For special keys (Enter, Tab, Ctrl+C, ...): `/input/press_key` (ctypes,
reliable) or `/input/sendkeys` (SendKeys syntax, e.g. `"{ENTER}"`, `"^v"`).

## Auto-update

The agent self-updates: every 10 minutes it compares its own file hash
against `main` on GitHub. On change it compile-checks the new file,
canary-starts it on port 8766 (must answer `/health`), then re-execs
into it. A failed canary keeps the old version — a bad push can't brick
it. `/health` reports update status plus `image_hash` (the sha256 of the
actually-running code, so you can tell if the process is stale).
`POST /update/check` (auth) forces a check now (applies immediately, even
if busy). Disable with `--no-auto-update`, `WINREMOTE_AUTO_UPDATE=0`, or
`--update-interval 0`. Tune the idle gate with `--update-idle` /
`WINREMOTE_UPDATE_IDLE` (seconds of no requests before an update applies,
default 180).

Robustness details (learned the hard way):

- **CDN cache-buster**: `raw.githubusercontent.com` sits behind a CDN that
  can serve the pre-push file for minutes after a push. Update checks append
  `?t=<epoch>` so a fresh push is never misread as "already up to date".
- **Stale-image detection**: the running process captures its own file hash
  at startup. If the on-disk file is newer than the loaded image (file was
  replaced but the restart never took effect — port race, zombie old
  process), the next check restarts into the current file instead of
  falsely reporting "up to date".
- **Circuit breaker**: after 3 failed stale-image auto-restarts, the agent
  stops trying and reports "manual restart required" instead of looping
  forever.

Manual fallback (if auto-update ever gets stuck):

```powershell
iwr https://raw.githubusercontent.com/germanygsg/winremote/main/setup/deploy-update.ps1 -OutFile $env:TEMP\wr-upd.ps1; & $env:TEMP\wr-upd.ps1
```

Tradeoff to know: the agent runs elevated and executes code fetched over
HTTPS from this repo. Fine for your own repo; if the GitHub account were
compromised that would be remote code execution on the machine.

## SSH parity

What `ssh` can do that WinRemote covers:

| SSH feature | WinRemote equivalent |
|---|---|
| Run a command | `/shell/exec` |
| 32-bit execution | `bitness: "32"` on `/shell/exec` and `/shell/session/create` |
| Persistent session (cd, env persist) | `/shell/session/create|exec|close|list` |
| Local port forwarding (`ssh -L`) | `/net/forward/add|remove|list` |
| File transfer (`scp`) | Chunked resumable `/file/upload|download` |
| Environment control | Sessions keep env vars across calls |

Known gaps vs SSH: no full PTY (ConPTY) — the persistent sessions cover
everything automation needs (cwd/env persistence, streaming output,
long-running commands, killing hung processes); a real PTY would only add
interactive TTY-only programs (vim, password prompts), which don't fit an
agent-driven workflow. No `ssh -R` (remote forwarding), no `ssh -D`
(SOCKS), no agent forwarding. Sessions and port forwards are in-memory
and lost on agent restart.

## RDP parity

What RDP gives a human operator that WinRemote gives an agent:

| RDP capability | WinRemote equivalent |
|---|---|
| See the screen | `/screenshot` (on demand, ~20ms) |
| Click / double-click / right-click / drag | `/uia/click_at`, `/uia/double_click_at`, `/uia/right_click_at`, `/input/drag`, `/input/mouse_move|down|up` |
| Type | `/input/type_text`, `/input/paste_text`, `/input/press_key`, `/input/sendkeys` |
| Clipboard sync | `/clipboard/get`, `/clipboard/set` |
| Run programs (visible) | `/proc/start` (launches in the interactive session) |
| Command line | `/shell/exec` + persistent sessions |
| File transfer | Chunked resumable `/file/*` |
| Element-level control (better than RDP) | Full UIA tree/find/invoke/set_value — no coordinate guessing |

Known gaps vs RDP: cannot unlock a locked workstation or dismiss UAC
prompts (the secure desktop is blocked for user-mode software by Windows
design — same limitation as any non-driver remote tool without a kernel
component). No audio redirection. `/system/lock/state` reports whether
the machine is locked so callers can fail gracefully instead of sending
input into the void.

## Security notes

- Bind is `0.0.0.0:8765` so Tailscale peers can reach it; the bearer token
  is required for everything except `/health`.
- Never commit a token. The install script generates one locally.
- The agent runs elevated (highest privileges) so it can drive admin
  windows. It cannot touch the secure desktop (UAC prompts, lock screen)
  — a Windows security boundary for user-mode software. Tools that *can*
  (e.g. some commercial remote-desktop products) do it via a SYSTEM
  service or kernel driver; WinRemote deliberately does not implement
  lock-screen input.
- Port forwards bind `0.0.0.0` — keep the host firewall tight; the API
  token is the only gate on who can open a forward.

## Known limitations

- **Secure desktop**: no interaction with UAC prompts or the lock screen.
  If the machine is locked, GUI automation can't proceed — check
  `/system/lock/state` first.
- **Keystroke delivery**: `/input/type_text` and `/input/press_key` return
  success when Windows accepts the `SendInput` structures, which proves
  delivery to the input queue but not that the target app processed them.
  Verify visually (screenshot) for critical input.
- **UIA in WebView2**: `ValuePattern` for text fields can be flaky;
  prefer `/js/execute` for WebView2 DOM work or `/input/type_text` after
  clicking to focus.
- **32-bit subsystem**: `/shell/exec` with `bitness: "32"` needs a healthy
  WOW64 subsystem; on machines with a damaged one it fails with
  `0xC0000135`.
- **In-memory state**: shell sessions and port forwards vanish on agent
  restart. The auto-updater restarts the agent on new versions.

## License

MIT
