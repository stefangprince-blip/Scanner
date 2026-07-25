$ErrorActionPreference = "Stop"

$repoRoot = "C:\Scanner"
$cloudflaredConfig = Join-Path $repoRoot ".cloudflared\config.yml"
$logDir = Join-Path $repoRoot "logs"
$serverLog = Join-Path $logDir "server.log"
$serverErrLog = Join-Path $logDir "server.err.log"
$tunnelLog = Join-Path $logDir "tunnel.log"
$tunnelErrLog = Join-Path $logDir "tunnel.err.log"

if (!(Test-Path $logDir)) {
  New-Item -Path $logDir -ItemType Directory | Out-Null
}

try {
  $serverConn = Get-NetTCPConnection -LocalPort 5057 -State Listen -ErrorAction Stop
  if ($serverConn) {
    Stop-Process -Id $serverConn.OwningProcess -Force
    Start-Sleep -Milliseconds 500
  }
} catch {}

$existingTunnel = Get-Process -Name cloudflared -ErrorAction SilentlyContinue
if ($existingTunnel) {
  foreach ($p in $existingTunnel) {
    Stop-Process -Id $p.Id -Force
  }
  Start-Sleep -Milliseconds 500
}

Start-Process -FilePath "python.exe" `
  -ArgumentList "run.py" `
  -WorkingDirectory $repoRoot `
  -WindowStyle Minimized `
  -RedirectStandardOutput $serverLog `
  -RedirectStandardError $serverErrLog

$cloudflaredExe = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cloudflaredExe) {
  throw "cloudflared was not found in PATH."
}

$configText = ""
if (Test-Path $cloudflaredConfig) {
  $configText = Get-Content $cloudflaredConfig -Raw
}
$hasNamedTunnel = ($configText -match "(?m)^\s*tunnel:\s*[^\s#]+") -and ($configText -match "(?m)^\s*credentials-file:\s*[^\s#]+")

if ($hasNamedTunnel) {
  Start-Process -FilePath $cloudflaredExe `
    -ArgumentList @("tunnel", "--config", $cloudflaredConfig, "run") `
    -WorkingDirectory $repoRoot `
    -WindowStyle Minimized `
    -RedirectStandardOutput $tunnelLog `
    -RedirectStandardError $tunnelErrLog
} else {
  Start-Process -FilePath $cloudflaredExe `
    -ArgumentList @("tunnel", "--url", "http://127.0.0.1:5057", "--no-autoupdate") `
    -WorkingDirectory $repoRoot `
    -WindowStyle Minimized `
    -RedirectStandardOutput $tunnelLog `
    -RedirectStandardError $tunnelErrLog
}

Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:5057"
Write-Output "Scanner server and Cloudflare tunnel started. Logs: $logDir"
