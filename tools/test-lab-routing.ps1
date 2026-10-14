# No host operations: every Windows network command and wsl.exe is mocked.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\prepare-lab-routing.ps1"
function Assert-RoutingAdministrator {}
$script:calls = [Collections.Generic.List[object]]::new()
$script:mode = 'nat'
$script:source = '172.26.182.215'
$script:physicalForwarding = 'Disabled'
$script:existing = $false
function wsl.exe {
    $global:LASTEXITCODE = 0
    if ($args -contains '--networking-mode') { return $script:mode }
    $script:calls.Add(@{ kind = 'route'; arguments = $args })
}
function Invoke-WslJson {
    param([string[]] $Arguments)
    if ($Arguments -contains 'route') { return [pscustomobject]@{ gateway = '172.26.176.1'; dev = 'eth0' } }
    return [pscustomobject]@{ addr_info = @([pscustomobject]@{ local = $script:source; scope = 'global' }) }
}
function Get-NetIPAddress {
    param($AddressFamily, $IPAddress, $InterfaceAlias)
    if ($IPAddress) { return [pscustomobject]@{ InterfaceAlias = 'vEthernet (WSL (Hyper-V firewall))' } }
    return [pscustomobject]@{ IPAddress = '10.20.0.1'; PrefixLength = 24 }
}
function Get-NetAdapter { param([switch] $Physical) return [pscustomobject]@{ ifIndex = 1; Name = 'Ethernet' } }
function Get-NetIPInterface {
    param($InterfaceIndex, $AddressFamily)
    return [pscustomobject]@{ Forwarding = $script:physicalForwarding }
}
function Get-NetFirewallProfile { param($PolicyStore) return [pscustomobject]@{ Enabled = $true } }
function Get-NetFirewallRule { param($Name, $ErrorAction) return $script:existing }
function Get-NetFirewallHyperVRule { param($Name, $ErrorAction) return $script:existing }
function New-NetFirewallRule {
    param($Name, $DisplayName, $Direction, $Action, $Enabled, $Profile, $InterfaceAlias, $RemoteAddress, $LocalAddress, $Protocol)
    $script:calls.Add(@{ kind = 'firewall'; source = $RemoteAddress; destination = $LocalAddress; interface = $InterfaceAlias })
}
function Set-NetFirewallRule {
    param($Name, $Direction, $Action, $Enabled, $Profile, $InterfaceAlias, $RemoteAddress, $LocalAddress, $Protocol)
    New-NetFirewallRule @PSBoundParameters
}
function New-NetFirewallHyperVRule {
    param($Name, $DisplayName, $Direction, $Action, $VMCreatorId, $RemoteAddresses, $LocalAddresses, $Enabled)
    $script:calls.Add(@{ kind = 'hyperv-firewall'; source = $LocalAddresses; destination = $RemoteAddresses })
}
function Set-NetFirewallHyperVRule {
    param($Name, $Direction, $Action, $VMCreatorId, $RemoteAddresses, $LocalAddresses, $Enabled)
    New-NetFirewallHyperVRule @PSBoundParameters
}
function Set-NetIPInterface {
    param($InterfaceAlias, $AddressFamily, $Forwarding)
    $script:calls.Add(@{ kind = 'forwarding'; interface = $InterfaceAlias })
}

foreach ($address in @('172.26.182.215', '172.26.182.216')) {
    $script:source = $address
    $script:calls.Clear()
    Invoke-LabRoutingPreparation | Out-Null
    $rules = @($script:calls | Where-Object { $_.kind -eq 'firewall' })
    if ($rules.Count -ne 2 -or $rules[0].source -ne "$address/32" -or $rules[0].destination -ne '10.20.0.0/24' -or
        $rules[1].source -ne '10.20.0.0/24' -or $rules[1].destination -ne "$address/32") { throw 'Firewall scopes escaped the WSL/lab pair.' }
    $forwarded = @($script:calls | Where-Object { $_.kind -eq 'forwarding' })
    if ($forwarded.Count -ne 2 -or @($forwarded | Where-Object { $_.interface -notlike 'vEthernet (*' }).Count) {
        throw 'Only two virtual interfaces may be enabled for forwarding.'
    }
    $hv = @($script:calls | Where-Object { $_.kind -eq 'hyperv-firewall' })
    if ($hv.Count -ne 1 -or $hv[0].source -ne "$address/32" -or $hv[0].destination -ne '10.20.0.0/24') { throw 'Hyper-V firewall scope mismatch.' }
    $script:existing = $true
}
foreach ($failure in @('mirrored', 'physical-forwarding')) {
    $script:calls.Clear()
    $script:mode = if ($failure -eq 'mirrored') { 'mirrored' } else { 'nat' }
    $script:physicalForwarding = if ($failure -eq 'physical-forwarding') { 'Enabled' } else { 'Disabled' }
    $failed = $false
    try { Invoke-LabRoutingPreparation | Out-Null } catch { $failed = $true }
    if (-not $failed -or $script:calls.Count) { throw "Unsafe $failure was not rejected before mutation." }
}
Write-Output 'Routing mocks: creation, address-change reconciliation and unsafe preconditions passed.'
