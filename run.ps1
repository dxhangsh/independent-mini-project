# PicNamer 一键启动脚本（Windows PowerShell）
# 用法：
#   powershell -ExecutionPolicy Bypass -File run.ps1        # 本机使用 http://127.0.0.1:8765
#   powershell -ExecutionPolicy Bypass -File run.ps1 -Lan   # 手机/局域网 HTTPS（PWA 可安装，ADR-009）
param([switch]$Lan)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root ".venv"
$py = Join-Path $venv "Scripts\python.exe"

# 1) 首次运行：建虚拟环境并装依赖
if (-not (Test-Path $py)) {
    Write-Host "[1/3] Creating venv..." -ForegroundColor Cyan
    python -m venv $venv
    Write-Host "[2/3] Installing deps (1-2 min on first run)..." -ForegroundColor Cyan
    & $py -m pip install -r (Join-Path $root "backend\requirements.txt") -q
}

Set-Location (Join-Path $root "backend")

# 2) 启动后端（前端由后端托管）
if ($Lan) {
    # 局域网模式：生成自签证书（IP 变了会自动重生成），0.0.0.0 监听，HTTPS
    $certOut = Join-Path $root "out"
    $ipLine = & $py (Join-Path $root "tools\make_cert.py") --out $certOut | Select-String "cert ok for ip:"
    $ip = ($ipLine -replace ".*cert ok for ip:\s*", "").Trim()
    if (-not $ip) { throw "LAN IP detect failed; run tools\make_cert.py --ip <IP> manually" }
    Write-Host "[3/3] Starting PicNamer (LAN HTTPS: https://${ip}:8765)" -ForegroundColor Cyan
    Start-Process "https://${ip}:8765"
    & $py -m uvicorn main:app --host 0.0.0.0 --port 8765 --ssl-certfile (Join-Path $certOut "cert.pem") --ssl-keyfile (Join-Path $certOut "key.pem")
} else {
    # 本机模式：端口用 8765（8000 已被另一个订阅项目占用，见决策记录 ADR-006）
    Write-Host "[3/3] Starting PicNamer (http://127.0.0.1:8765) ..." -ForegroundColor Cyan
    Start-Process "http://127.0.0.1:8765"
    & $py -m uvicorn main:app --port 8765
}
