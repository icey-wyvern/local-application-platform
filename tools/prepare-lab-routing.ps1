[CmdletBinding()]
param(
    [string] $ConfigPath,
    [string] $Distribution = 'Ubuntu-24.04'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Resolve repository-relative input here because parameter defaults are
# evaluated in the caller's scope when PowerShell is invoked through WSL.
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $PSScriptRoot '..\.generated\terraform\lab.auto.tfvars.json'
}

function Invoke-WslJson {
    param([string[]] $Arguments)
    $output = & wsl.exe -d $Distribution -- @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'Cannot discover WSL networking.' }
    return ($output | ConvertFrom-Json)
}

function Set-ScopedFirewallRule {
    param([string] $Name, [string] $Interface, [string] $Source, [string] $Destination)
    $rule = Get-NetFirewallRule -Name $Name -ErrorAction SilentlyContinue
    $parameters = @{
        Direction = 'Inbound'; Action = 'Allow'; Enabled = 'True'; Profile = 'Any'
        InterfaceAlias = $Interface; RemoteAddress = $Source; LocalAddress = $Destination
        Protocol = 'Any'
    }
    if ($rule) { Set-NetFirewallRule -Name $Name @parameters | Out-Null }
    else { New-NetFirewallRule -Name $Name -DisplayName $Name @parameters | Out-Null }
}

function Assert-RoutingAdministrator {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run lab routing preparation in elevated Windows PowerShell after the lab switch exists.'
    }
}

function Invoke-LabRoutingPreparation {
    Assert-RoutingAdministrator
    $config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
    if ($config.network_cidr -ne '10.20.0.0/24' -or $config.network_gateway -ne '10.20.0.1') {
        throw 'Review routing bootstrap before changing the ADR 0002 network.'
    }
    $mode = & wsl.exe -d $Distribution -- wslinfo --networking-mode
    if ($LASTEXITCODE -ne 0 -or ($mode -join '').Trim() -ne 'nat') {
        throw 'WSL NAT networking is required. See docs/networking.md; mirrored mode is not supported here.'
    }
    $routes = @(Invoke-WslJson @('ip', '-j', '-4', 'route', 'show', 'default'))
    if ($routes.Count -ne 1) { throw 'Expected exactly one WSL default route.' }
    $gateway = [string] $routes[0].gateway
    $addresses = @(Invoke-WslJson @('ip', '-j', '-4', 'address', 'show', 'dev', $routes[0].dev))
    $sources = @($addresses.addr_info | Where-Object { $_.scope -eq 'global' })
    if ($sources.Count -ne 1) { throw 'Expected exactly one global WSL IPv4 address.' }
    $source = [string] $sources[0].local
    if ([ipaddress]::Parse($source).AddressFamily -ne 'InterNetwork' -or $source.StartsWith('10.20.0.')) {
        throw 'WSL source overlaps the lab or is not IPv4.'
    }
    $wslInterface = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $gateway)
    if ($wslInterface.Count -ne 1 -or $wslInterface[0].InterfaceAlias -notlike 'vEthernet (WSL*') {
        throw 'WSL gateway does not identify a unique Windows WSL virtual interface.'
    }
    $labAlias = "vEthernet ($($config.switch_name))"
    $labInterface = @(Get-NetIPAddress -AddressFamily IPv4 -InterfaceAlias $labAlias)
    if ($labInterface.Count -ne 1 -or $labInterface[0].IPAddress -ne $config.network_gateway -or $labInterface[0].PrefixLength -ne 24) {
        throw 'Terraform-owned lab interface is missing or has unexpected addressing.'
    }
    foreach ($adapter in Get-NetAdapter -Physical) {
        if (Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 |
            Where-Object { $_.Forwarding -eq 'Enabled' }) {
            throw "Physical interface forwarding is enabled on $($adapter.Name); refusing to expose a routed lab."
        }
    }
    if (Get-NetFirewallProfile -PolicyStore ActiveStore | Where-Object { -not $_.Enabled }) {
        throw 'All Windows firewall profiles must remain enabled.'
    }
    # Rules are limited to this distro's current IPv4 address and the lab subnet.
    Set-ScopedFirewallRule 'LAP-WSL-to-lab' $wslInterface[0].InterfaceAlias "$source/32" $config.network_cidr
    Set-ScopedFirewallRule 'LAP-lab-to-WSL' $labAlias $config.network_cidr "$source/32"
    $creator = '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}'
    $hvRule = Get-NetFirewallHyperVRule -Name 'LAP-WSL-lab-outbound' -ErrorAction SilentlyContinue
    $hvParameters = @{
        Direction = 'Outbound'; Action = 'Allow'; VMCreatorId = $creator
        RemoteAddresses = $config.network_cidr; LocalAddresses = "$source/32"; Enabled = 'True'
    }
    if ($hvRule) { Set-NetFirewallHyperVRule -Name 'LAP-WSL-lab-outbound' @hvParameters | Out-Null }
    else {
        New-NetFirewallHyperVRule -Name 'LAP-WSL-lab-outbound' -DisplayName 'LAP WSL to lab' @hvParameters | Out-Null
    }
    foreach ($alias in @($wslInterface[0].InterfaceAlias, $labAlias)) {
        Set-NetIPInterface -InterfaceAlias $alias -AddressFamily IPv4 -Forwarding Enabled
    }
    & wsl.exe -d $Distribution -u root -- ip -4 route replace $config.network_cidr via $gateway dev $routes[0].dev
    if ($LASTEXITCODE -ne 0) { throw 'Installing the WSL lab route failed.' }
    Write-Output "Configured WSL $source -> $gateway -> $($config.network_cidr). Validate direct SSH before administration."
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-LabRoutingPreparation }
