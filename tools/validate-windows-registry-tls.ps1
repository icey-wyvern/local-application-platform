[CmdletBinding()]
param(
    [string]$Hostname,
    [string]$Address,
    [string]$DnsServer,
    [int]$Port = 5000,
    [switch]$ParseOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($ParseOnly) {
    Write-Output 'Windows registry TLS validator parsed successfully.'
    exit 0
}
if (-not $Hostname -or -not $Address -or -not $DnsServer) {
    throw 'Hostname, Address and DnsServer are required.'
}

$answers = @(Resolve-DnsName -Type A -Name $Hostname -Server $DnsServer |
    Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress)
if ($Address -notin $answers) {
    throw "$Hostname did not resolve to $Address through $DnsServer."
}

$client = [System.Net.Sockets.TcpClient]::new()
try {
    $client.Connect($Address, $Port)
    $tls = [System.Net.Security.SslStream]::new($client.GetStream(), $false)
    try {
        # No validation callback: Windows enforces the trusted CA and hostname.
        $tls.AuthenticateAsClient($Hostname)
        $request = "GET /v2/ HTTP/1.1`r`nHost: ${Hostname}:$Port`r`nConnection: close`r`n`r`n"
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($request)
        $tls.Write($bytes, 0, $bytes.Length)
        $reader = [System.IO.StreamReader]::new($tls)
        try { $response = $reader.ReadToEnd() } finally { $reader.Dispose() }
    }
    finally {
        $tls.Dispose()
    }
}
finally {
    $client.Dispose()
}

if ($response -notmatch '^HTTP/[0-9.]+ 401' -or $response -notmatch '(?im)^Www-Authenticate:\s*Basic') {
    throw 'Expected the registry Basic authentication HTTP 401 challenge.'
}
Write-Output "Windows trusted TLS and authoritative DNS passed for https://${Hostname}:$Port."
