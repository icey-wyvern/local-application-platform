param([Parameter(Mandatory = $true)][string]$Path)

$ErrorActionPreference = 'Stop'
$expected = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\backups'))
$target = [System.IO.Path]::GetFullPath($Path)
if ($target -ne $expected) {
    throw 'Backup ACL target must be the repository backups directory.'
}
if (-not (Test-Path -LiteralPath $target -PathType Container)) {
    throw 'Backup directory does not exist.'
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$administrators = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$identities = @($currentUser, $system, $administrators)
$allowedSidValues = @($identities | ForEach-Object { $_.Value })

function Test-PrivateAcl([string]$ItemPath, [bool]$RequireProtected) {
    $acl = Get-Acl -LiteralPath $ItemPath
    if ($RequireProtected -and -not $acl.AreAccessRulesProtected) { return $false }
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        if ($sid -notin $allowedSidValues -or
            $rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) {
            return $false
        }
    }
    return $true
}

function Set-PrivateAcl([string]$ItemPath) {
    $item = Get-Item -LiteralPath $ItemPath -Force
    $acl = Get-Acl -LiteralPath $ItemPath
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) {
        [void]$acl.RemoveAccessRuleSpecific($rule)
    }
    $inheritance = if ($item.PSIsContainer) {
        [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        [System.Security.AccessControl.InheritanceFlags]::None
    }
    foreach ($identity in $identities) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $identity,
            [System.Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            [System.Security.AccessControl.PropagationFlags]::None,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $ItemPath -AclObject $acl
}

if (-not (Test-PrivateAcl $target $true)) {
    Set-PrivateAcl $target
}
Get-ChildItem -LiteralPath $target -Force -Recurse | ForEach-Object {
    if (-not (Test-PrivateAcl $_.FullName $false)) {
        Set-PrivateAcl $_.FullName
    }
}
