[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pkiDirectory = Join-Path $repositoryRoot '.generated\pki'
$rootKey = Join-Path $pkiDirectory 'lab-root-ca.key'
$rootCertificate = Join-Path $pkiDirectory 'lab-root-ca.crt'
$opensslCandidates = @(
    'C:\Program Files\Git\usr\bin\openssl.exe',
    'C:\Program Files\Git\mingw64\bin\openssl.exe'
)
$openssl = $opensslCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $openssl) { throw 'Windows OpenSSL was not found.' }
if (-not (Test-Path -LiteralPath $rootKey) -or -not (Test-Path -LiteralPath $rootCertificate)) {
    throw 'The lab root CA is incomplete. Run prepare-lab-pki.ps1 -EnsureRoot first.'
}

function Invoke-OpenSsl {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & $script:openssl @Arguments
    if ($LASTEXITCODE -ne 0) { throw "OpenSSL failed with exit code $LASTEXITCODE." }
}

function Set-PrivateAcl {
    param([Parameter(Mandatory)][string]$Path)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Path /inheritance:r /grant:r "${identity}:F" 'SYSTEM:F' 'Administrators:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to restrict permissions on $Path." }
}

function Ensure-Leaf {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('server', 'client')][string]$Usage,
        [string]$DnsName
    )
    $key = Join-Path $pkiDirectory "$Name.key"
    $certificate = Join-Path $pkiDirectory "$Name.crt"
    if ((Test-Path -LiteralPath $key) -ne (Test-Path -LiteralPath $certificate)) {
        throw "Certificate state for $Name is incomplete; refusing to overwrite it."
    }
    $extendedUsage = if ($Usage -eq 'server') { 'serverAuth' } else { 'clientAuth' }
    $subjectAltName = if ($DnsName) { "subjectAltName = DNS:$DnsName" } else { '' }
    if (Test-Path -LiteralPath $certificate) {
        Invoke-OpenSsl @('verify', '-CAfile', $rootCertificate, $certificate)
        Invoke-OpenSsl @('x509', '-in', $certificate, '-noout', '-checkend', '604800')
        if ($DnsName) { Invoke-OpenSsl @('x509', '-in', $certificate, '-noout', '-checkhost', $DnsName) }
        $text = (& $openssl x509 -in $certificate -noout -text) -join "`n"
        $expected = if ($Usage -eq 'server') { 'TLS Web Server Authentication' } else { 'TLS Web Client Authentication' }
        if ($text -notmatch $expected) { throw "$Name has the wrong extended key usage." }
        $keyPublic = (& $openssl pkey -in $key -pubout 2>$null) -join "`n"
        $certificatePublic = (& $openssl x509 -in $certificate -pubkey -noout 2>$null) -join "`n"
        if (-not $keyPublic -or $keyPublic -ne $certificatePublic) {
            throw "The private key does not match the $Name certificate."
        }
        Set-PrivateAcl -Path $key
        Write-Output "Reusing the existing $Name certificate."
        return
    }
    $temporaryDirectory = Join-Path $pkiDirectory ('.build-agent-leaf-' + [guid]::NewGuid().ToString('N'))
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
CN = $Name
O = Local Application Platform
[v3_leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = $extendedUsage
$subjectAltName
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
"@ | Set-Content -LiteralPath $configuration -Encoding ascii
        Invoke-OpenSsl @('req', '-new', '-newkey', 'rsa:3072', '-nodes', '-sha384', '-config', $configuration, '-keyout', $temporaryKey, '-out', $request)
        $serial = (& $openssl rand -hex 16).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $serial) { throw 'Failed to generate certificate serial.' }
        Invoke-OpenSsl @('x509', '-req', '-in', $request, '-CA', $rootCertificate, '-CAkey', $rootKey, '-set_serial', "0x$serial", '-days', '90', '-sha384', '-extfile', $configuration, '-extensions', 'v3_leaf', '-out', $temporaryCertificate)
        Set-PrivateAcl -Path $temporaryKey
        Move-Item -LiteralPath $temporaryKey -Destination $key
        Move-Item -LiteralPath $temporaryCertificate -Destination $certificate
        Set-PrivateAcl -Path $key
        Write-Output "Created the $Name certificate."
    }
    finally {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Ensure-Leaf -Name 'build-agent-lab-test' -Usage server -DnsName 'build-agent.lab.test'
Ensure-Leaf -Name 'platform-api-build-agent-client' -Usage client
