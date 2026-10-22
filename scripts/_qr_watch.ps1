# Watch the WeChat login on this machine and auto-refresh the QR image.
# The QR rotates every ~1 minute; each rotation pops a fresh image window so the
# human can scan whatever is on screen without asking anyone to re-post it.
$ErrorActionPreference = "Continue"
$root = "D:\Project\ai_agent\agent-remote-bridge"
$home2 = "$env:TEMP\arb-login-home"
$log = "$env:TEMP\arb-login.log"
$png = "D:\Project\ai_agent\qr-live.png"
New-Item -ItemType Directory -Force -Path $home2 | Out-Null
foreach ($f in @($log, $png)) { Remove-Item $f -ErrorAction SilentlyContinue }

$env:PYTHONPATH = "$root\src"
$env:ARB_HOME = $home2
$env:PYTHONIOENCODING = "utf-8"

Write-Host "starting login on this machine..."
$proc = Start-Process -FilePath "python" -ArgumentList "-X", "utf8", "-m", "arb", "login" `
  -WorkingDirectory $root -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput $log -RedirectStandardError "$env:TEMP\arb-login.err"

$last = ""
$deadline = (Get-Date).AddMinutes(12)
$shown = 0
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 1500
  if ($proc.HasExited -and -not (Test-Path $log)) { break }
  $text = ""; foreach ($f in @($log, "$env:TEMP\arb-login.err")) { if (Test-Path $f) { $text += (Get-Content $f -Raw -Encoding UTF8) } }
  $url = ""
  foreach ($m in [regex]::Matches($text, "https://liteapp\S+")) { $url = $m.Value }
  if ($url -and $url -ne $last) {
    $last = $url
    python -X utf8 -c "import segno; segno.make(r'$url', error='m').save(r'D:\Project\ai_agent\qr-live.png', scale=12, border=2)" 2>$null
    if (Test-Path $png) {
      Start-Process $png
      $shown++
      Write-Host "[$(Get-Date -Format HH:mm:ss)] new QR shown (#$shown)"
    }
  }
  if ($text -match "登录成功|已授权|授权成功|login ok|token") {
    if ($text -notmatch "未安装|刷新") { Write-Host "login seems to have completed"; break }
  }
  if ($proc.HasExited) {
    Write-Host "login process exited"
    if (Test-Path "$home2\ilink.json") { Write-Host "ilink.json written" }
    break
  }
}
Write-Host "watcher done; QR windows shown: $shown"
