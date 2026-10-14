[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Address,
    [Parameter(Mandatory)][string]$DnsServer,
    [Parameter(Mandatory)][string]$Hostname
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$answers = @(Resolve-DnsName -Type A -Name $Hostname -Server $DnsServer |
    Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress)
if ($Address -notin $answers) {
    throw "$Hostname did not resolve to $Address through $DnsServer."
}

function Read-Response {
    param([Parameter(Mandatory)][System.IO.Stream]$Stream)
    $buffer = New-Object byte[] 4096
    $memory = New-Object System.IO.MemoryStream
    do {
        $count = $Stream.Read($buffer, 0, $buffer.Length)
        if ($count -gt 0) {
            $memory.Write($buffer, 0, $count)
        }
    } while ($count -gt 0)
    return [System.Text.Encoding]::UTF8.GetString($memory.ToArray())
}

$client = [System.Net.Sockets.TcpClient]::new()
try {
    $client.Connect($Address, 443)
    $tls = [System.Net.Security.SslStream]::new($client.GetStream(), $false)
    try {
        # No validation callback is supplied: Windows performs its normal
        # hostname, chain, validity and trust-store validation.
        $tls.AuthenticateAsClient($Hostname)
        $request = "GET / HTTP/1.1`r`nHost: $Hostname`r`nConnection: close`r`n`r`n"
        $requestBytes = [System.Text.Encoding]::ASCII.GetBytes($request)
        $tls.Write($requestBytes, 0, $requestBytes.Length)
        $response = Read-Response -Stream $tls
    }
    finally {
        $tls.Dispose()
    }
}
finally {
    $client.Dispose()
}

if ($response -notmatch '^HTTP/[0-9.]+ 200' -or
    $response -notmatch '(?m)^tls-smoke-[a-z0-9-]+\r?$') {
    throw 'Windows HTTPS validation did not return a healthy smoke backend.'
}

$redirectClient = [System.Net.Sockets.TcpClient]::new()
try {
    $redirectClient.Connect($Address, 80)
    $stream = $redirectClient.GetStream()
    $request = "HEAD / HTTP/1.1`r`nHost: $Hostname`r`nConnection: close`r`n`r`n"
    $requestBytes = [System.Text.Encoding]::ASCII.GetBytes($request)
    $stream.Write($requestBytes, 0, $requestBytes.Length)
    $redirect = Read-Response -Stream $stream
}
finally {
    $redirectClient.Dispose()
}

if ($redirect -notmatch '^HTTP/[0-9.]+ 308' -or
    $redirect -notmatch "(?im)^location: https://$([regex]::Escape($Hostname))/\r?$") {
    throw 'Windows HTTP validation did not receive the expected HTTPS redirect.'
}

Write-Output "Windows trusted HTTPS and HTTP redirect validation passed for $Hostname."
