param(
    [string]$DesktopFolder = [Environment]::GetFolderPath('Desktop'),
    [string]$ProgramsFolder = [Environment]::GetFolderPath('Programs')
)

$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $appRoot 'Start Roca.vbs'
$python = Join-Path $appRoot '.venv\Scripts\pythonw.exe'
$icon = Join-Path $appRoot 'assets\stock-and-sleeve-d.ico'
$brand = Get-Content -LiteralPath (Join-Path $appRoot 'product_brand.json') -Raw | ConvertFrom-Json
$displayName = [string]$brand.display_name
if (-not $displayName -or $displayName.Length -gt 60 -or $displayName.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
    throw 'The product display name is invalid.'
}
foreach ($requiredFile in @($launcher, $python, $icon)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Roca setup is incomplete. Missing: $requiredFile"
    }
}

$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @($DesktopFolder, $ProgramsFolder)) {
    if (-not $folder -or -not [IO.Path]::IsPathRooted($folder)) {
        throw 'Windows did not provide a valid shortcut folder.'
    }
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    $shortcutPath = Join-Path $folder ($displayName + '.lnk')
    if (Test-Path -LiteralPath $shortcutPath) {
        $existing = $shell.CreateShortcut($shortcutPath)
        if ($existing.TargetPath -and $existing.TargetPath -ne (Join-Path $env:WINDIR 'System32\wscript.exe')) {
            throw "An unrelated shortcut already uses $shortcutPath. It was not replaced."
        }
        if ($existing.Arguments -and $existing.Arguments -ne ('"' + $launcher + '"')) {
            throw "A shortcut for a different installation already uses $shortcutPath. It was not replaced."
        }
    }
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
    $shortcut.Arguments = '"' + $launcher + '"'
    $shortcut.WorkingDirectory = $appRoot
    $shortcut.IconLocation = $icon + ',0'
    $shortcut.Description = 'Open ' + $displayName + ' and start its background service'
    $shortcut.WindowStyle = 7
    $shortcut.Save()
    Write-Output "Installed: $shortcutPath"
    # Remove only old shortcuts that point to this exact installation.
    foreach ($oldName in @('Roca Station', 'Card Store Suite')) {
        $oldPath = Join-Path $folder ($oldName + '.lnk')
        if ($oldPath -ne $shortcutPath -and (Test-Path -LiteralPath $oldPath)) {
            $old = $shell.CreateShortcut($oldPath)
            if ($old.TargetPath -eq $shortcut.TargetPath -and $old.Arguments -eq $shortcut.Arguments) {
                Remove-Item -LiteralPath $oldPath
                Write-Output "Replaced old shortcut: $oldName"
            }
        }
    }
}
