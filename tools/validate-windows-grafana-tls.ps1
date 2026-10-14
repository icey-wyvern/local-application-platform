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
        # No callback is supplied; Windows enforces normal hostname and CA validation.
        $tls.AuthenticateAsClient($Hostname)
        $request = "GET /api/health HTTP/1.1`r`nHost: $Hostname`r`nConnection: close`r`n`r`n"
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($request)
        $tls.Write($bytes, 0, $bytes.Length)
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
    $response -notmatch '"database"\s*:\s*"ok"') {
    throw 'Windows trusted Grafana HTTPS validation failed.'
}

Write-Output "Windows trusted Grafana HTTPS validation passed for $Hostname."
