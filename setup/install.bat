@echo off
REM WinRemote one-time setup for the Windows machine.
REM - Installs Python dependencies
REM - Generates an auth token (saved to %APPDATA%\winremote\token.txt)
REM - Registers a scheduled task to start the agent at logon (highest privileges)

setlocal

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ first.
    exit /b 1
)

echo [1/4] Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r "%~dp0..\agent\requirements.txt"
if errorlevel 1 exit /b 1

echo [2/4] Generating auth token...
set APPDATA_DIR=%APPDATA%\winremote
if not exist "%APPDATA_DIR%" mkdir "%APPDATA_DIR%"
set TOKEN_FILE=%APPDATA_DIR%\token.txt
if not exist "%TOKEN_FILE%" (
    python -c "import secrets; open(r'%TOKEN_FILE%','w').write(secrets.token_hex(24))"
    echo Token saved to %TOKEN_FILE%
) else (
    echo Token already exists at %TOKEN_FILE%
)

echo [3/4] Registering scheduled task (WinRemoteAgent)...
set AGENT_DIR=%~dp0..\agent
schtasks /delete /tn "WinRemoteAgent" /f >nul 2>&1
schtasks /create /tn "WinRemoteAgent" /tr "python \"%AGENT_DIR%\winremote_agent.py\"" /sc onlogon /rl highest /f
if errorlevel 1 (
    echo [ERROR] Failed to register scheduled task. Run as administrator.
    exit /b 1
)

echo [4/4] Starting agent...
schtasks /run /tn "WinRemoteAgent" >nul 2>&1

echo.
echo Done. The agent listens on port 8765 (localhost + Tailscale).
echo Token: %TOKEN_FILE%
echo Test: curl -H "Authorization: Bearer YOUR_TOKEN" http://100.69.141.114:8765/health
