$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Distribution = 'Ubuntu-24.04'
$generatedMake = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\.generated\make\generated.mk')
$apiAddressLine = @($generatedMake | Where-Object { $_ -match '^API_VIRTUAL_IP := ' })
if ($apiAddressLine.Count -ne 1) {
    throw 'Generated configuration has no valid lab API IPv4 address.'
}
if ($apiAddressLine[0] -notmatch '^API_VIRTUAL_IP := ([0-9.]+)$') {
    throw 'Generated configuration has no valid lab API IPv4 address.'
}
$ApiAddress = $Matches[1]
$gatewayAddressLine = @($generatedMake | Where-Object { $_ -match '^ENVOY_GATEWAY_IP := ' })
if ($gatewayAddressLine.Count -ne 1 -or $gatewayAddressLine[0] -notmatch '^ENVOY_GATEWAY_IP := ([0-9.]+)$') {
    throw 'Generated configuration has no valid Envoy Gateway IPv4 address.'
}
$GatewayAddress = $Matches[1]
$platformEnvironment = Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\.generated\platform\platform.env')
$apiHostnameLine = @($platformEnvironment | Where-Object { $_ -match '^PLATFORM_API_HOST=' })
if ($apiHostnameLine.Count -ne 1 -or $apiHostnameLine[0] -notmatch '^PLATFORM_API_HOST=([a-z0-9.-]+\.lab\.test)$') {
    throw 'Generated configuration has no valid lab API hostname.'
}
$ApiHostname = $Matches[1]

function Invoke-WslRouteJson {
    param([string[]] $Arguments)
    $output = & wsl.exe -d $Distribution -- @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect WSL routes.' }
    if (-not $output -or ($output -join '').Trim() -eq '[]') { return @() }
    return @($output | ConvertFrom-Json)
}

function Test-WslLabRoute {
    param([string] $NetworkCidr)
    $defaults = @(Invoke-WslRouteJson @('ip', '-j', '-4', 'route', 'show', 'default'))
    $routes = @(Invoke-WslRouteJson @('ip', '-j', '-4', 'route', 'show', $NetworkCidr))
    if ($defaults.Count -ne 1 -or $routes.Count -ne 1) { return $false }
    return $routes[0].dst -eq $NetworkCidr -and
        $routes[0].gateway -eq $defaults[0].gateway -and
        $routes[0].dev -eq $defaults[0].dev
}

function Test-WslApi {
    $probe = 'import socket,sys; socket.create_connection((sys.argv[1],6443),3).close()'
    try { $null = & wsl.exe -d $Distribution -- python3 -c $probe $ApiAddress }
    catch { return $false }
    return $LASTEXITCODE -eq 0
}

function Test-WindowsApi {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.BeginConnect($ApiAddress, 6443, $null, $null)
        if (-not $connection.AsyncWaitHandle.WaitOne(3000)) { return $false }
        try { $client.EndConnect($connection) } catch { return $false }
        return $true
    } finally {
        $client.Dispose()
    }
}

function Ensure-WslDns {
    $scriptPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'configure-wsl-lab-dns.py')).Path
    $wslPath = & wsl.exe -d $Distribution -- wslpath -u $scriptPath.Replace('\', '/')
    if ($LASTEXITCODE -ne 0 -or -not $wslPath) { throw 'Could not locate the WSL lab DNS configuration script.' }
    & wsl.exe -d $Distribution -u root -- python3 $wslPath.Trim() $ApiAddress $ApiHostname $GatewayAddress
    if ($LASTEXITCODE -ne 0) { throw 'WSL lab DNS configuration or API hostname resolution failed.' }
}

function Invoke-ElevatedRoutingPreparation {
    $preparation = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'prepare-lab-routing.ps1')).Path
    # -File receives an absolute, repository-owned path. No shell or user input is
    # interpolated into the elevated command. Only this preparation is elevated.
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $preparation
    try {
        $process = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    } catch {
        throw 'Lab routing repair was not authorized or could not start through Windows UAC.'
    }
    if ($process.ExitCode -ne 0) {
        throw "Elevated lab routing preparation failed (exit code $($process.ExitCode))."
    }
}

function Assert-LabApiAddress {
    $address = $null
    if (-not [System.Net.IPAddress]::TryParse($ApiAddress, [ref] $address) -or
        $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'The generated lab API address must be an IPv4 address.'
    }
}

function Invoke-LabRoutingStartup {
    Assert-LabApiAddress
    $configPath = Join-Path $PSScriptRoot '..\.generated\terraform\lab.auto.tfvars.json'
    $config = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    $networkCidr = [string] $config.network_cidr
    if ($networkCidr -ne '10.20.0.0/24') {
        throw 'Review routing bootstrap before changing the ADR 0002 network.'
    }
    if (Test-WslApi) {
        Ensure-WslDns
        Write-Output 'WSL lab routing and Kubernetes API are reachable.'
        return
    }
    if (-not (Test-WindowsApi)) {
        throw 'The lab API is unreachable from Windows and WSL. Check the lab.'
    }
    Write-Output 'WSL lab routing needs repair; requesting Windows UAC consent.'
    Invoke-ElevatedRoutingPreparation
    if (-not (Test-WslLabRoute $networkCidr)) {
        throw 'The WSL lab route is still missing after elevated routing preparation.'
    }
    if (-not (Test-WslApi)) {
        throw 'The WSL lab route was restored, but the lab API is still unreachable on TCP 6443.'
    }
    Ensure-WslDns
    Write-Output 'WSL lab route restored and Kubernetes API is reachable.'
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-LabRoutingStartup }
