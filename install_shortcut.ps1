param([string]$Name = '同频课程播放器（开源版）')

$ErrorActionPreference = 'Stop'
if ($Name.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) { throw '快捷方式名称含无效字符。' }
$ShortcutLauncher = Join-Path $PSScriptRoot 'launch_player.ps1'
if (-not (Test-Path -LiteralPath $ShortcutLauncher)) { throw '没有找到 launch_player.ps1。' }
$ShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) ($Name + '.lnk')
$ShortcutShell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $ShortcutPath) {
    $ExistingShortcut = $ShortcutShell.CreateShortcut($ShortcutPath)
    if ($ExistingShortcut.Arguments.IndexOf($ShortcutLauncher, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        throw '同名快捷方式已经属于其他位置；请用 -Name 指定另一个名称。'
    }
}
$Shortcut = $ShortcutShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$Shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $ShortcutLauncher + '"'
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.Description = '同频：本地课堂录像与 PPT 双画面课程播放器'
$Shortcut.IconLocation = (Join-Path $PSScriptRoot 'player.ico') + ',0'
$Shortcut.WindowStyle = 7
$Shortcut.Save()
Write-Output ('已创建快捷方式：' + $ShortcutPath)
