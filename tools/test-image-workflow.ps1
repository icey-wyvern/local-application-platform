# In-process mocks only. Every host-mutating command is replaced below.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\prepare-ubuntu-hyperv-image.ps1"
function Assert-Administrator {}
function Invoke-WebRequest {
    param($Uri, $OutFile, [switch] $UseBasicParsing)
    Set-Content -LiteralPath $OutFile -Value "$script:archiveHash *ubuntu-24.04-server-cloudimg-amd64-azure.vhd.tar.gz" -Encoding ASCII
}
function tar.exe {
    if ($args[0] -eq '-tzf') { 'livecd.ubuntu-cpc.azure.vhd' }
    else { Set-Content -LiteralPath (Join-Path $args[3] 'livecd.ubuntu-cpc.azure.vhd') -Value 'raw' }
    $global:LASTEXITCODE = 0
}
function Get-VHD {
    param($Path)
    if ($Path.EndsWith('.vhd')) { return [pscustomobject]@{ VhdFormat = 'VHD'; Size = 32213303296 } }
    return [pscustomobject]@{ VhdFormat = 'VHDX'; VhdType = 'Dynamic'; Size = 60GB; Attached = $false }
}
function Convert-VHD {
    param($Path, $DestinationPath, $VHDType)
    $script:conversions++
    if ($script:failure -eq 'conversion') { throw 'Mock conversion failure' }
    Set-Content -LiteralPath $DestinationPath -Value 'converted fixture' -Encoding ASCII
}
function Resize-VHD { param($Path, $SizeBytes) }
function Test-VHD { param($Path) return $true }
function wsl.exe {
    if ($args[0] -eq '--mount') {
        $script:mounted = $true
        $global:LASTEXITCODE = 0
    } elseif ($args[0] -eq '--unmount') {
        $script:detaches++
        if ($script:failure -eq 'detach') { $global:LASTEXITCODE = 1 }
        else { $script:mounted = $false; $global:LASTEXITCODE = 0 }
    } else { throw 'Unexpected mocked WSL invocation' }
}
function Invoke-Linux {
    param([string[]] $Arguments)
    switch ($Arguments[0]) {
        'lsblk' { 'sda'; if ($script:mounted) { 'sdb' } }
        'wslpath' { '/fixture/helper.sh' }
        'bash' { if ($script:failure -eq 'mount') { throw 'Mock Linux mount/verification failure' } }
        default { throw 'Unexpected Linux invocation' }
    }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('lap-workflow-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    foreach ($scenario in @('success', 'conversion', 'mount', 'detach')) {
        $script:failure = $scenario
        $script:conversions = 0
        $script:detaches = 0
        $script:mounted = $false
        $WorkRoot = Join-Path $testRoot $scenario
        New-Item -ItemType Directory -Path $WorkRoot | Out-Null
        $archive = Join-Path $WorkRoot 'ubuntu-24.04-server-cloudimg-amd64-azure.vhd.tar.gz'
        Set-Content -LiteralPath $archive -Value 'archive fixture' -Encoding ASCII
        $script:archiveHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        $ConfigPath = Join-Path $WorkRoot 'config.json'
        @{
            base_image_source = 'https://cloud-images.ubuntu.com/releases/noble/release-20260826/ubuntu-24.04-server-cloudimg-amd64-azure.vhd.tar.gz'
            base_image_sha256 = $script:archiveHash
            base_image_capacity_gib = 60
            base_image_name = 'base.vhdx'
            vhd_root_path = $WorkRoot
            seed_iso_root_path = $WorkRoot
        } | ConvertTo-Json | Set-Content -LiteralPath $ConfigPath
        $failed = $false
        try { Invoke-ImagePreparation | Out-Null } catch { $failed = $true }
        $destination = Join-Path $WorkRoot 'base.vhdx'
        if ($scenario -eq 'success') {
            if ($failed -or -not (Test-Path -LiteralPath "$destination.prepared.json")) { throw 'Successful workflow did not publish a receipt.' }
            Invoke-ImagePreparation | Out-Null
            if ($script:conversions -ne 1) { throw 'Repeated preparation must not reconvert a published parent.' }
        } else {
            if (-not $failed -or (Test-Path -LiteralPath $destination) -or
                (Test-Path -LiteralPath "$destination.prepared.json")) { throw "Failed $scenario workflow published an image." }
        }
        if ($scenario -in @('mount', 'detach') -and $script:detaches -lt 1) { throw 'Failure did not attempt detach.' }
    }
    Write-Output 'Image workflow: success/idempotence, conversion, mount and detach failure mocks passed.'
} finally {
    $resolved = [IO.Path]::GetFullPath($testRoot)
    $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolved.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolved) -like 'lap-workflow-test-*') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
