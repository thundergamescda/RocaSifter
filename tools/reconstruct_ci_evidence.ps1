#requires -Version 7.2
<#
Reconstruct provider-decoded CI evidence. This parses data only: no application,
test module, browser, network, or embedded payload is executed.
Transport integrity does not establish workload/test acceptance.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$LogPath,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [ValidateRange(1024,268435456)][long]$MaxLogBytes=16777216,
    [ValidateRange(1,67108864)][long]$MaxPayloadBytes=67108864,
    [ValidateRange(0,1024)][int]$ExpectedFrameCount=0
)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$utf8=[Text.UTF8Encoding]::new($false,$true)
$rows=[Collections.Generic.List[object]]::new()
$issues=[Collections.Generic.List[string]]::new()
$seen=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
$current=$null
$lineNumber=0
$root=$null
$inputHash=$null
$inputBytes=0
$outputPrepared=$false

function Assert-Transport([bool]$condition,[string]$code) {
    if(-not $condition){
        $errorObject=[IO.InvalidDataException]::new($code)
        $errorObject.Data['TransportCode']=$code
        throw $errorObject
    }
}
function Sha256([byte[]]$bytes) {
    [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}
function Assert-NoLinks([string]$path) {
    $cursor=$path
    while($cursor){
        if(Test-Path -LiteralPath $cursor){
            Assert-Transport (-not ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) 'REPARSE_POINT_REFUSED'
        }
        $parent=[IO.Path]::GetDirectoryName($cursor)
        if($parent -eq $cursor){break}
        $cursor=$parent
    }
}
function Save-NewBytes([string]$relative,[byte[]]$bytes) {
    $target=[IO.Path]::GetFullPath((Join-Path $script:root $relative))
    Assert-Transport ($target.StartsWith($script:root+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) 'OUTPUT_PATH_ESCAPE'
    Assert-NoLinks ([IO.Path]::GetDirectoryName($target))
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))|Out-Null
    $stream=[IO.File]::Open($target,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($bytes,0,$bytes.Length)}finally{$stream.Dispose()}
    return $relative.Replace('\','/')
}
function Read-Marker([string]$text,[bool]$isEnd) {
    $document=$null
    try {
        $document=[Text.Json.JsonDocument]::Parse($text)
        Assert-Transport ($document.RootElement.ValueKind -eq [Text.Json.JsonValueKind]::Object) 'MARKER_NOT_OBJECT'
        $allowed=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach($key in @('name','bytes','sha256','encoding','decoded_bytes','decoded_sha256')){[void]$allowed.Add($key)}
        [void]$allowed.Add($(if($isEnd){'delivered'}else{'convention'}))
        $properties=@{}
        $names=[Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach($property in $document.RootElement.EnumerateObject()){
            Assert-Transport ($allowed.Contains($property.Name)) 'UNKNOWN_MARKER_FIELD'
            Assert-Transport ($names.Add($property.Name)) 'DUPLICATE_MARKER_FIELD'
            $properties[$property.Name]=$property.Value
        }
        foreach($key in @('name','bytes','sha256')){
            Assert-Transport ($properties.ContainsKey($key)) 'MISSING_MARKER_FIELD'
        }
        foreach($key in @('name','sha256')){
            Assert-Transport ($properties[$key].ValueKind -eq [Text.Json.JsonValueKind]::String) 'MARKER_STRING_TYPE'
        }
        $name=$properties.name.GetString()
        Assert-Transport ($name -cmatch '^(?:(?:evidence|diagnostics)/[a-z0-9][a-z0-9_.-]{0,150}|harness-summary\.json)$') 'UNAPPROVED_FRAME_PATH'
        $sha=$properties.sha256.GetString()
        Assert-Transport ($sha -cmatch '^[a-f0-9]{64}$') 'MARKER_HASH_FORMAT'
        Assert-Transport ($properties.bytes.ValueKind -eq [Text.Json.JsonValueKind]::Number) 'MARKER_BYTE_TYPE'
        $bytes=[long]0
        Assert-Transport ($properties.bytes.TryGetInt64([ref]$bytes)) 'MARKER_BYTE_INTEGER'
        Assert-Transport ($bytes -ge 0 -and $bytes -le $MaxPayloadBytes) 'MARKER_BYTE_BOUND'
        $encoding='utf8'
        $explicitEncoding=$properties.ContainsKey('encoding')
        if($explicitEncoding){
            Assert-Transport ($properties.encoding.ValueKind -eq [Text.Json.JsonValueKind]::String) 'MARKER_ENCODING_TYPE'
            $encoding=$properties.encoding.GetString()
            Assert-Transport ($encoding -cin @('utf8','base64')) 'UNSUPPORTED_ENCODING'
        }
        $decodedBytes=$null
        $decodedSha=$null
        if($encoding -ceq 'base64'){
            Assert-Transport ($name.StartsWith('diagnostics/',[StringComparison]::Ordinal)) 'BASE64_OUTSIDE_DIAGNOSTICS'
            Assert-Transport ($properties.ContainsKey('decoded_bytes') -and $properties.ContainsKey('decoded_sha256')) 'MISSING_DECODED_IDENTITY'
            Assert-Transport ($properties.decoded_bytes.ValueKind -eq [Text.Json.JsonValueKind]::Number) 'DECODED_BYTE_TYPE'
            $count=[long]0
            Assert-Transport ($properties.decoded_bytes.TryGetInt64([ref]$count)) 'DECODED_BYTE_INTEGER'
            Assert-Transport ($count -ge 0 -and $count -le 50331648) 'DECODED_BYTE_BOUND'
            $decodedBytes=$count
            Assert-Transport ($properties.decoded_sha256.ValueKind -eq [Text.Json.JsonValueKind]::String) 'DECODED_HASH_TYPE'
            $decodedSha=$properties.decoded_sha256.GetString()
            Assert-Transport ($decodedSha -cmatch '^[a-f0-9]{64}$') 'DECODED_HASH_FORMAT'
        } else {
            Assert-Transport (-not $properties.ContainsKey('decoded_bytes') -and -not $properties.ContainsKey('decoded_sha256')) 'AMBIGUOUS_UTF8_IDENTITY'
        }
        $delivered=$null
        if($isEnd){
            Assert-Transport ($properties.ContainsKey('delivered')) 'MISSING_DELIVERY_FLAG'
            Assert-Transport ($properties.delivered.ValueKind -in @([Text.Json.JsonValueKind]::True,[Text.Json.JsonValueKind]::False)) 'DELIVERY_FLAG_TYPE'
            $delivered=$properties.delivered.GetBoolean()
        } elseif($properties.ContainsKey('convention')){
            Assert-Transport ($properties.convention.ValueKind -eq [Text.Json.JsonValueKind]::String -and $properties.convention.GetString().Length -le 256) 'CONVENTION_FORMAT'
        }
        return @{
            name=$name;bytes=$bytes;sha256=$sha;encoding=$encoding;
            explicit_encoding=$explicitEncoding;decoded_bytes=$decodedBytes;
            decoded_sha256=$decodedSha;delivered=$delivered
        }
    } catch {
        if($_.Exception.Data.Contains('TransportCode')){throw}
        Assert-Transport $false 'MALFORMED_MARKER_JSON'
    } finally {
        if($document){$document.Dispose()}
    }
}
function Complete-Frame($frame,$ending) {
    $begin=$frame.begin
    $problems=[Collections.Generic.List[string]]::new()
    $payload=[string]::Join([string][char]10,[string[]]$frame.lines)
    $outer=$utf8.GetBytes($payload)
    Assert-Transport ($outer.Length -le $MaxPayloadBytes) 'ACTUAL_PAYLOAD_BOUND'
    $outerHash=Sha256 $outer
    if($begin.name -cne $ending.name){$problems.Add('END_NAME_MISMATCH')}
    if($begin.bytes -ne $ending.bytes -or $begin.sha256 -cne $ending.sha256){$problems.Add('MARKER_IDENTITY_MISMATCH')}
    if($begin.encoding -cne $ending.encoding -or $begin.explicit_encoding -ne $ending.explicit_encoding){$problems.Add('ENCODING_MISMATCH')}
    if($begin.decoded_bytes -ne $ending.decoded_bytes -or $begin.decoded_sha256 -cne $ending.decoded_sha256){$problems.Add('DECODED_MARKER_MISMATCH')}
    if($begin.bytes -ne $outer.Length){$problems.Add('PAYLOAD_LENGTH_MISMATCH')}
    if($begin.sha256 -cne $outerHash){$problems.Add('PAYLOAD_HASH_MISMATCH')}
    if($ending.delivered -ne $true){$problems.Add('PRODUCER_DELIVERY_FALSE')}
    $outerVerified=($problems.Count -eq 0)
    $decoded=$null
    $decodedHash=$null
    $decodedBytes=$null
    $decodedPath=$null
    $rawPath=$null
    if($begin.encoding -ceq 'base64' -or -not $outerVerified){
        $rawPath=Save-NewBytes ('transport-raw/'+$begin.name+'.payload') $outer
    }
    if($outerVerified){
        if($begin.encoding -ceq 'base64'){
            if($payload -cnotmatch '^[A-Za-z0-9+/=\n]*$'){
                $problems.Add('BASE64_ALPHABET_INVALID')
            }else{
                try{$decoded=[Convert]::FromBase64String($payload)}
                catch{$problems.Add('BASE64_DECODE_FAILED')}
                if($null -ne $decoded){
                    $decodedBytes=$decoded.Length
                    $decodedHash=Sha256 $decoded
                    if($decodedBytes -ne $begin.decoded_bytes){$problems.Add('DECODED_LENGTH_MISMATCH')}
                    if($decodedHash -cne $begin.decoded_sha256){$problems.Add('DECODED_HASH_MISMATCH')}
                    if($decodedBytes -gt 50331648){$problems.Add('ACTUAL_DECODED_BOUND')}
                }
            }
        }else{
            $decoded=$outer;$decodedBytes=$outer.Length;$decodedHash=$outerHash
        }
        if($problems.Count -eq 0){$decodedPath=Save-NewBytes $begin.name $decoded}
    }
    $row=[ordered]@{
        path=$begin.name;encoding=$begin.encoding;
        declared_transport_bytes=$begin.bytes;declared_transport_sha256=$begin.sha256;
        actual_transport_bytes=$outer.Length;actual_transport_sha256=$outerHash;
        transport_verified=$outerVerified;decoded_bytes=$decodedBytes;decoded_sha256=$decodedHash;
        declared_decoded_bytes=$begin.decoded_bytes;declared_decoded_sha256=$begin.decoded_sha256;
        decoded_file=$decodedPath;retained_transport_payload=$rawPath;
        verified=($problems.Count -eq 0);issues=@($problems.ToArray())
    }
    $script:rows.Add($row)
}
try {
    $resolvedLogPath=[IO.Path]::GetFullPath($LogPath)
    Assert-NoLinks $resolvedLogPath
    $info=Get-Item -LiteralPath $resolvedLogPath -Force
    Assert-Transport (-not $info.PSIsContainer -and $info.Length -le $MaxLogBytes) 'INPUT_LOG_BOUND_OR_TYPE'
    $root=[IO.Path]::GetFullPath($OutputDirectory).TrimEnd([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar)
    Assert-Transport ($root.Length -gt 0 -and $root -cne [IO.Path]::GetPathRoot($root).TrimEnd([IO.Path]::DirectorySeparatorChar)) 'OUTPUT_ROOT_REFUSED'
    Assert-NoLinks $root
    if(Test-Path -LiteralPath $root){
        Assert-Transport ((Get-Item -LiteralPath $root).PSIsContainer) 'OUTPUT_NOT_DIRECTORY'
        Assert-Transport (@(Get-ChildItem -LiteralPath $root -Force).Count -eq 0) 'OUTPUT_NOT_EMPTY'
    }else{[IO.Directory]::CreateDirectory($root)|Out-Null}
    $outputPrepared=$true
    $raw=[IO.File]::ReadAllBytes($resolvedLogPath)
    Assert-Transport ($raw.Length -le $MaxLogBytes) 'INPUT_LOG_CHANGED_OR_OVERSIZED'
    $inputBytes=$raw.Length;$inputHash=Sha256 $raw
    $log=$utf8.GetString($raw)
    foreach($physical in $log.Split([char]10)){
        ++$lineNumber
        # Exactly one provider prefix. Genuine payload BOM/timestamps remain.
        $line=[regex]::Replace($physical,'^\uFEFF?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?','')
        if($line.StartsWith('SS_CI_EVIDENCE_BEGIN ',[StringComparison]::Ordinal)){
            Assert-Transport ($null -eq $current) 'NESTED_FRAME'
            $begin=Read-Marker $line.Substring(21) $false
            Assert-Transport ($seen.Add($begin.name)) 'DUPLICATE_FRAME_PATH'
            $current=@{begin=$begin;lines=[Collections.Generic.List[string]]::new();characters=[long]0}
        }elseif($line.StartsWith('SS_CI_EVIDENCE_END ',[StringComparison]::Ordinal)){
            Assert-Transport ($null -ne $current) 'ORPHAN_FRAME_END'
            $end=Read-Marker $line.Substring(19) $true
            Complete-Frame $current $end
            $current=$null
        }elseif($line.StartsWith('SS_CI_EVIDENCE_MISSING ',[StringComparison]::Ordinal)){
            Assert-Transport ($null -eq $current) 'MISSING_MARKER_INSIDE_FRAME'
            $issues.Add('PRODUCER_REPORTED_MISSING_EVIDENCE')
        }elseif($null -ne $current){
            $current.characters+=$line.Length+1
            Assert-Transport ($current.characters -le $MaxPayloadBytes+1) 'PAYLOAD_CHARACTER_BOUND'
            $current.lines.Add($line)
        }
    }
    Assert-Transport ($null -eq $current) 'UNTERMINATED_FRAME'
    Assert-Transport ($rows.Count -gt 0) 'NO_FRAMES'
    if($ExpectedFrameCount -gt 0){Assert-Transport ($rows.Count -eq $ExpectedFrameCount) 'FRAME_COUNT_MISMATCH'}
}catch{
    $code='RECONSTRUCTION_IO_OR_DATA_ERROR'
    if($_.Exception.Data.Contains('TransportCode')){$code=[string]$_.Exception.Data['TransportCode']}
    $issues.Add($code)
}
$validFrames=@($rows|Where-Object{$_.verified}).Count
$verified=($issues.Count -eq 0 -and $rows.Count -gt 0 -and $validFrames -eq $rows.Count)
$record=[ordered]@{
    scope='Provider-decoded log transport integrity only; not workload, test or release acceptance';
    reconstructed_utc=[DateTime]::UtcNow.ToString('o');
    input_log_bytes=$inputBytes;input_log_sha256=$inputHash;
    provider_prefix_convention='Strip exactly one optional-BOM UTC prefix per physical LF-delimited record; preserve payload bytes';
    expected_frame_count=$ExpectedFrameCount;frame_count=$rows.Count;verified_frames=$validFrames;
    transport_verified=$verified;issues=@($issues.ToArray());frames=@($rows.ToArray())
}
if($outputPrepared){
    try{
        $report=$utf8.GetBytes(($record|ConvertTo-Json -Depth 20)+[char]10)
        $null=Save-NewBytes 'transport-reconstruction.json' $report
    }catch{
        Write-Error 'Could not save the new reconstruction report; input was not changed.'
        exit 1
    }
}
[ordered]@{scope='transport only';frames=$rows.Count;verified=$validFrames;passed=$verified;issues=@($issues.ToArray())}|ConvertTo-Json -Compress
if($verified){exit 0}else{exit 1}

