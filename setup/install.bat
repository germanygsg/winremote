@echo off
REM WinRemote one-time setup for the Windows machine.
REM - Installs Python dependencies
REM - Generates an auth token (saved to %APPDATA%\winremote\token.txt)
REM - Registers WinRemoteAgent (logon, highest) to start the agent via
REM   setup\winremote-launch.ps1, which feeds the saved token to the agent
REM   through WINREMOTE_TOKEN. Launching the agent bare makes it generate a
REM   random token, so every client call would 401 -- do NOT point the task
REM   at winremote_agent.py directly.
REM - Registers WinRemoteSystem (SYSTEM, at startup) for lock-screen support
REM   via the 127.0.0.1:8767 helper (localhost-only, no token needed).

setlocal

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ first.
    exit /b 1
)

echo [1/5] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r "%~dp0..\agent\requirements.txt"
if errorlevel 1 exit /b 1

echo [2/5] Generating auth token...
set APPDATA_DIR=%APPDATA%\winremote
if not exist "%APPDATA_DIR%" mkdir "%APPDATA_DIR%"
set TOKEN_FILE=%APPDATA_DIR%\token.txt
if not exist "%TOKEN_FILE%" (
    python -c "import secrets; open(r'%TOKEN_FILE%','w').write(secrets.token_hex(24))"
    echo Token saved to %TOKEN_FILE%
) else (
    echo Token already exists at %TOKEN_FILE%
)

REM Resolve absolute paths (schtasks does not expand ".." in /tr).
pushd "%~dp0" >nul
set SETUP_DIR=%CD%
popd >nul
pushd "%~dp0..\agent" >nul
set AGENT_DIR=%CD%
popd >nul

echo [3/5] Registering scheduled task (WinRemoteAgent)...
schtasks /delete /tn "WinRemoteAgent" /f >nul 2>&1
schtasks /create /tn "WinRemoteAgent" /tr "powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File \"%SETUP_DIR%\winremote-launch.ps1\"" /sc onlogon /rl highest /f
if errorlevel 1 (
    echo [ERROR] Failed to register scheduled task. Run as administrator.
    exit /b 1
)

echo [4/5] Registering system helper task (WinRemoteSystem)...
schtasks /delete /tn "WinRemoteSystem" /f >nul 2>&1
schtasks /create /tn "WinRemoteSystem" /tr "python \"%AGENT_DIR%\winremote_system.py\"" /sc onstart /ru SYSTEM /f
if errorlevel 1 (
    echo [WARNING] Failed to register WinRemoteSystem task. Lock-screen support will be unavailable after reboot.
) else (
    schtasks /run /tn "WinRemoteSystem" >nul 2>&1
)

echo [5/5] Starting agent...
schtasks /run /tn "WinRemoteAgent" >nul 2>&1

echo.
echo Done. The agent listens on port 8765 (localhost + Tailscale).
echo Token: %TOKEN_FILE%
echo Test: curl -H "Authorization: Bearer YOUR_TOKEN" http://100.69.141.114:8765/health
