"""Package only explicit program files, plus logs; never include merchant data."""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT=Path(__file__).resolve().parents[1]
PROGRAM=ROOT/'dist'/'test-2.6.0-final'/'StockAndSleeve'
OUT=ROOT/'Downloads';OUT.mkdir(exist_ok=True)
if not (PROGRAM/'StockAndSleeve.exe').is_file():raise SystemExit('Build the preview first.')
manifest={}
installer=r'''$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = Join-Path $packageRoot 'StockAndSleeve'
$expected = Get-Content -LiteralPath (Join-Path $packageRoot 'SHA256.json') -Raw | ConvertFrom-Json
foreach ($entry in $expected.PSObject.Properties) {
  $path = Join-Path $source $entry.Name
  if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) { throw "Package verification failed: $($entry.Name)" }
}
$tag = [Guid]::NewGuid().ToString('N').Substring(0,8)
$destination = Join-Path $env:LOCALAPPDATA ('Programs\StockAndSleevePreview\2.6.0-' + $tag)
[IO.Directory]::CreateDirectory($destination) | Out-Null
Copy-Item -LiteralPath $source -Destination $destination -Recurse
$program = Join-Path $destination 'StockAndSleeve\StockAndSleeve.exe'
$profile = Join-Path $env:LOCALAPPDATA 'StockAndSleeve\TestProfile-2.6.0'
$launcher = Join-Path $destination 'Start preview.vbs'
$lines = @('Set s=CreateObject("WScript.Shell")', ('s.Environment("Process")("ROCA_DATA_HOME")="' + $profile + '"'), 's.Environment("Process")("ROCA_STATION_PORT")="8876"', ('s.Run Chr(34) & "' + $program + '" & Chr(34), 0, False'))
[IO.File]::WriteAllLines($launcher,$lines)
$shell=New-Object -ComObject WScript.Shell
foreach ($folder in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))) {
  $link=$shell.CreateShortcut((Join-Path $folder 'Stock & Sleeve Test.lnk'))
  $link.TargetPath=Join-Path $env:WINDIR 'System32\wscript.exe'
  $link.Arguments='"'+$launcher+'"';$link.WorkingDirectory=$destination
  $link.IconLocation=(Join-Path $destination 'StockAndSleeve\_internal\assets\stock-and-sleeve.ico')+',0'
  $link.Description='Stock & Sleeve 2.6.0 local test - separate profile';$link.Save()
}
Write-Output 'Installed. Open Stock & Sleeve Test from your Desktop. Existing business data was not migrated.'
'''
readme='''Stock & Sleeve 2.6.0 LOCAL TEST BUILD

Extract this ZIP completely, then double-click Install Test Preview.cmd.
Open the Stock & Sleeve Test Desktop icon. Python installation is not needed;
Chrome is still required. The test shortcut uses local port 8876, allowing the
existing station to remain open on its default port. Do not enter production credentials.

Uses a separate TestProfile-2.6.0 data directory. Existing business data is not
copied, migrated or overwritten. The test app is unsigned.

AVAILABLE: reviewed intake, labels, matching-price Square variations, local
customer credit history/manual use, manager adjustments and audit export.
DOMAIN TESTS ONLY: reserved-credit settlement from fictional completed Square
discount orders; hosted OAuth state/token processing.
NOT CONNECTED: shared online balances, public webhooks, end-to-end OAuth,
Register/Terminal automatic checkout, pricing subscriptions or tax validation.
CLEANUP: conservative eligibility journal/dry run; live deletion disabled.

Logs and screenshots are in Test results. Their fictional fixtures are not proof
of live merchant operations. No production Square data was changed.
'''
archive=OUT/'Stock-and-Sleeve-2.6.0-Local-Test.zip'
with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
    for file in sorted(PROGRAM.rglob('*')):
        if file.is_file():
            relative=file.relative_to(PROGRAM).as_posix()
            if any(p in ('station_data','outputs','.env','backups') for p in file.relative_to(PROGRAM).parts):
                raise RuntimeError('Unexpected business files in the program build.')
            manifest[relative]=hashlib.sha256(file.read_bytes()).hexdigest()
            z.write(file,'StockAndSleeve/'+relative)
    z.writestr('SHA256.json',json.dumps(manifest,indent=2))
    z.writestr('Install Test Preview.ps1',installer)
    z.writestr('Install Test Preview.cmd','@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "Install Test Preview.ps1"\r\npause\r\n')
    z.writestr('READ ME FIRST.txt',readme)
    for file in (ROOT/'Testing'/'2.6.0').iterdir():
        if file.is_file():z.write(file,'Test results/'+file.name)
with zipfile.ZipFile(archive) as z:
    if z.testzip():raise RuntimeError('Archive integrity failed.')
(OUT/'Stock-and-Sleeve-2.6.0-Local-Test.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
print(archive);print('Bytes:',archive.stat().st_size);print('Program files verified:',len(manifest))
