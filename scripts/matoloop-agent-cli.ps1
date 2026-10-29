$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ($args.Count -gt 0 -and $args[0] -in @('install', 'update', 'status', 'uninstall')) {
    & "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'installer.ps1') @args
    exit $LASTEXITCODE
}
$state = Get-Content -LiteralPath (Join-Path $root 'install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$env:MATOLOOP_AGENT_CONFIG = Join-Path $root 'data\config.json'
if ($args.Count -gt 0 -and $args[0] -eq 'pair' -and
    -not ($args | Where-Object { $_ -eq '--backend-url' -or $_ -like '--backend-url=*' })) {
    $websocket = $state.instanceUrl.TrimEnd('/') -replace '^https:', 'wss:' -replace '^http:', 'ws:'
    $args += @('--backend-url', ($websocket + '/api/execution-agents/connect'))
}
if ($args.Count -gt 0 -and $args[0] -in @('--help', '-h')) {
    Write-Host 'Native installation commands: install | update | status | uninstall [-Purge]'
}
& (Join-Path $state.activeVenv 'Scripts\matoloop-agent.exe') @args
exit $LASTEXITCODE
