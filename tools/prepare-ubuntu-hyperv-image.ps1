[CmdletBinding()]
param(
    [string] $ConfigPath,
    [string] $WorkRoot,
    [string] $Distribution = "Ubuntu-24.04",
    [switch] $VerifyOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# PowerShell evaluates parameter defaults in the caller's scope, where
# $PSScriptRoot can be empty when this script is launched through WSL.
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $PSScriptRoot '..\.generated\terraform\lab.auto.tfvars.json'
}
if ([string]::IsNullOrWhiteSpace($WorkRoot)) {
    $WorkRoot = Join-Path $PSScriptRoot '..\.local-tools\ubuntu-image'
}

function Assert-Administrator {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run image preparation/verification in elevated Windows PowerShell.'
    }
}

function Invoke-Linux {
    param([string[]] $Arguments)
    $result = & wsl.exe -d $Distribution -u root -- @Arguments
    if ($LASTEXITCODE -ne 0) { throw "WSL command failed (exit $LASTEXITCODE)." }
    return $result
}

function Assert-ArchiveChecksum {
    param([string] $ArchivePath, [string] $SumsPath, [string] $ArchiveName, [string] $PinnedHash)
    $entries = @(Get-Content -LiteralPath $SumsPath | Where-Object {
        $_ -match ('^[0-9a-fA-F]{64}\s+\*?' + [regex]::Escape($ArchiveName) + '$')
    })
    if ($entries.Count -ne 1) { throw 'Expected exactly one Canonical checksum entry.' }
    $canonical = ($entries[0] -split '\s+')[0].ToLowerInvariant()
    if ($canonical -ne $PinnedHash) { throw 'Canonical checksum differs from the repository pin.' }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash.ToLowerInvariant() -ne $PinnedHash) {
        throw 'Archive checksum mismatch; no extraction or conversion performed.'
    }
}

function Assert-PreparedImage {
    param([string] $Path, [long] $Capacity, [string] $ArchiveHash)
    $receipt = Get-Content -Raw -LiteralPath "$Path.prepared.json" | ConvertFrom-Json
    if ($receipt.recipe -ne 2 -or $receipt.archive_sha256 -ne $ArchiveHash -or
        $receipt.capacity_bytes -ne $Capacity -or
        $receipt.image_sha256 -ne (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()) {
        throw 'Prepared image receipt/hash mismatch. Never repair or overwrite a published parent.'
    }
    $disk = Get-VHD -Path $Path
    if ($disk.VhdFormat -ne 'VHDX' -or $disk.VhdType -ne 'Dynamic' -or
        $disk.Size -ne $Capacity -or $disk.Attached) {
        throw 'Prepared image must be a detached dynamic VHDX of the configured capacity.'
    }
    if (-not (Test-VHD -Path $Path)) { throw 'Test-VHD rejected the prepared image.' }
}

function Invoke-ImagePreparation {
    Assert-Administrator
    $config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
    $archiveUrl = [string] $config.base_image_source
    if ($archiveUrl -notmatch '^https://cloud-images\.ubuntu\.com/releases/noble/release-[0-9]{8}/ubuntu-24\.04-server-cloudimg-amd64-azure\.vhd\.tar\.gz$') {
        throw 'Expected a dated Canonical noble Azure VHD archive URL.'
    }
    $capacity = [long] $config.base_image_capacity_gib * 1GB
    if ($capacity -ne 60GB) { throw 'ADR 0002 requires a 60 GiB parent.' }
    $destination = Join-Path $config.vhd_root_path $config.base_image_name
    if (Test-Path -LiteralPath $destination) {
        Assert-PreparedImage $destination $capacity $config.base_image_sha256
        Write-Output "Verified existing immutable prepared image: $destination"
        return
    }
    if ($VerifyOnly) { throw 'Prepared image is missing; run elevated preparation first.' }
    if (Test-Path -LiteralPath "$destination.prepared.json") { throw 'Orphan receipt requires investigation.' }

    $work = [IO.Path]::GetFullPath($WorkRoot)
    New-Item -ItemType Directory -Force -Path $work, $config.vhd_root_path, $config.seed_iso_root_path | Out-Null
    $archiveName = ($archiveUrl -split '/')[-1]
    $archive = Join-Path $work $archiveName
    $sums = Join-Path $work 'SHA256SUMS'
    Invoke-WebRequest -UseBasicParsing -Uri ($archiveUrl -replace '[^/]+$', 'SHA256SUMS') -OutFile $sums
    if (-not (Test-Path -LiteralPath $archive)) {
        $download = "$archive.download"
        Invoke-WebRequest -UseBasicParsing -Uri $archiveUrl -OutFile $download
        Assert-ArchiveChecksum $download $sums $archiveName $config.base_image_sha256
        Move-Item -LiteralPath $download -Destination $archive
    }
    Assert-ArchiveChecksum $archive $sums $archiveName $config.base_image_sha256

    $runId = [guid]::NewGuid().ToString('N')
    $extract = Join-Path $work "extract-$runId"
    $stage = Join-Path $config.vhd_root_path "prepare-$runId.vhdx"
    New-Item -ItemType Directory -Path $extract | Out-Null
    $attached = $false
    try {
        $members = @(& tar.exe -tzf $archive)
        if ($LASTEXITCODE -ne 0 -or $members.Count -ne 1 -or $members[0] -ne 'livecd.ubuntu-cpc.azure.vhd') {
            throw 'Unexpected archive contents; refusing extraction.'
        }
        & tar.exe -xzf $archive -C $extract
        if ($LASTEXITCODE -ne 0) { throw 'Archive extraction failed.' }
        $raw = Join-Path $extract $members[0]
        if ((Get-Item -LiteralPath $raw).Attributes -band [IO.FileAttributes]::SparseFile) {
            & fsutil.exe sparse setflag $raw 0
            if ($LASTEXITCODE -ne 0) { throw 'Clearing source sparse attribute failed.' }
        }
        if ((Get-Item -LiteralPath $raw).Attributes -band [IO.FileAttributes]::SparseFile) {
            throw 'Source remains sparse; refusing conversion.'
        }
        $source = Get-VHD -Path $raw
        if ($source.VhdFormat -ne 'VHD' -or $source.Size -ne 32213303296 -or $source.Size -ge $capacity) {
            throw 'Unexpected Canonical source format/capacity; review the image pin.'
        }
        Convert-VHD -Path $raw -DestinationPath $stage -VHDType Dynamic
        Resize-VHD -Path $stage -SizeBytes $capacity
        $before = @(Invoke-Linux @('lsblk', '-dnro', 'NAME'))
        & wsl.exe --mount --vhd $stage --bare
        if ($LASTEXITCODE -ne 0) { throw 'WSL image attachment failed.' }
        $attached = $true
        $after = @(Invoke-Linux @('lsblk', '-dnro', 'NAME'))
        $newDevices = @($after | Where-Object { $_ -notin $before })
        if ($newDevices.Count -ne 1 -or $newDevices[0] -notmatch '^[a-zA-Z0-9]+$') {
            throw 'Cannot uniquely identify the newly attached disk; refusing to mount.'
        }
        # wsl.exe reparses command arguments through a Linux shell, so protect
        # Windows backslashes while wslpath converts the helper location.
        $helperWindowsPath = "$PSScriptRoot\prepare-nocloud-root.sh"
        $helper = (Invoke-Linux @('wslpath', '-u', "'$helperWindowsPath'")).Trim()
        Invoke-Linux @('bash', $helper, "/dev/$($newDevices[0])")
        & wsl.exe --unmount $stage
        if ($LASTEXITCODE -ne 0) { throw 'WSL detach failed; image will not be published.' }
        $attached = $false
        $converted = Get-VHD -Path $stage
        if ($converted.VhdFormat -ne 'VHDX' -or $converted.VhdType -ne 'Dynamic' -or
            $converted.Size -ne $capacity -or $converted.Attached -or -not (Test-VHD -Path $stage)) {
            throw 'Converted image verification failed.'
        }
        $receipt = @{
            recipe = 2
            archive_sha256 = $config.base_image_sha256
            capacity_bytes = $capacity
            image_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $stage).Hash.ToLowerInvariant()
        }
        $receipt | ConvertTo-Json | Set-Content -LiteralPath "$stage.prepared.json" -Encoding ASCII
        Move-Item -LiteralPath $stage -Destination $destination
        Move-Item -LiteralPath "$stage.prepared.json" -Destination "$destination.prepared.json"
        Assert-PreparedImage $destination $capacity $config.base_image_sha256
        Write-Output "Prepared and verified: $destination"
    }
    finally {
        if ($attached) {
            & wsl.exe --unmount $stage
            if ($LASTEXITCODE -ne 0) { throw "Detach failed: $stage. Resolve attachment before retrying." }
        }
        $resolvedExtract = [IO.Path]::GetFullPath($extract)
        if ($resolvedExtract.StartsWith($work.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase) -and
            (Split-Path -Leaf $resolvedExtract) -eq "extract-$runId") {
            Remove-Item -LiteralPath $resolvedExtract -Recurse -Force
        }
        # Failed staged images remain for diagnosis; never auto-delete mounted disks.
    }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-ImagePreparation }
