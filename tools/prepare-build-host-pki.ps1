[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('registry.lab.test')]
    [string]$Hostname
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pkiDirectory = Join-Path $repositoryRoot '.generated\pki'
$rootKey = Join-Path $pkiDirectory 'lab-root-ca.key'
$rootCertificate = Join-Path $pkiDirectory 'lab-root-ca.crt'
$leafName = $Hostname.Replace('.', '-')
$leafKey = Join-Path $pkiDirectory "$leafName.key"
$leafCertificate = Join-Path $pkiDirectory "$leafName.crt"
$opensslCandidates = @(
    'C:\Program Files\Git\usr\bin\openssl.exe',
    'C:\Program Files\Git\mingw64\bin\openssl.exe'
)
$openssl = $opensslCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $openssl) {
    throw 'Windows OpenSSL was not found. Install Git for Windows or OpenSSL.'
}
if (-not (Test-Path -LiteralPath $rootKey) -or -not (Test-Path -LiteralPath $rootCertificate)) {
    throw 'The lab root CA is incomplete. Run prepare-lab-pki.ps1 -EnsureRoot first.'
}
if ((Test-Path -LiteralPath $leafKey) -ne (Test-Path -LiteralPath $leafCertificate)) {
    throw "Certificate state for $Hostname is incomplete; refusing to overwrite it."
}

function Invoke-OpenSsl {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & $script:openssl @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "OpenSSL failed with exit code $LASTEXITCODE."
    }
}

function Set-PrivateAcl {
    param([Parameter(Mandatory)][string]$Path)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Path /inheritance:r /grant:r "${identity}:F" 'SYSTEM:F' 'Administrators:F' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict permissions on $Path."
    }
}

function Assert-LeafCertificate {
    Invoke-OpenSsl @('verify', '-CAfile', $rootCertificate, $leafCertificate)
    Invoke-OpenSsl @('x509', '-in', $leafCertificate, '-noout', '-checkend', '604800')
    Invoke-OpenSsl @('x509', '-in', $leafCertificate, '-noout', '-checkhost', $Hostname)
    $keyPublic = (& $openssl pkey -in $leafKey -pubout 2>$null) -join "`n"
    $certificatePublic = (& $openssl x509 -in $leafCertificate -pubkey -noout 2>$null) -join "`n"
    if (-not $keyPublic -or $keyPublic -ne $certificatePublic) {
        throw "The private key does not match the $Hostname certificate."
    }
    $certificateText = (& $openssl x509 -in $leafCertificate -noout -text) -join "`n"
    if ($certificateText -notmatch 'TLS Web Server Authentication' -or
        $certificateText -notmatch "DNS:$([regex]::Escape($Hostname))") {
        throw "The $Hostname certificate lacks the required server identity or usage."
    }
    Set-PrivateAcl -Path $leafKey
}

if (Test-Path -LiteralPath $leafCertificate) {
    Assert-LeafCertificate
    Write-Output "Reusing the existing host-issued certificate for $Hostname."
    exit 0
}

$temporaryDirectory = Join-Path $pkiDirectory ('.host-leaf-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
try {
    $configuration = Join-Path $temporaryDirectory 'leaf.cnf'
    $request = Join-Path $temporaryDirectory 'leaf.csr'
    $temporaryKey = Join-Path $temporaryDirectory 'leaf.key'
    $temporaryCertificate = Join-Path $temporaryDirectory 'leaf.crt'
    @"
[req]
distinguished_name = dn
prompt = no
[dn]
CN = $Hostname
O = Local Application Platform
[v3_server]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:$Hostname
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
"@ | Set-Content -LiteralPath $configuration -Encoding ascii
    Invoke-OpenSsl @(
        'req', '-new', '-newkey', 'rsa:3072', '-nodes', '-sha384',
        '-config', $configuration, '-keyout', $temporaryKey, '-out', $request
    )
    $serial = (& $openssl rand -hex 16).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $serial) {
        throw 'Failed to generate a certificate serial number.'
    }
    Invoke-OpenSsl @(
        'x509', '-req', '-in', $request, '-CA', $rootCertificate, '-CAkey', $rootKey,
        '-set_serial', "0x$serial", '-days', '90', '-sha384', '-extfile', $configuration,
        '-extensions', 'v3_server', '-out', $temporaryCertificate
    )
    Set-PrivateAcl -Path $temporaryKey
    Move-Item -LiteralPath $temporaryKey -Destination $leafKey
    Move-Item -LiteralPath $temporaryCertificate -Destination $leafCertificate
    Assert-LeafCertificate
    Write-Output "Created a 90-day host-issued certificate for $Hostname."
}
finally {
    Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
}
