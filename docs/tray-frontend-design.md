# WinRemote Tray Frontend — Design

Status: **draft for review** (2026-11-02). No code yet.
Goal: a lightweight local UI for the WinRemote agent — status, settings,
permissions, and clean termination — without giving up the agent's
single-file, zero-extra-dependency nature.

---

## 1. Problem

The agent (`agent/winremote_agent.py`, ~2,100 lines) runs invisibly:
started at logon by the `WinRemoteAgent` scheduled task via
`setup/winremote-launch.ps1` (which loads the token from
`%APPDATA%\winremote\token.txt` into `WINREMOTE_TOKEN`), listening on
`0.0.0.0:8765`. Today, the local user cannot:

- see whether the agent is running, which build it is running
  (`image_hash` exists in `/health` but nothing local surfaces it),
- change anything without editing the launcher / env vars,
- restrict what the token holder may do,
- stop it cleanly (killing the process fights the task + updater).

## 2. Hard constraints

1. **Lightweight, zero new dependencies.** No pystray, no Electron/Tauri,
   no web UI. Tray = raw Win32 via `ctypes` (`Shell_NotifyIconW`, popup
   menu, hidden message window). Windows = `tkinter`/`ttk` (stdlib).
   The icon is a pre-generated asset embedded as base64 (§6) — no file
   I/O, no runtime drawing except the state dot (Pillow, already a
   dependency).
2. **Single file stays single file.** The self-updater downloads exactly
   one file (`UPDATE_URL` → `agent/winremote_agent.py`), canary-tests it
   on port 8766, and re-execs into it. Splitting the tray into a second
   module would silently break updates. **Therefore the tray code lives
   inside `winremote_agent.py`** in its own clearly fenced section.
   Estimated +600–700 lines total (tray ~400, settings ~120,
   permission gate ~50, glue ~30).
3. **Failure isolation.** Any exception in tray/UI code must never take
   down the HTTP server. Tray init is wrapped; on failure the agent logs
   and continues headless, exactly as today.
4. **Permissions are a local-only control.** The point of permissions is
   letting the person at the machine constrain remote callers. A remote
   caller (even with the token — the token already grants shell) must
   **never** be able to change permissions or un-pause. All writes happen
   in the local UI; the remote API is read-only for settings.
5. **Manual verification only**, per standing practice: every behaviour
   below gets a live check on H410M before it is called done.

## 3. Architecture

```
WinRemoteAgent task (logon, germa, highest)
  └─ winremote-launch.ps1  (token.txt → WINREMOTE_TOKEN)
       └─ pythonw winremote_agent.py            ← single process
            ├─ ThreadedHTTPServer :8765          (unchanged)
            ├─ _update_loop thread               (unchanged)
            ├─ tray thread (NEW) — Win32 msg loop, owns the icon
            └─ UI thread (NEW, on demand) — one tkinter window at a time

%APPDATA%\winremote\
  ├─ token.txt        (unchanged, launcher-owned)
  └─ settings.json    (NEW, tray-owned — see §4)
```

- The tray starts **after** the HTTP server binds successfully (a failed
  bind exits as today, no orphan icon). Started from `main()` unless
  `--no-tray` is passed.
- The canary instance (`_canary_ok` launches the candidate on :8766)
  **must** pass `--no-tray`, otherwise every update test would flash a
  second icon. Same for any future test instance.
- `winremote_system.py` (SYSTEM helper, :8767) gets **no UI**. The agent
  only displays its health (polled when the Status tab opens — no
  background polling anywhere).
- Explorer restart: the tray thread registers the `TaskbarCreated`
  broadcast and re-adds the icon. Session lock is fine — the icon
  persists; windows simply can't open on the secure desktop.

## 4. Settings file

`%APPDATA%\winremote\settings.json`, written atomically (temp + replace)
by the tray, read by the agent at startup and re-read on tray request
(in-process call, no watcher/polling).

```json
{
  "version": 1,
  "port": 8765,
  "auto_update": true,
  "update_interval_s": 600,
  "update_idle_s": 180,
  "permissions": {
    "paused": false,
    "categories": {
      "shell": true, "files": true, "uia_input": true,
      "clipboard": true, "js": true, "registry": true,
      "proc": true, "net": true, "system": true
    }
  }
}
```

Rules:

- **Precedence:** CLI args > `settings.json` > env vars > built-in
  defaults. The launcher flow keeps working untouched; the file only
  exists once the user saves from the UI.
- **Token is never in this file.** It stays in `token.txt`, owned by the
  launcher/installer.
- **Hot-applied keys:** `auto_update`, `update_interval_s`,
  `update_idle_s`, all of `permissions` — applied immediately on save
  (they map onto the existing `_update_state` dict + the new permission
  state).
- **Restart-required keys:** `port`. The Settings tab says so inline and
  enables the *Save & restart* button (reuses the updater's proven
  spawn-detached-then-exit restart pattern).
- A corrupt/unreadable file is ignored with a console warning — defaults
  win, the agent must always start.

## 5. Permissions

Enforcement point: one gate function called in `do_GET`/`do_POST`
immediately after `_auth()` succeeds and before any dispatch:

```python
cat = _route_category(path)          # None for exempt routes
if cat is not None:
    if _perm_state["paused"]:
        self._send(403, {"error": "paused by local user"}); return
    if not _perm_state["categories"].get(cat, True):
        self._send(403, {"error": f"category disabled: {cat}"}); return
```

Route → category map (from the live route table):

| Category | Routes |
|---|---|
| `shell` | `/shell/exec`, `/shell/session/*` |
| `files` | `/file/info`, `/file/list`, `/file/download`, `/file/upload` |
| `uia_input` | `/screenshot`, `/uia/*`, `/input/*`, `/window/activate` |
| `clipboard` | `/clipboard/get`, `/clipboard/set` |
| `js` | `/js/eval`, `/js/execute` |
| `registry` | `/registry/get`, `/registry/set` |
| `proc` | `/proc/list`, `/proc/start` |
| `net` | `/net/forward/*` |
| `system` | `/system/lock/*` |
| *(exempt)* | `/health` (always, unauthenticated as today), `/update/check` (agent lifecycle, not a capability) |

- `/health` gains two read-only fields so remote clients can see the
  state and back off instead of retrying blindly:
  `"paused": bool`, `"disabled_categories": [...]`.
- New `GET /settings` (auth required) returns the *effective* settings
  — diagnostics only. There is deliberately **no** POST/write endpoint
  (constraint 4). The tray, being in-process, reads/writes state
  directly and never uses HTTP for this.

## 6. Tray icon & menu

- **Icon:** a real generated asset — **the flat TV-remote handset,
  chosen by boss 2026-11-02** (canonical files: `assets/winremote.ico` /
  `assets/winremote.png`; monitor and cursor candidates kept in the
  same folder as alternates). Corners made
  transparent, multi-size 16–256. The updater still must not fetch a
  second file, so at build time the 16 px and 32 px PNG renders are
  embedded as base64 in the agent (~a few KB each) and loaded from
  memory; the full `.ico` in the repo is the asset of record. Because
  the artwork is full-colour, **state is shown as a small coloured dot
  overlaid on the icon's lower-right corner** at runtime (Pillow draws
  it): green — running, amber — paused/update pending, red — server
  error. The original "recolour the glyph" plan is dropped.
- **Tooltip:** `WinRemote · <tailscale-ip>:8765 · <image_hash[:7]> · Running|Paused`
- **Menu** (right-click; double-click opens Status):
  - `Status…`
  - `Settings…`
  - `Permissions…`
  - `Pause remote control` / `Resume remote control` (label flips)
  - `Check for update now` (calls the existing `update_now(force=True)`)
  - `Restart agent`
  - `Quit`
  - `Quit && disable autostart` — also disables the `WinRemoteAgent`
    task via `schtasks /change /disable`; a checkbox in the confirm
    dialog offers the same for `WinRemoteSystem`.
- No background timers in the tray: menu labels, icon colour, and
  tooltip are refreshed when the menu is opened (`WM_INITMENUPOPUP`)
  or after an action — never by polling.

## 7. The window (one window, three tabs)

A single `ttk.Notebook` window, created on demand in its own thread,
destroyed on close; only one instance at a time (re-clicking a menu item
focuses the existing window). All data is read at open time.

**Status tab**
- State (Running/Paused), bind addresses (host name, LAN IP, Tailscale
  IP), port, uptime (from a new `_start_time`), image hash + "on-disk
  file differs — restart pending" indicator when `_LOADED_HASH` ≠ disk
  hash (the stale-image case the updater fix exposed).
- Update block: auto on/off, last check, last result (existing
  `_update_state`), *Check now* button.
- Live counts: shell sessions open, port forwards active (the in-memory
  registries already exist).
- SYSTEM helper: `GET 127.0.0.1:8767/health`, 1 s timeout → Running / Not
  running.
- Token: masked (`abcd…wxyz`), *Copy* button, *Open config folder*
  button.

**Settings tab**
- Port (number, marks "restart required"), auto-update checkbox,
  update interval + idle seconds (spinners), *Save* / *Save & restart*.
- "Start at logon" checkbox reflecting the real `WinRemoteAgent` task
  state (`schtasks /query`), toggling enables/disables the task.
- *Regenerate token* — writes a new `token.txt`, requires restart,
  with a blunt warning: **this breaks every configured client
  (including Muse's VM copy) until they are given the new token.**

**Permissions tab**
- Master *Pause all remote control* switch at top.
- One checkbox per category from §5, with route counts as hints.
- Footer note: "Changes apply immediately. They can only be changed
  here, on this machine — not over the API."

## 8. Lifecycle actions

- **Quit:** confirm if sessions/forwards are active (list counts).
  Then: `server.shutdown()` on a side thread, close sessions/forwards,
  `NIM_DELETE` the icon, `os._exit(0)` (same exit style the updater's
  restart already uses — proven on this codebase).
- **Restart agent:** spawn the detached successor with the same argv
  (the updater's `_restart_into_new` approach), remove icon, exit.
- **Quit & disable autostart:** `schtasks /change /tn WinRemoteAgent
  /disable` first (verify exit code, report failure instead of quitting
  silently), then Quit. The SYSTEM task is only touched if the user
  checks the box.
- **Pause:** flips `permissions.paused`, persists, icon turns amber,
  menu label flips. The HTTP server keeps running — `/health` must stay
  answerable so clients can see *why* they're rejected.

## 9. Changes to existing code (inventory)

| Where | Change |
|---|---|
| `winremote_agent.py` header | `--no-tray` in usage |
| `_canary_ok()` | pass `--no-tray` to the candidate process |
| `main()` | load `settings.json` after arg parsing (precedence §4); start tray thread after bind unless `--no-tray`; record `_start_time` |
| `Handler.do_GET/do_POST` | permission gate after `_auth()`; `/health` gains `paused` + `disabled_categories`; new read-only `GET /settings` |
| New section in same file | settings load/save (~120 lines), permission state + route map (~50), tray thread + menu (~250), tkinter window (~250) |
| `setup/winremote-launch.ps1` | unchanged |
| `client/winremote.py` | optional convenience: `settings()` getter. No write methods — by design |
| `README.md` | document tray, settings file, permissions at build time |

## 10. Verification plan (manual, on H410M)

1. Fresh start via task → icon appears, tooltip correct.
2. Status tab values match `/health` from the VM client.
3. Pause from tray → VM client call returns 403 "paused by local
   user"; `/health` shows `paused: true`; Resume restores access.
4. Disable `shell` only → `/shell/exec` 403s, `/screenshot` still works.
5. Change a hot key (idle seconds) → applies without restart; change
   port → prompt → *Save & restart* → agent answers on the new port.
6. Kill Explorer / restart it → icon comes back (TaskbarCreated).
7. Self-update with tray running → update applies, exactly one icon
   afterwards; canary never shows an icon.
8. Quit with an open session → warning shown; Quit → process and icon
   gone, port closed.
9. Quit & disable autostart → relogon → agent does not start; task
   shows Disabled. Re-enable from Task Scheduler (document the manual
   path back, since the tray is gone too).
10. Corrupt `settings.json` → agent still starts on defaults.

## 11. Risks & mitigations

- **Updater breaks if tray is a separate file** → mitigated by
  constraint 2 (everything in `winremote_agent.py`).
- **tkinter thread quirks** — Tk must own its thread; we create/destroy
  per open and never touch Tk objects from other threads. All agent
  state reads from the UI go through plain dict reads (GIL-safe).
- **Elevated UI** — the agent runs highest-privileges in the
  interactive session; a local window is fine. (UIPI only restricts
  *lower*-privilege processes sending messages *up*.)
- **Token regeneration footgun** → warning + restart-required, §7.
- **User quits the agent mid-automation** → accepted by design: local
  user outranks remote control. Remote clients see a clean connection
  refusal + last known `/health` state, not corruption.

## 12. Open questions for boss

1. SYSTEM helper in scope for Quit (checkbox offered in §6/§8) — OK,
   or strictly agent-only in v1?
2. Any category you want split further (e.g. `/file/download` read vs
   `/file/upload` write as separate switches)?
3. ~~Icon: generated "W" glyph vs real `.ico`~~ — **answered 2026-11-02:**
   real generated asset; boss chose the flat TV-remote handset
   (`assets/winremote.ico`). Delivery via embedded 16/32 px PNGs (§6),
   so the updater stays single-file.

## 13. Out of scope (v1)

- No remote settings writes (by design, §2.4).
- No lock-screen interaction (unchanged policy; `/system/lock/state`
  remains detection only).
- No multi-machine management, no logs viewer (console output goes to
  the launcher today; a log tab is a possible v2).
- No installer changes beyond what exists.
