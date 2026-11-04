# Clean launch: kill any previous Hyperdrive Remote server, start fresh, wait until healthy.
$ErrorActionPreference = "SilentlyContinue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = "http://127.0.0.1:5050/"
$port = 5050

function Find-Python {
  $candidates = @(
    "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
  )
  foreach ($c in $candidates) {
    if (Test-Path $c) { return $c }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

function Stop-HyperdriveServer {
  Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -and ($_.CommandLine -like "*$dir*app.py*")
  } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Milliseconds 600
}

function Test-ServerHealthy {
  try {
    $r = Invoke-WebRequest -Uri "$url" -UseBasicParsing -TimeoutSec 2
    return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500)
  } catch {
    return $false
  }
}

Stop-HyperdriveServer

$python = Find-Python
if (-not $python) {
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show(
    "Python introuvable. Installe Python ou ajoute-le au PATH.",
    "Hyperdrive Remote"
  ) | Out-Null
  exit 1
}

Start-Process -FilePath $python -ArgumentList "app.py" -WorkingDirectory $dir -WindowStyle Hidden

$ok = $false
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 250
  if (Test-ServerHealthy) { $ok = $true; break }
}

if (-not $ok) {
  Add-Type -AssemblyName PresentationFramework
  [System.Windows.MessageBox]::Show(
    "Le serveur n'a pas demarre sur http://127.0.0.1:5050",
    "Hyperdrive Remote"
  ) | Out-Null
  exit 2
}

Start-Process $url
exit 0
