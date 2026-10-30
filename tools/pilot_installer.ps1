param([switch]$VerifyOnly)
$ErrorActionPreference = 'Stop'

function Get-PilotHash([string]$Path) {
    $stream = [IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-','').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}

function Assert-PilotRelativePath([string]$Name) {
    if (-not $Name -or $Name.Contains('\') -or $Name.Contains(':')) { throw 'Unsafe package path.' }
    foreach ($part in $Name.Split('/')) {
        if (-not $part -or $part -in @('.', '..') -or $part.EndsWith('.') -or $part.EndsWith(' ') -or
            $part -match '[\x00-\x1f<>"|?*]' -or $part -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$') {
            throw 'Unsafe package path.'
        }
        if ($part.ToLowerInvariant() -in @('.git','.venv','__pycache__','station_data','outputs','backups','profiles','profile',
            'merchant','merchant_data','merchant-data','merchantdata','cloud.session','cloud.pending','square.secret',
            'vault.key','roca_settings.json','installed_release.json','runtime.json','runtime.lock') -or
            $part -match '^(\.env($|\.)|testprofile-|merchant[-_])') { throw 'Merchant or credential data in package.' }
    }
    if ([IO.Path]::GetExtension($Name) -in @('.db','.sqlite','.sqlite3','.csv','.log','.secret','.key','.token','.pfx','.p12','.pdf','.zpl')) {
        throw 'Data or credentials in program payload.'
    }
    if ([IO.Path]::GetExtension($Name) -eq '.pem' -and $Name -cne '_internal/certifi/cacert.pem') {
        throw 'Unexpected certificate in program payload.'
    }
}

function Get-PilotFiles([string]$Root) {
    $resolved = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $resolved -PathType Container)) { throw 'Missing program folder.' }
    $result = @{}
    function Visit-PilotFolder([string]$Folder) {
        foreach ($entry in Get-ChildItem -LiteralPath $Folder -Force) {
            $relative = $entry.FullName.Substring($resolved.Length + 1).Replace('\','/')
            Assert-PilotRelativePath $relative
            if ($relative -cne 'StockAndSleeve.exe' -and $relative -cne '_internal' -and -not $relative.StartsWith('_internal/')) {
                throw 'Unexpected top-level program payload.'
            }
            if (-not $entry.PSIsContainer -and $entry.Extension -eq '.json' -and $relative -cnotin
                @('_internal/desktop_build.json','_internal/product_brand.json','_internal/roca_edition.json',
                  '_internal/cryptography-50.0.2.dist-info/sboms/sbom.json',
                  '_internal/cryptography-50.0.2.dist-info/sboms/cryptography-rust.cyclonedx.json')) {
                throw 'Unapproved JSON data in program payload.'
            }
            # Junctions/symbolic links redirect names. OneDrive cloud placeholders do not.
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -and $entry.LinkType) {
                throw 'Links and junctions are not permitted.'
            }
            if ($entry.PSIsContainer) { Visit-PilotFolder $entry.FullName }
            else {
                if ($result.ContainsKey($relative)) { throw 'Aliased package paths.' }
                $result[$relative] = $entry.FullName
            }
        }
    }
    $rootItem = Get-Item -LiteralPath $resolved -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -and $rootItem.LinkType) { throw 'Linked program root.' }
    Visit-PilotFolder $resolved
    return ,$result
}

function Test-PilotPayload([string]$Root, $Expected) {
    $files = Get-PilotFiles $Root
    $names = @($Expected.PSObject.Properties)
    if ($names.Count -eq 0 -or $files.Count -ne $names.Count) { throw 'Unexpected or missing program files.' }
    $seen = @{}
    foreach ($entry in $names) {
        Assert-PilotRelativePath $entry.Name
        if ($seen.ContainsKey($entry.Name) -or $entry.Value -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid hash manifest.' }
        $seen[$entry.Name] = $true
        if (-not $files.ContainsKey($entry.Name) -or
            (Get-PilotHash $files[$entry.Name]) -cne $entry.Value) {
            throw ('Package verification failed: ' + $entry.Name)
        }
    }
    return $true
}

function Copy-PilotPayload([string]$Source, [string]$Destination, $Expected) {
    $null = Test-PilotPayload $Source $Expected
    if (Test-Path -LiteralPath $Destination) { throw 'Installation destination already exists.' }
    [IO.Directory]::CreateDirectory($Destination) | Out-Null
    foreach ($entry in $Expected.PSObject.Properties) {
        $target = Join-Path $Destination $entry.Name
        [IO.Directory]::CreateDirectory((Split-Path -Parent $target)) | Out-Null
        $original = [IO.File]::Open((Join-Path $Source $entry.Name),[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
        try {
            $copy = [IO.File]::Open($target,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            try { $original.CopyTo($copy) } finally { $copy.Dispose() }
        } finally { $original.Dispose() }
    }
    # No launcher or shortcut exists until both copies and source pass again.
    $null = Test-PilotPayload $Destination $Expected
    $null = Test-PilotPayload $Source $Expected
}

function Reserve-PilotPort([string]$ProfileBase, [int[]]$Candidates = @()) {
    $portRoot = Join-Path $ProfileBase '.ports'
    [IO.Directory]::CreateDirectory($portRoot) | Out-Null
    if ($Candidates.Count -eq 0) { $Candidates = @(1..100 | ForEach-Object { Get-Random -Minimum 20000 -Maximum 60000 }) }
    foreach ($port in $Candidates) {
        if ($port -lt 1024 -or $port -gt 65535 -or $port -in @(8765,8876)) { continue }
        $reservation = Join-Path $portRoot ([string]$port)
        try { $claim = [IO.File]::Open($reservation,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None) }
        catch [IO.IOException] { continue }
        $listener = $null
        $available = $false
        try {
            $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$port)
            $listener.Server.ExclusiveAddressUse = $true
            $listener.Start()
            $available = $true
        } catch [Net.Sockets.SocketException] {
            $available = $false
        } finally {
            if ($listener) { $listener.Stop() }
            $claim.Dispose()
            if (-not $available) { Remove-Item -LiteralPath $reservation }
        }
        if ($available) { return $port }
    }
    throw 'No unused pilot port was available. Other applications were not stopped.'
}

function Write-PilotLauncher([string]$Path, [string]$Program, [string]$Profile, [int]$Port, [switch]$Stop) {
    if ($Program.Contains('"') -or $Profile.Contains('"') -or $Port -lt 1024 -or $Port -gt 65535) { throw 'Invalid launcher parameters.' }
    $argument = if ($Stop) { ' --stop' } else { '' }
    $lines = @('Set s=CreateObject("WScript.Shell")',
        ('s.Environment("Process")("ROCA_DATA_HOME")="' + $Profile + '"'),
        ('s.Environment("Process")("ROCA_STATION_PORT")="' + $Port + '"'),
        ('s.Run Chr(34) & "' + $Program + '" & Chr(34) & "' + $argument + '", 0, False'))
    [IO.File]::WriteAllLines($Path,$lines,[Text.Encoding]::Unicode)
}

function New-PilotShortcut([string]$Folder, [string]$Name, [string]$Tag, [string]$Launcher,
                           [string]$WorkingDirectory, [string]$Icon) {
    [IO.Directory]::CreateDirectory($Folder) | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $temporary = Join-Path $Folder ('.pilot-' + [Guid]::NewGuid().ToString('N') + '.lnk')
    try {
        $link = $shell.CreateShortcut($temporary)
        $link.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
        $link.Arguments = '"' + $Launcher + '"'
        $link.WorkingDirectory = $WorkingDirectory
        $link.IconLocation = $Icon + ',0'
        $link.Description = 'Stock & Sleeve isolated shared pilot - ' + $Tag
        $link.Save()
        $target = Join-Path $Folder ($Name + '.lnk')
        if (Test-Path -LiteralPath $target) { $target = Join-Path $Folder ($Name + ' (' + $Tag + ').lnk') }
        # Move is create-new, so a racing install cannot overwrite another link.
        [IO.File]::Move($temporary,$target)
        return $target
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
    }
}

function Install-SharedPilot([string]$PackageRoot, [string]$ProgramBase, [string]$ProfileBase,
                            [string[]]$ShortcutFolders, [switch]$ValidateOnly) {
    $package = Get-Content -LiteralPath (Join-Path $PackageRoot 'package.json') -Raw | ConvertFrom-Json
    if ($package.schema -ne 1 -or $package.kind -cne 'shared-pilot' -or $package.version -cnotmatch '^\d+\.\d+\.\d+(-[A-Za-z0-9-]+)?$' -or
        $package.build_id -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid pilot package metadata.' }
    $source = Join-Path $PackageRoot 'StockAndSleeve'
    $expected = Get-Content -LiteralPath (Join-Path $PackageRoot 'ProgramSHA256.json') -Raw | ConvertFrom-Json
    $null = Test-PilotPayload $source $expected
    foreach ($required in @('StockAndSleeve.exe','_internal/desktop_build.json','_internal/assets/stock-and-sleeve.ico')) {
        if (-not ($expected.PSObject.Properties.Name -ccontains $required)) { throw 'Incomplete program package.' }
    }
    $build = Get-Content -LiteralPath (Join-Path $source '_internal/desktop_build.json') -Raw | ConvertFrom-Json
    if ($build.build_id -cne $package.build_id -or $build.format_version -ne 1 -or $build.channel -cne 'private-pilot') {
        throw 'Wrong build identity or unsupported pilot channel.'
    }
    if ($ValidateOnly) { return @{ verified = $true; build_id = $package.build_id } }
    foreach ($base in @($ProgramBase,$ProfileBase) + $ShortcutFolders) {
        if (-not $base -or -not [IO.Path]::IsPathRooted($base)) { throw 'An absolute installation folder is required.' }
    }
    $tag = $package.version + '-' + [Guid]::NewGuid().ToString('N').Substring(0,12)
    $destination = Join-Path $ProgramBase $tag
    $programRoot = Join-Path $destination 'StockAndSleeve'
    Copy-PilotPayload $source $programRoot $expected
    $profile = Join-Path $ProfileBase $tag
    if (Test-Path -LiteralPath $profile) { throw 'Pilot profile already exists.' }
    [IO.Directory]::CreateDirectory($profile) | Out-Null
    $port = Reserve-PilotPort $ProfileBase
    $program = Join-Path $programRoot 'StockAndSleeve.exe'
    $start = Join-Path $destination 'Start Pilot.vbs'
    $stop = Join-Path $destination 'Stop Pilot.vbs'
    Write-PilotLauncher $start $program $profile $port
    Write-PilotLauncher $stop $program $profile $port -Stop
    $links = @()
    foreach ($folder in $ShortcutFolders) {
        $icon = Join-Path $programRoot '_internal/assets/stock-and-sleeve.ico'
        $links += New-PilotShortcut $folder 'Stock & Sleeve Pilot' $tag $start $destination $icon
        $links += New-PilotShortcut $folder 'Stop Stock & Sleeve Pilot' $tag $stop $destination $icon
    }
    $record = @{ schema=1; kind='shared-pilot'; tag=$tag; version=$package.version; build_id=$package.build_id;
        program=$program; profile=$profile; port=$port; start=$start; stop=$stop; shortcuts=$links }
    $record | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $profile 'pilot-install.json') -Encoding UTF8
    return $record
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        $result = Install-SharedPilot -PackageRoot $PSScriptRoot -ValidateOnly:$VerifyOnly `
            -ProgramBase (Join-Path $env:LOCALAPPDATA 'Programs\StockAndSleevePilot') `
            -ProfileBase (Join-Path $env:LOCALAPPDATA 'StockAndSleeve\Pilots') `
            -ShortcutFolders @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))
        $result | ConvertTo-Json -Depth 5
        if (-not $VerifyOnly) { Write-Output 'Installed. Open Stock & Sleeve Pilot. No existing store data was migrated or started.' }
    } catch {
        Write-Error ('Pilot installation stopped: ' + $_.Exception.Message)
        exit 1
    }
}
