#Requires -Version 5.1
<#
.SYNOPSIS
    WinRemote agent launcher.

.DESCRIPTION
    Loads the bearer token from %APPDATA%\winremote\token.txt, exports it as
    WINREMOTE_TOKEN, and starts the agent. The WinRemoteAgent scheduled task
    runs this script - NOT the agent directly.

    Rationale: winremote_agent.py only honors --token or WINREMOTE_TOKEN. If
    it is launched bare, it generates a random token at startup, while
    install.bat saved a different token to token.txt - so every authenticated
    client call 401s. This launcher guarantees the agent always uses the
    saved token.

    Any extra arguments are forwarded to the agent (e.g. --port 8766).
#>
$ErrorActionPreference = "Stop"

$tokenFile = Join-Path $env:APPDATA "winremote\token.txt"
if (-not (Test-Path $tokenFile)) {
    throw "token not found at $tokenFile - re-run setup\install.bat to generate one"
}
$env:WINREMOTE_TOKEN = (Get-Content $tokenFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($env:WINREMOTE_TOKEN)) {
    throw "token file is empty: $tokenFile"
}

$agentFile = Join-Path (Split-Path $PSScriptRoot -Parent) "agent\winremote_agent.py"
if (-not (Test-Path $agentFile)) {
    throw "agent not found at $agentFile"
}

& python "$agentFile" @args
