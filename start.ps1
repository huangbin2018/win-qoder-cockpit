param(
    [int]$Port = 8765
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "Starting Qoder Cockpit at http://127.0.0.1:$Port"
python -c "import cryptography" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing the local cryptography dependency...'
    python -m pip install -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请手动执行 python -m pip install -r requirements.txt' }
}
python (Join-Path $root 'server.py') --host 127.0.0.1 --port $Port
