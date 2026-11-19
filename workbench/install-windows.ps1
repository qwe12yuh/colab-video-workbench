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
    throw '请先安装 Windows 版 uv，再运行本脚本。'
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
$desktop = [Environment]::GetFolderPath('DesktopDirectory', 'Create')
if (!$desktop) { throw '找不到 Windows 桌面目录。' }
$shortcutPath = Join-Path $desktop 'Colab 影像工作台.lnk'
# Use the Unicode Shell Link interface so the filename and target paths work on any Windows locale.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

public static class WorkbenchShortcut
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    // Keep the native IShellLinkW method order, including methods unused by the installer.
    [ComImport, Guid("000214F9-0000-0000-C000-000000000046")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellLinkW
    {
        void GetPath([MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length, IntPtr findData, uint flags);
        void GetIDList(out IntPtr idList);
        void SetIDList(IntPtr idList);
        void GetDescription([MarshalAs(UnmanagedType.LPWStr)] StringBuilder text, int length);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string text);
        void GetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string path);
        void GetArguments([MarshalAs(UnmanagedType.LPWStr)] StringBuilder args, int length);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string args);
        void GetHotkey(out ushort key);
        void SetHotkey(ushort key);
        void GetShowCmd(out int command);
        void SetShowCmd(int command);
        void GetIconLocation([MarshalAs(UnmanagedType.LPWStr)] StringBuilder path, int length, out int index);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string path, int index);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string path, uint reserved);
        void Resolve(IntPtr window, uint flags);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string path);
    }

    public static void Save(string file, string target, string args, string directory, string description)
    {
        var link = (IShellLinkW)new ShellLink();
        link.SetPath(target);
        link.SetArguments(args);
        link.SetWorkingDirectory(directory);
        link.SetDescription(description);
        ((IPersistFile)link).Save(file, false);
    }
}
'@
[WorkbenchShortcut]::Save($shortcutPath, $pythonw, ('"' + $launcher + '"'), $desktopApp, '使用 Google Colab 生成、审片和导出视频')
Write-Output $shortcutPath
if ($Launch) { Start-Process -FilePath $pythonw -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $desktopApp -WindowStyle Hidden }
