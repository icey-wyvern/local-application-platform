[CmdletBinding()]
param(
    [switch]$EnsureRoot,
    [switch]$CreateIntermediate,
    [switch]$TrustRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not ($EnsureRoot -or $CreateIntermediate -or $TrustRoot)) {
    throw 'Specify -EnsureRoot, -CreateIntermediate or -TrustRoot.'
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$pkiDirectory = Join-Path $repositoryRoot '.generated\pki'
$rootKey = Join-Path $pkiDirectory 'lab-root-ca.key'
$rootCertificate = Join-Path $pkiDirectory 'lab-root-ca.crt'
$intermediateKey = Join-Path $pkiDirectory '.cluster-intermediate-ca.key.pending'
$intermediateRequest = Join-Path $pkiDirectory '.cluster-intermediate-ca.csr.pending'
$intermediateCertificate = Join-Path $pkiDirectory 'cluster-intermediate-ca.crt'
$opensslCandidates = @(
    'C:\Program Files\Git\usr\bin\openssl.exe',
    'C:\Program Files\Git\mingw64\bin\openssl.exe'
)
$openssl = $opensslCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $openssl) {
    throw 'Windows OpenSSL was not found. Install Git for Windows or OpenSSL before preparing PKI.'
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
    if (Test-Path -LiteralPath $Path -PathType Container) {
        $grants = @("${identity}:(OI)(CI)F", 'SYSTEM:(OI)(CI)F', 'Administrators:(OI)(CI)F')
    } else {
        $grants = @("${identity}:F", 'SYSTEM:F', 'Administrators:F')
    }
    & icacls.exe $Path /inheritance:r /grant:r $grants | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict permissions on $Path."
    }
}

function Assert-RootCa {
    if ((Test-Path -LiteralPath $rootKey) -ne (Test-Path -LiteralPath $rootCertificate)) {
        throw 'Root CA state is incomplete; refusing to generate or overwrite either file.'
    }
    if (-not (Test-Path -LiteralPath $rootKey)) {
        return $false
    }

    Invoke-OpenSsl @('x509', '-in', $rootCertificate, '-noout', '-checkend', '2592000')
    Invoke-OpenSsl @('verify', '-CAfile', $rootCertificate, $rootCertificate)
    $certificateText = (& $openssl x509 -in $rootCertificate -noout -text) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $certificateText -notmatch 'CA:TRUE' -or
        $certificateText -notmatch 'Certificate Sign') {
        throw 'The existing root certificate is not a valid signing CA.'
    }
    $keyPublic = (& $openssl pkey -in $rootKey -pubout 2>$null) -join "`n"
    $certificatePublic = (& $openssl x509 -in $rootCertificate -pubkey -noout 2>$null) -join "`n"
    if (-not $keyPublic -or $keyPublic -ne $certificatePublic) {
        throw 'The existing root private key does not match the root certificate.'
    }
    Set-PrivateAcl -Path $rootKey
    return $true
}

New-Item -ItemType Directory -Path $pkiDirectory -Force | Out-Null
Set-PrivateAcl -Path $pkiDirectory

$rootExists = Assert-RootCa
if (-not $rootExists) {
    if (-not $EnsureRoot) {
        throw 'The lab root CA does not exist; run this script with -EnsureRoot explicitly.'
    }
    $temporaryDirectory = Join-Path $pkiDirectory ('.root-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
    Set-PrivateAcl -Path $temporaryDirectory
    try {
        $configuration = Join-Path $temporaryDirectory 'root.cnf'
        @'
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_root
[dn]
CN = Local Application Platform Lab Root CA
O = Local Application Platform
[v3_root]
basicConstraints = critical,CA:TRUE,pathlen:1
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
'@ | Set-Content -LiteralPath $configuration -Encoding ascii
        $temporaryKey = Join-Path $temporaryDirectory 'root.key'
        $temporaryCertificate = Join-Path $temporaryDirectory 'root.crt'
        Invoke-OpenSsl @(
            'req', '-x509', '-newkey', 'rsa:4096', '-nodes', '-sha384',
            '-days', '1825', '-config', $configuration,
            '-keyout', $temporaryKey, '-out', $temporaryCertificate
        )
        Set-PrivateAcl -Path $temporaryKey
        Move-Item -LiteralPath $temporaryKey -Destination $rootKey
        Move-Item -LiteralPath $temporaryCertificate -Destination $rootCertificate
        Write-Output "Created the five-year lab root CA at $rootCertificate."
    }
    finally {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not (Assert-RootCa)) {
        throw 'Root CA validation failed after creation.'
    }
} else {
    Write-Output "Reusing the existing lab root CA at $rootCertificate."
}

if ($TrustRoot) {
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($rootCertificate)
    $trusted = Get-ChildItem Cert:\CurrentUser\Root | Where-Object Thumbprint -eq $certificate.Thumbprint
    if (-not $trusted) {
        # Windows requires confirmation before adding a private root. The action
        # is explicit (-TrustRoot), so acknowledge only certutil's own dialog.
        $process = Start-Process -FilePath certutil.exe -ArgumentList @(
            '-user', '-f', '-addstore', 'Root', $rootCertificate
        ) -PassThru -WindowStyle Hidden
        $shell = New-Object -ComObject WScript.Shell
        $confirmed = $false
        for ($attempt = 0; $attempt -lt 50; $attempt++) {
            Start-Sleep -Milliseconds 100
            if ($shell.AppActivate($process.Id)) {
                $shell.SendKeys('%y')
                $confirmed = $true
                break
            }
        }
        if (-not $confirmed -or -not $process.WaitForExit(10000) -or $process.ExitCode -ne 0) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            throw 'Windows did not confirm installation of the lab root CA.'
        }
        $trusted = Get-ChildItem Cert:\CurrentUser\Root |
            Where-Object Thumbprint -eq $certificate.Thumbprint
        if (-not $trusted) {
            throw 'The lab root CA is absent from the Windows trust store after import.'
        }
        Write-Output 'Installed the lab root CA in the Windows CurrentUser Root store.'
    } else {
        Write-Output 'The Windows CurrentUser Root store already trusts the lab root CA.'
    }
}

if ($CreateIntermediate) {
    if (Test-Path -LiteralPath $intermediateCertificate) {
        throw 'The cluster intermediate certificate already exists; refusing silent replacement.'
    }
    if ((Test-Path -LiteralPath $intermediateKey) -or (Test-Path -LiteralPath $intermediateRequest)) {
        throw 'Pending cluster intermediate material already exists; inspect it before retrying.'
    }
    $temporaryConfiguration = Join-Path $pkiDirectory '.intermediate.cnf.pending'
    try {
        Invoke-OpenSsl @(
            'req', '-new', '-newkey', 'rsa:4096', '-nodes', '-sha384',
            '-subj', '/CN=Local Application Platform Cluster Intermediate CA/O=Local Application Platform',
            '-keyout', $intermediateKey, '-out', $intermediateRequest
        )
        Set-PrivateAcl -Path $intermediateKey
        @'
[v3_intermediate]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
'@ | Set-Content -LiteralPath $temporaryConfiguration -Encoding ascii
        $serial = (& $openssl rand -hex 16).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $serial) {
            throw 'Failed to generate the intermediate certificate serial number.'
        }
        Invoke-OpenSsl @(
            'x509', '-req', '-in', $intermediateRequest,
            '-CA', $rootCertificate, '-CAkey', $rootKey,
            '-set_serial', "0x$serial", '-days', '365', '-sha384',
            '-extfile', $temporaryConfiguration, '-extensions', 'v3_intermediate',
            '-out', $intermediateCertificate
        )
        Invoke-OpenSsl @('verify', '-CAfile', $rootCertificate, $intermediateCertificate)
        $intermediateText = (& $openssl x509 -in $intermediateCertificate -noout -text) -join "`n"
        if ($LASTEXITCODE -ne 0 -or $intermediateText -notmatch 'CA:TRUE, pathlen:0' -or
            $intermediateText -notmatch 'Certificate Sign') {
            throw 'The generated intermediate does not have the required CA constraints.'
        }
        Write-Output "Created the one-year cluster intermediate certificate at $intermediateCertificate."
    }
    catch {
        Remove-Item -LiteralPath $intermediateKey, $intermediateRequest -Force -ErrorAction SilentlyContinue
        throw
    }
    finally {
        Remove-Item -LiteralPath $temporaryConfiguration -Force -ErrorAction SilentlyContinue
    }
}
