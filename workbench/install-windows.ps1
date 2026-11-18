param(
    [string]$LinuxRoot,
    [string]$Distro,
    [switch]$Launch
)
$ErrorActionPreference = 'Stop'
if ($PSScriptRoot -match '^\\\\(?:wsl\$|wsl\.localhost)\\([^\\]+)\\(.+)$') {
    if (!$Distro) { $Distro = $Matches[1] }
    if (!$LinuxRoot) { $LinuxRoot = '/' + (($Matches[2] -replace '\\workbench$', '') -replace '\\', '/') }
}
if (!$Distro) { $Distro = 'Ubuntu-24.04' }
if (!$LinuxRoot -or !$LinuxRoot.StartsWith('/')) {
    throw '请从 WSL 仓库目录运行，或以 -LinuxRoot 指定仓库在 Ubuntu 中的绝对路径。'
}
wsl.exe -d $Distro --cd $LinuxRoot -- test -f workbench/app.py
if ($LASTEXITCODE) { throw '在所选 WSL 分发和目录中找不到工作台。' }
$desktopApp = Join-Path $env:LOCALAPPDATA 'ColabVideoWorkbench'
$venv = Join-Path $desktopApp 'venv'
$python = Join-Path $venv 'Scripts\python.exe'
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
if (!(Get-Command uv -ErrorAction SilentlyContinue)) {
    throw '请先安装 Python 3.13 和 uv，再运行本脚本。'
}
if (!(Test-Path -LiteralPath $python)) {
    uv venv --python 3.13 $venv
    if ($LASTEXITCODE) { throw 'Python 环境初始化失败。' }
}
uv pip install --python $python pywebview==6.2.1
if ($LASTEXITCODE) { throw 'WebView2 桌面依赖安装失败。' }
$source = Join-Path $PSScriptRoot 'desktop.py'
$launcher = Join-Path $desktopApp 'desktop.py'
Copy-Item -LiteralPath $source -Destination $launcher -Force
@{distro = $Distro; linux_root = $LinuxRoot} | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $desktopApp 'desktop-config.json') -Encoding UTF8
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Colab 影像工作台.lnk'
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $launcher + '"'
$shortcut.WorkingDirectory = $desktopApp
$shortcut.Description = '使用 Google Colab 生成、审片和导出视频'
$shortcut.Save()
Write-Output $shortcutPath
if ($Launch) { Start-Process -FilePath $pythonw -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $desktopApp -WindowStyle Hidden }
