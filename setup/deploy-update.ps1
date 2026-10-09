#Requires -Version 5.1
<#
.SYNOPSIS
    WinRemote agent self-update for H410M.

.DESCRIPTION
    Downloads the latest agent from GitHub, verifies it compiles, canary-starts
    it on port 8766 and health-checks it, then stops the old agent (:8765) and
    starts the new one elevated via the WinRemoteAgent scheduled task.

    Safe to re-run. Run from any PowerShell in the user's session — stopping
    own-session processes needs no admin, and schtasks /run executes the task
    with its stored "highest privileges".

    One-liner (paste into PowerShell):
      iwr https://raw.githubusercontent.com/germanygsg/winremote/main/setup/deploy-update.ps1 -OutFile $env:TEMP\wr-upd.ps1; & $env:TEMP\wr-upd.ps1
#>
$ErrorActionPreference = "Stop"

$agentDir  = "C:\winremote\winremote-main\agent"
$agentFile = Join-Path $agentDir "winremote_agent.py"
$base      = "https://raw.githubusercontent.com/germanygsg/winremote/main"
$tokenFile = Join-Path $env:APPDATA "winremote\token.txt"

if (-not (Test-Path $tokenFile)) { throw "token not found at $tokenFile" }
$token = (Get-Content $tokenFile -Raw).Trim()
$env:WINREMOTE_TOKEN = $token

Write-Host "[1/5] Downloading agent from GitHub..."
iwr "$base/agent/winremote_agent.py" -OutFile "$agentFile.tmp" -UseBasicParsing
python -m py_compile "$agentFile.tmp"   # sanity: the new file must compile
Move-Item "$agentFile.tmp" $agentFile -Force
Write-Host "      saved to $agentFile"

Write-Host "[2/5] Canary-starting new agent on :8766 ..."
$canary = Start-Process pythonw -ArgumentList "`"$agentFile`" --port 8766" `
    -WindowStyle Hidden -PassThru
$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
    Start-Sleep 1
    try { $ok = (iwr "http://127.0.0.1:8766/health" -UseBasicParsing).StatusCode -eq 200 } catch {}
}
if (-not $ok) {
    Stop-Process -Id $canary.Id -Force -ErrorAction SilentlyContinue
    throw "canary failed to come up on :8766 - old agent untouched"
}
Write-Host "      canary healthy"

Write-Host "[3/5] Stopping old agent (:8765) ..."
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*winremote_agent.py*" -and $_.CommandLine -notlike "*--port 8766*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

Write-Host "[4/5] Starting new agent elevated via scheduled task ..."
schtasks /run /tn WinRemoteAgent | Out-Null
$ok = $false
for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
    Start-Sleep 1
    try {
        $r = iwr "http://127.0.0.1:8765/health" -UseBasicParsing `
            -Headers @{ Authorization = "Bearer $token" }
        $ok = $r.StatusCode -eq 200
    } catch {}
}
Stop-Process -Id $canary.Id -Force -ErrorAction SilentlyContinue
if (-not $ok) {
    throw "new agent did not come up on :8765. Canary stopped, old agent stopped. " +
          "Start the WinRemoteAgent task manually from Task Scheduler."
}

Write-Host "[5/5] OK - new agent live on :8765"
iwr "http://127.0.0.1:8765/health" -UseBasicParsing `
    -Headers @{ Authorization = "Bearer $token" } |
    Select-Object -ExpandProperty Content
