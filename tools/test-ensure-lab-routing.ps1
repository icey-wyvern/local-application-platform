# All network and UAC actions are mocked; this test never changes host state.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\ensure-lab-routing.ps1"

$script:labGateway = '172.26.176.1'
$script:missingLabRoute = $false
function wsl.exe {
    $global:LASTEXITCODE = 0
    if ($args -contains 'default') {
        return '[{"dst":"default","gateway":"172.26.176.1","dev":"eth0"}]'
    }
    if ($script:missingLabRoute) { return '[]' }
    return ('[{{"dst":"10.20.0.0/24","gateway":"{0}","dev":"eth0"}}]' -f $script:labGateway)
}
if (-not (Test-WslLabRoute '10.20.0.0/24')) { throw 'Correct WSL lab route was missed.' }
$script:labGateway = '172.26.176.9'
if (Test-WslLabRoute '10.20.0.0/24') { throw 'Stale WSL gateway was accepted.' }
$script:missingLabRoute = $true
if (Test-WslLabRoute '10.20.0.0/24') { throw 'Missing WSL route was accepted.' }

$script:elevationExitCode = 0
$script:elevationDeclined = $false
function Start-Process {
    param($FilePath, $Verb, $ArgumentList, [switch] $Wait, [switch] $PassThru, $WindowStyle)
    if ($FilePath -ne 'powershell.exe' -or $Verb -ne 'RunAs' -or -not $Wait -or
        -not $PassThru -or $WindowStyle -ne 'Hidden' -or
        $ArgumentList -notmatch '^-NoProfile -ExecutionPolicy Bypass -File "[^"]+prepare-lab-routing\.ps1"$') {
        throw 'Elevated command broadened beyond the routing preparation script.'
    }
    if ($script:elevationDeclined) { throw 'The operation was canceled by the user.' }
    return [pscustomobject]@{ ExitCode = $script:elevationExitCode }
}
Invoke-ElevatedRoutingPreparation
$script:elevationExitCode = 1
$failed = $false
try { Invoke-ElevatedRoutingPreparation } catch { $failed = $true }
if (-not $failed) { throw 'Elevated setup failure was ignored.' }
$script:elevationDeclined = $true
$failed = $false
try { Invoke-ElevatedRoutingPreparation } catch { $failed = $true }
if (-not $failed) { throw 'UAC cancellation was ignored.' }

$script:routeReady = $true
$script:wslReady = $true
$script:windowsReady = $true
$script:repaired = $false
$script:repairCalls = 0
$script:declineUac = $false

function Test-WslLabRoute { param([string] $NetworkCidr)
    if ($NetworkCidr -ne '10.20.0.0/24') { throw 'Unexpected lab network.' }
    return $script:routeReady
}
function Test-WslApi { return $script:wslReady }
function Test-WindowsApi { return $script:windowsReady }
function Ensure-WslDns { $script:dnsCalls++ }
$script:dnsCalls = 0
function Invoke-ElevatedRoutingPreparation {
    $script:repairCalls++
    if ($script:declineUac) { throw 'UAC was declined.' }
    if ($script:repaired) {
        $script:routeReady = $true
        $script:wslReady = $true
    }
}

Invoke-LabRoutingStartup | Out-Null
if ($script:repairCalls -ne 0) { throw 'Healthy routing requested elevation.' }
if ($script:dnsCalls -ne 1) { throw 'Healthy startup did not reconcile WSL DNS.' }

# WSL's default gateway can carry the lab route without a more-specific entry.
$script:routeReady = $false
Invoke-LabRoutingStartup | Out-Null
if ($script:repairCalls -ne 0 -or $script:dnsCalls -ne 2) {
    throw 'Reachable lab through the default gateway requested elevation.'
}
$script:routeReady = $true

# A missing route or changed WSL IP repairs once and then remains idempotent.
$script:routeReady = $false
$script:wslReady = $false
$script:repaired = $true
Invoke-LabRoutingStartup | Out-Null
Invoke-LabRoutingStartup | Out-Null
if ($script:repairCalls -ne 1) { throw 'Route restoration was not idempotent.' }
if ($script:dnsCalls -ne 4) { throw 'Repaired startup did not reconcile WSL DNS.' }

# A changed WSL address can leave the route intact while scoped firewall rules
# still refer to the old source; failed WSL/API reachability must re-run setup.
$script:wslReady = $false
Invoke-LabRoutingStartup | Out-Null
if ($script:repairCalls -ne 2) { throw 'Stale WSL firewall scope was not repaired.' }

$script:routeReady = $true
$script:wslReady = $false
$script:windowsReady = $false
$failed = $false
try { Invoke-LabRoutingStartup | Out-Null } catch { $failed = $true }
if (-not $failed -or $script:repairCalls -ne 2) {
    throw 'A down lab API incorrectly requested elevation.'
}

$script:windowsReady = $true
$script:routeReady = $false
$script:repaired = $false
$failed = $false
try { Invoke-LabRoutingStartup | Out-Null } catch { $failed = $true }
if (-not $failed -or $script:repairCalls -ne 3) {
    throw 'Failed post-repair WSL verification did not fail startup.'
}

$script:declineUac = $true
$failed = $false
try { Invoke-LabRoutingStartup | Out-Null } catch { $failed = $true }
if (-not $failed -or $script:repairCalls -ne 4) {
    throw 'Declined UAC did not fail startup.'
}

Write-Output 'Lab startup routing mocks: healthy, restore, replay, down API, failed repair and UAC decline passed.'
