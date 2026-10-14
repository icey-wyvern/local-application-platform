$ErrorActionPreference = 'Stop'
$files = Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1' -Recurse
foreach ($file in $files) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref] $tokens, [ref] $parseErrors) | Out-Null
    if ($parseErrors) { throw ($parseErrors | Out-String) }
}
Write-Output "PowerShell parser: $($files.Count) files passed."
if (Get-Module -ListAvailable PSScriptAnalyzer) {
    $findings = @($files | ForEach-Object { Invoke-ScriptAnalyzer -Path $_.FullName -Severity Error })
    if ($findings) { throw ($findings | Out-String) }
    Write-Output 'PSScriptAnalyzer error checks passed.'
} else { Write-Output 'PSScriptAnalyzer is not installed; parser checks only.' }
