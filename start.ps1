param(
    [int]$Port = 8765
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "Starting Qoder Cockpit at http://127.0.0.1:$Port"

# 优先使用 Windows Python 启动器，兼容仅配置了 py -3 的环境；
# 未提供启动器时回退到 PATH 中的 python 可执行文件。
$pythonCommand = Get-Command py -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $pythonPath = $pythonCommand.Source
    $pythonPrefix = @('-3')
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw '未找到 Python，请安装 Python 3 并确保 py -3 或 python 在 PATH 中'
    }
    $pythonPath = $pythonCommand.Source
    $pythonPrefix = @()
}

& $pythonPath @pythonPrefix -c "import cryptography" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing the local cryptography dependency...'
    & $pythonPath @pythonPrefix -m pip install -r (Join-Path $root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请手动执行 python -m pip install -r requirements.txt' }
}
& $pythonPath @pythonPrefix (Join-Path $root 'server.py') --host 127.0.0.1 --port $Port
