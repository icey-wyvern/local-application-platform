# Pure fixture tests. No Hyper-V, WSL mount, conversion or networking commands run.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\prepare-ubuntu-hyperv-image.ps1"

function Assert-Fails {
    param([scriptblock] $Action, [string] $Message)
    $failed = $false
    try { & $Action } catch { $failed = $true }
    if (-not $failed) { throw "Expected failure: $Message" }
}

$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('lap-image-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    $archive = Join-Path $testRoot 'archive'
    $sums = Join-Path $testRoot 'SHA256SUMS'
    Set-Content -LiteralPath $archive -Value 'fixture' -Encoding ASCII
    $hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $sums -Value "$hash *archive" -Encoding ASCII
    Assert-ArchiveChecksum $archive $sums 'archive' $hash
    Assert-Fails { Assert-ArchiveChecksum $archive $sums 'missing' $hash } 'missing manifest entry'
    Assert-Fails { Assert-ArchiveChecksum $archive $sums 'archive' ('0' * 64) } 'wrong repository pin'
    Add-Content -LiteralPath $sums -Value "$hash *archive"
    Assert-Fails { Assert-ArchiveChecksum $archive $sums 'archive' $hash } 'duplicate manifest entries'
    Set-Content -LiteralPath $sums -Value "$hash *archive" -Encoding ASCII
    Add-Content -LiteralPath $archive -Value 'corruption'
    Assert-Fails { Assert-ArchiveChecksum $archive $sums 'archive' $hash } 'corrupt archive'

    $script:fixtureDisk = [pscustomobject]@{ VhdFormat = 'VHDX'; VhdType = 'Dynamic'; Size = 60GB; Attached = $false }
    function Get-VHD { param([string] $Path) return $script:fixtureDisk }
    function Test-VHD { param([string] $Path) return $true }
    $receipt = @{
        recipe = 2; archive_sha256 = $hash; capacity_bytes = 60GB
        image_sha256 = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $receipt | ConvertTo-Json | Set-Content -LiteralPath "$archive.prepared.json"
    Assert-PreparedImage $archive 60GB $hash
    $script:fixtureDisk.Attached = $true
    Assert-Fails { Assert-PreparedImage $archive 60GB $hash } 'attached parent'
    $script:fixtureDisk.Attached = $false
    $script:fixtureDisk.Size = 30GB
    Assert-Fails { Assert-PreparedImage $archive 60GB $hash } 'wrong capacity'
    $script:fixtureDisk.Size = 60GB
    function Test-VHD { param([string] $Path) return $false }
    Assert-Fails { Assert-PreparedImage $archive 60GB $hash } 'invalid VHD chain'
    Add-Content -LiteralPath $archive -Value 'tampered'
    Assert-Fails { Assert-PreparedImage $archive 60GB $hash } 'modified parent'
    Write-Output 'Image preparation fixture checks passed (10 cases; no host mutation).'
} finally {
    $resolved = [IO.Path]::GetFullPath($testRoot)
    $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($resolved.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $resolved) -like 'lap-image-test-*') {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
