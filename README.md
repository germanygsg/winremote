# WinRemote

Seamless remote control for Windows machines. A persistent Python agent on the
Windows box exposes a fast HTTP API for GUI automation — no per-operation
scheduled tasks, no file polling, sub-second latency.

Built to replace a slow SSH+scheduled-task automation loop (60–90s per
operation) with direct calls (<1s).

## How it works

```
[Your machine] --Tailscale--> [Windows agent :8765]
     HTTP REST + bearer token      UIA (uiautomation) + screenshots (mss)
```

- **Text input** uses UIA `ValuePattern.SetValue()` — works even in WebView2 /
  Chromium inputs that ignore synthetic keystrokes.
- **Clicks** use UIA `InvokePattern` — no coordinate guessing.
- **Screenshots** via `mss` (~20ms).
- Auth: bearer token (generated at install, stored in `%APPDATA%\winremote`).
  Tailscale already restricts network access; the token is defense in depth.

## Layout

- `agent/winremote_agent.py` — the Windows service
- `agent/requirements.txt` — `uiautomation`, `mss`, `Pillow`
- `client/winremote.py` — thin Python client for the controlling machine
- `setup/install.bat` — one-time setup on Windows (deps, token, autostart task)

## Quick start (Windows machine)

1. Copy this repo to the Windows machine.
2. Run `setup\install.bat` as administrator.
3. Note the token in `%APPDATA%\winremote\token.txt`.
4. The agent starts at every logon and listens on port 8765.

## Updating the agent (H410M)

On the Windows machine, in any PowerShell:

```powershell
iwr https://raw.githubusercontent.com/germanygsg/winremote/main/setup/deploy-update.ps1 -OutFile $env:TEMP\wr-upd.ps1; & $env:TEMP\wr-upd.ps1
```

It downloads the latest agent, canary-starts it on port 8766 to prove it
works, then swaps it in on 8765 via the `WinRemoteAgent` scheduled task
(elevated). Safe to re-run; aborts before touching the old agent if the
canary fails.

## Quick start (controlling machine)

```python
from client.winremote import WinRemote

agent = WinRemote("100.69.141.114", token="...")
agent.health()

# Screenshot (PNG bytes)
png, _ = agent.screenshot()

# Find + fill a text field (works in WebView2)
fields = agent.find(window="MedRecPlus", name="Password", control_type="edit")
agent.set_value(fields[0]["handle"], "s3cret")

# Click a button
agent.click_button(window="MedRecPlus", name="Invoices")

# Special keys
agent.sendkeys("{ENTER}")
```

## API reference

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check (no auth) |
| POST | `/screenshot` | PNG screenshot. Body: `{"monitor": 1}` |
| POST | `/uia/tree` | UI tree. Body: `{"window": "Name", "max_depth": 5}` |
| POST | `/uia/find` | Find elements. Body: `{"window", "name", "control_type", "automation_id", "scope", "limit"}` — `name` is substring match |
| POST | `/uia/invoke` | Click. Body: `{"handle"}` |
| POST | `/uia/set_value` | Set text. Body: `{"handle", "value"}` — falls back to clipboard paste |
| POST | `/uia/focus` | Focus element. Body: `{"handle"}` |
| POST | `/input/sendkeys` | SendKeys. Body: `{"keys": "{ENTER}"}` |
| POST | `/window/activate` | Bring window forward. Body: `{"window": "Name"}` |
| GET | `/file/info?path=...` | File metadata: `{"size", "mtime", "is_dir"}` |
| GET | `/file/list?path=...` | Directory listing: `{"entries": [{"name", "is_dir", "size", "mtime"}]}` |
| GET | `/file/download?path=...&offset=0&length=1048576` | Raw bytes chunk of a file (ranged, resumable) |
| POST | `/file/upload` | Raw bytes chunk in body; headers `X-File-Path`, `X-File-Offset`, `X-File-Size` |

File transfers are chunked (1 MiB default, 8 MiB max per request) and
resumable: the client asks `/file/info` for the remote size and continues
from that offset, so a dropped connection restarts mid-file, not from zero.
Paths must be absolute. There is no delete/rename endpoint by design.

Element `handle`s expire after 5 minutes of disuse — call `/uia/find` again.

## Security notes

- Bind is `0.0.0.0:8765` so Tailscale peers can reach it; the bearer token is
  required for everything except `/health`.
- Never commit a token. The install script generates one locally.
- The agent runs elevated (highest privileges) so it can drive admin windows.
  It cannot touch the secure desktop (UAC prompts, lock screen) — no software can.

## License

MIT
