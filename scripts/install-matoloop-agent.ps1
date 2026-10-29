param(
    [ValidateSet('install', 'update', 'status', 'uninstall', 'pair', 'run', 'maintenance-run')]
    [string] $Command = 'install',
    [string] $InstanceUrl,
    [Alias('Home')]
    [string] $HomeDirectory,
    [string] $Revision,
    [string] $Source,
    [switch] $Purge,
    [switch] $Yes
)

$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'matoloop-agent'
if ($PSCommandPath -and (Split-Path -Leaf $PSCommandPath) -eq 'installer.ps1') {
    $root = $PSScriptRoot
}
$data = Join-Path $root 'data'
$stateFile = Join-Path $root 'install.json'
$taskName = 'MatoLoop Agent'
$maintenanceTask = 'MatoLoop Agent Maintenance'
$marker = Join-Path $root '.managed-by-matoloop-agent'
$commandDirectory = Join-Path $root 'bin'
$env:UV_CACHE_DIR = Join-Path $root 'cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $root 'tools\python'
$env:UV_PYTHON_INSTALL_BIN = '0'
$env:UV_MANAGED_PYTHON = '1'
$uv = Join-Path $root 'tools\bin\uv.exe'

function Fail([string] $Message) { throw "MatoLoop Agent: $Message" }
# Managed state and executable paths must never traverse directory junctions.
$current = [IO.DirectoryInfo] $root
while ($current) {
    if ($current.Exists -and ($current.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Fail 'The installation path contains a reparse point.'
    }
    $current = $current.Parent
}
function State {
    if (-not (Test-Path -LiteralPath $stateFile -PathType Leaf)) { return $null }
    return (Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json)
}
function Save-State($Value) {
    $temporary = "$stateFile.new"
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $stateFile -Force
}
function Task { return Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
function Runtime($Value) {
    if (-not $Value) { Fail 'The agent is not installed.' }
    return Join-Path $Value.activeVenv 'Scripts\matoloop-agent.exe'
}
function Stop-Agent {
    $job = Task
    if ($job -and $job.State -eq 'Running') {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            if ((Task).State -ne 'Running') { return }
            Start-Sleep -Milliseconds 500
        }
        Fail 'The startup task did not stop; no files were removed.'
    }
}

function Set-CommandPath([bool] $Installed) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
    try {
        $value = $key.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = if ($null -ne $value) { $key.GetValueKind('Path') } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
        $entries = @()
        if ($null -ne $value) { $entries = @($value -split ';' | Where-Object { $_.TrimEnd('\') -ine $commandDirectory }) }
        if ($Installed) { $entries += $commandDirectory }
        if ($entries.Count) { $key.SetValue('Path', ($entries -join ';'), $kind) }
        else { $key.DeleteValue('Path', $false) }
    } finally { $key.Dispose() }
    if (-not ('MatoLoopEnvironment' -as [type])) {
        Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class MatoLoopEnvironment {
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr wparam,
        string lparam, uint flags, uint timeout, out UIntPtr result);
}
'@
    }
    $result = [UIntPtr]::Zero
    [void] [MatoLoopEnvironment]::SendMessageTimeout([IntPtr] 0xffff, 0x1a, [IntPtr]::Zero, 'Environment', 2, 1000, [ref] $result)
    $entries = @($env:PATH -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $commandDirectory })
    if ($Installed) { $entries += $commandDirectory }
    $env:PATH = $entries -join ';'
}

function Resolve-Git {
    $installed = Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($installed) { return $installed.Source }
    $destination = Join-Path $root 'tools\git'
    $executable = Join-Path $destination 'cmd\git.exe'
    foreach ($path in @((Join-Path $root 'tools'), $destination, (Join-Path $destination 'cmd'), $executable)) {
        if ((Test-Path -LiteralPath $path) -and
            ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Fail 'The private Git path contains a reparse point.'
        }
    }
    if (Test-Path -LiteralPath $executable -PathType Leaf) { return $executable }
    if (Test-Path -LiteralPath $destination) { Fail 'The private Git directory is incomplete.' }
    $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    switch ($architecture) {
        'AMD64' { $variant = '64-bit'; $sha256 = 'da35e72aa21c005a5a0d298cfbae110bc1609a815730ea0dde84b01a1b3cd3be' }
        'ARM64' { $variant = 'arm64'; $sha256 = '38b33dc6024026e3315cf88ab2cfea65205bbd7bb3a8e824bd21c8ad4fe609a7' }
        default { Fail 'Private Git requires x64 or ARM64 Windows.' }
    }
    $temporary = Join-Path $root ('tools\git-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary -Force | Out-Null
    try {
        $archive = Join-Path $temporary 'git.zip'
        Write-Host 'Downloading private MinGit 2.56.0.2.'
        Invoke-WebRequest -UseBasicParsing -Uri (
            "https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.2/MinGit-2.56.0.2-${variant}.zip"
        ) -OutFile $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $sha256) {
            Fail 'The private Git download checksum did not match.'
        }
        $unpacked = Join-Path $temporary 'unpacked'
        Expand-Archive -LiteralPath $archive -DestinationPath $unpacked
        & (Join-Path $unpacked 'cmd\git.exe') --version | Out-Null
        if ($LASTEXITCODE) { Fail 'The private Git executable failed its startup check.' }
        Move-Item -LiteralPath $unpacked -Destination $destination
    } finally { Remove-Item -LiteralPath $temporary -Recurse -Force -ErrorAction SilentlyContinue }
    return $executable
}

function Read-AgentCredential {
    $path = Join-Path $data 'agent-state\credential.dat'
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    Add-Type -AssemblyName System.Security
    $plain = [Security.Cryptography.ProtectedData]::Unprotect(
        [IO.File]::ReadAllBytes($path), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    return ([Text.Encoding]::UTF8.GetString($plain) | ConvertFrom-Json)
}

function Register-AgentTasks {
    param([string] $InstallationRoot, [string] $ExpectedUserSid)
    $ErrorActionPreference = 'Stop'
    $user = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($user.User.Value -ne $ExpectedUserSid) {
        throw 'MatoLoop Agent: Elevation must use the installing Windows account, not another administrator account.'
    }
    if (-not ([Security.Principal.WindowsPrincipal]::new($user)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'MatoLoop Agent: Task registration requires administrator rights.'
    }
    $current = [IO.DirectoryInfo] $InstallationRoot
    while ($current) {
        if ($current.Exists -and ($current.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'MatoLoop Agent: The installation path contains a reparse point.'
        }
        $current = $current.Parent
    }
    if (-not (Test-Path -LiteralPath (Join-Path $InstallationRoot '.managed-by-matoloop-agent') -PathType Leaf)) {
        throw 'MatoLoop Agent: Installation marker is missing.'
    }
    $taskName = 'MatoLoop Agent'
    $maintenanceTask = 'MatoLoop Agent Maintenance'
    $identity = $user.Name
    $installer = Join-Path $InstallationRoot 'installer.ps1'
    Write-Host "Windows Task Scheduler requires the account password for $identity (not the Windows Hello PIN)."
    $password = Read-Host 'Windows account password' -AsSecureString
    $credential = [Management.Automation.PSCredential]::new($identity, $password)
    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $created = @()
    try {
        if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
            $action = New-ScheduledTaskAction -Execute $powershell -Argument (
                '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $installer + '" run')
            $trigger = New-ScheduledTaskTrigger -AtStartup
            $settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
                -User $identity -Password ($credential.GetNetworkCredential().Password) -RunLevel Limited -ErrorAction Stop | Out-Null
            $created += $taskName
        }
        if (-not (Get-ScheduledTask -TaskName $maintenanceTask -ErrorAction SilentlyContinue)) {
            $action = New-ScheduledTaskAction -Execute $powershell -Argument (
                '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $installer + '" maintenance-run')
            $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 60) `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            Register-ScheduledTask -TaskName $maintenanceTask -Action $action -Settings $settings `
                -User $identity -Password ($credential.GetNetworkCredential().Password) -RunLevel Limited -ErrorAction Stop | Out-Null
            $created += $maintenanceTask
        }
        # Both tasks run without elevation. Their account can start/stop/manage its own tasks.
        $scheduler = New-Object -ComObject 'Schedule.Service'
        $scheduler.Connect()
        $folder = $scheduler.GetFolder('\')
        foreach ($name in @($taskName, $maintenanceTask)) {
            $folder.GetTask($name).SetSecurityDescriptor("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;$ExpectedUserSid)", 0)
        }
    } catch {
        $failure = $_
        foreach ($name in $created) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        }
        throw $failure
    }
}

$installerMutex = $null
if ($Command -in @('install', 'update', 'uninstall', 'maintenance-run')) {
    $normalizedRoot = ([IO.Path]::GetFullPath($root)).ToLowerInvariant().TrimEnd('\', '/')
    $hash = [Security.Cryptography.SHA256]::Create()
    try { $suffix = [BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($normalizedRoot))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
    $installerMutex = [Threading.Mutex]::new($false, ('Global\MatoLoopAgentInstaller-' + $suffix))
    try { $acquired = $installerMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) {
        $installerMutex.Dispose()
        Fail 'Another installation or maintenance command is running.'
    }
}
try {
# One on-demand Task Scheduler worker survives stopping the boot-time Agent task.
if ($Command -eq 'maintenance-run') {
    $requestFile = Join-Path $data 'maintenance-request.json'
    $request = Get-Content -LiteralPath $requestFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ((($request.PSObject.Properties.Name | Sort-Object) -join ',') -ne 'action,jobId,revision' -or
        $request.action -notin @('update', 'uninstall') -or
        ([Guid]::Parse($request.jobId)).ToString() -cne $request.jobId) { Fail 'Invalid maintenance request.' }
    if (($request.action -eq 'update' -and $request.revision -cnotmatch '\A[0-9a-f]{40}\z') -or
        ($request.action -eq 'uninstall' -and $null -ne $request.revision)) { Fail 'Invalid maintenance revision.' }
    Remove-Item -LiteralPath $requestFile -Force
    $connection = Read-AgentCredential
    $completionUrl = ($connection.backendUrl -replace '^wss:', 'https:' -replace '^ws:', 'http:')
    $completionUrl = $completionUrl -replace '/connect$', "/$($connection.agentId)/maintenance/$($request.jobId)/complete"
    $result = 'failed'
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'Never'
    try {
        if ($request.action -eq 'update') { & $PSCommandPath update -Revision $request.revision -Yes }
        else { & $PSCommandPath uninstall -Yes }
        if ($LASTEXITCODE -ne 0) { Fail 'Maintenance failed.' }
        $result = 'succeeded'
    } catch { Write-Host 'MatoLoop Agent maintenance failed.' }
    $pending = Join-Path $data ("agent-state\maintenance-$($request.jobId).json")
    $outcome = @{backendUrl=$connection.backendUrl; agentId=$connection.agentId; jobId=$request.jobId; status=$result}
    $temporary = "$pending.new"
    try {
        [IO.File]::WriteAllText($temporary, ($outcome | ConvertTo-Json -Compress), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $pending -Force
    } catch { Write-Host 'Maintenance outcome could not be retained; attempting direct delivery.' }
    $reported = $false
    for ($attempt = 0; $attempt -lt 5; $attempt++) {
        try {
            Invoke-RestMethod -Method Post -Uri $completionUrl -TimeoutSec 15 `
                -Headers @{Authorization="Bearer $($connection.accessKey)"} `
                -ContentType 'application/json' -Body (@{status=$result} | ConvertTo-Json -Compress) | Out-Null
            Remove-Item -LiteralPath $pending -Force -ErrorAction SilentlyContinue
            $reported = $true
            break
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $reported) { Write-Host 'Maintenance result could not be delivered.' }
    exit $(if ($result -eq 'succeeded') { 0 } else { 1 })
}
if ($Command -eq 'run') {
    $state = State
    $env:MATOLOOP_AGENT_CONFIG = Join-Path $data 'config.json'
    & (Runtime $state) run
    exit $LASTEXITCODE
}
if ($Command -eq 'pair') {
    $state = State
    $env:MATOLOOP_AGENT_CONFIG = Join-Path $data 'config.json'
    $websocket = $state.instanceUrl.TrimEnd('/') -replace '^https:', 'wss:' -replace '^http:', 'ws:'
    & (Runtime $state) pair --backend-url ($websocket + '/api/execution-agents/connect')
    exit $LASTEXITCODE
}
if ($Command -eq 'status') {
    $state = State
    if (-not $state) { Fail 'The agent is not installed.' }
    Write-Host "Source: $($state.source)"
    Write-Host "Home: $($state.homeDirectory)"
    Write-Host "Data: $data"
    Task | Select-Object TaskName, State
    Get-ScheduledTaskInfo -TaskName $taskName | Select-Object LastRunTime, LastTaskResult
    exit
}
if ($Command -eq 'uninstall') {
    $state = State
    if (-not $state -and -not $Purge) { Fail 'The agent is not installed.' }
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { Fail 'Installation marker is missing.' }
    $job = Task
    $expected = Join-Path $root 'installer.ps1'
    if ($job -and ($job.Actions.Count -ne 1 -or $job.Actions[0].Arguments -notlike "*${expected}*")) {
        Fail 'The startup task was replaced; no files were removed.'
    }
    if (-not $Yes -and (Read-Host 'Remove MatoLoop Agent? [y/N]') -notin @('y', 'Y', 'yes', 'ja')) { exit }
    Stop-Agent
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $maintenanceTask -Confirm:$false -ErrorAction SilentlyContinue
    Set-CommandPath $false
    if ($Purge) {
        Remove-Item -LiteralPath $root -Recurse -Force
    } else {
        foreach ($name in @('venvs', 'source', 'sources', 'tools', 'cache', 'bin', 'install.json', 'installer.ps1')) {
            Remove-Item -LiteralPath (Join-Path $root $name) -Recurse -Force -ErrorAction SilentlyContinue
        }
        foreach ($child in Get-ChildItem -LiteralPath $data -ErrorAction SilentlyContinue) {
            if ($child.Name -ne 'agent-state') { Remove-Item -LiteralPath $child.FullName -Recurse -Force }
        }
    }
    Write-Host 'MatoLoop Agent removed. The workspace was preserved.'
    exit
}

if ($Command -eq 'update' -and -not (State)) { Fail 'The agent is not installed.' }
$existing = State
if ($existing -and -not (Test-Path -LiteralPath $marker -PathType Leaf)) { Fail 'Installation marker is missing.' }
if (-not $existing -and -not (Test-Path -LiteralPath $marker) -and ((Test-Path -LiteralPath $root) -and
    (Get-ChildItem -LiteralPath $root -Force | Select-Object -First 1))) { Fail 'The installation directory is not empty.' }
if (-not $existing -and (Task)) { Fail 'An unmanaged MatoLoop Agent task already exists.' }

$connection = Read-AgentCredential
$pairedInstance = if ($connection) {
    ($connection.backendUrl -replace '^wss:', 'https:' -replace '^ws:', 'http:') -replace '/api/execution-agents/connect$', ''
} else { $null }
if ($InstanceUrl -and $pairedInstance -and $InstanceUrl.TrimEnd('/') -ne $pairedInstance) {
    Fail 'The instance URL cannot change while pairing credentials are preserved.'
}
if (-not $InstanceUrl) { $InstanceUrl = if ($pairedInstance) { $pairedInstance } elseif ($existing) {
    $existing.instanceUrl
} else { Read-Host 'MatoLoop instance URL (https://host)' } }
try { $instance = [Uri] $InstanceUrl } catch { Fail 'Invalid instance URL.' }
if ($instance.Scheme -notin @('https', 'http') -or -not $instance.Host -or $instance.UserInfo -or
    $instance.Query -or $instance.Fragment) { Fail 'Invalid instance URL.' }
$websocket = $InstanceUrl.TrimEnd('/') -replace '^https:', 'wss:' -replace '^http:', 'ws:'
$websocket += '/api/execution-agents/connect'

$checkout = if ($Command -eq 'update') { $null } elseif ($Source) {
    (Resolve-Path -LiteralPath $Source).Path
} elseif ($PSCommandPath -and (Split-Path -Leaf $PSCommandPath) -ne 'installer.ps1') {
    $candidate = Join-Path (Split-Path -Parent $PSScriptRoot) 'apps\matoloop-agent\pyproject.toml'
    if (Test-Path -LiteralPath $candidate) { Split-Path -Parent $PSScriptRoot } else { $null }
} else { $null }
$managedSource = -not $checkout
$sourceUrl = if ($existing -and $existing.sourceUrl) { $existing.sourceUrl } elseif ($env:MATOLOOP_AGENT_SOURCE_URL) {
    $env:MATOLOOP_AGENT_SOURCE_URL
} elseif ($existing -and (Test-Path -LiteralPath (Join-Path $existing.source '.git'))) {
    $git = Resolve-Git
    $origin = & $git -C $existing.source remote get-url origin
    if ($LASTEXITCODE) { Fail 'Could not read the installed source repository.' }
    $origin
} else { 'https://github.com/devmato/mato-loop.git' }
New-Item -ItemType Directory -Path $root, $data, (Join-Path $root 'tools\bin'),
    (Join-Path $root 'venvs') -Force | Out-Null
if (-not (Test-Path -LiteralPath $marker)) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls $root /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE) { Fail 'Could not restrict the installation directory ACL.' }
    New-Item -ItemType File -Path $marker -Force | Out-Null
}
if ($managedSource) {
    if (-not $Revision) {
        try {
            $target = Invoke-RestMethod -Uri ($InstanceUrl.TrimEnd('/') + '/api/execution-agents/installation-target') `
                -TimeoutSec 30 -MaximumRedirection 0
        } catch { Fail 'The backend installation target is unavailable; use a published backend build.' }
        if ($target.revision -isnot [string] -or $target.revision -cnotmatch '\A[0-9a-f]{40}\z' -or
            $target.version -isnot [string] -or $target.version -cnotmatch (
                '\A[0-9]{4}-[0-9]{2}-[0-9]{2}-' + $target.revision.Substring(0, 8) + '\z')) {
            Fail 'The backend returned an invalid installation target.'
        }
        $Revision = $target.revision
    }
    if ($Revision -cnotmatch '\A[0-9a-f]{40}\z') { Fail 'The installation target must be a full Git commit SHA.' }
    $checkout = Join-Path $root ('sources\' + [Guid]::NewGuid().ToString('N'))
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'Never'
    $git = Resolve-Git
    try {
        & $git init --quiet $checkout
        if ($LASTEXITCODE) { Fail 'Could not initialize the source checkout.' }
        & $git -C $checkout remote add -- origin $sourceUrl
        if ($LASTEXITCODE) { Fail 'Could not configure the source repository.' }
        & $git -C $checkout fetch --depth=1 --no-tags origin $Revision
        if ($LASTEXITCODE) { Fail 'Could not fetch the backend commit.' }
        $actual = & $git -C $checkout rev-parse 'FETCH_HEAD^{commit}'
        if ($LASTEXITCODE -or $actual -cne $Revision) { Fail 'Source does not match the backend commit.' }
        & $git -C $checkout checkout --detach $Revision
        if ($LASTEXITCODE) { Fail 'Could not check out the backend commit.' }
        if (-not (Test-Path -LiteralPath (Join-Path $checkout 'libs\python\version\src\matoloop_version\__init__.py'))) {
            Fail 'The target commit does not support versioned Agent installations.'
        }
    } catch {
        Remove-Item -LiteralPath $checkout -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $checkout 'apps\matoloop-agent\pyproject.toml'))) {
    Fail 'Source is not a MatoLoop checkout.'
}
if (-not $HomeDirectory) { $HomeDirectory = if ($existing) { $existing.homeDirectory } else {
    Join-Path $env:USERPROFILE 'mato-loop-agent\workspace'
} }
$HomeDirectory = [IO.Path]::GetFullPath($HomeDirectory).TrimEnd('\')
if ($HomeDirectory.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
    $HomeDirectory.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $root.StartsWith($HomeDirectory + '\', [StringComparison]::OrdinalIgnoreCase)) { Fail 'Workspace must be outside the installation.' }
New-Item -ItemType Directory -Path $HomeDirectory -Force | Out-Null

if (-not (Test-Path -LiteralPath $uv)) {
    $bootstrap = Join-Path $env:TEMP 'matoloop-uv-install.ps1'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/0.10.9/install.ps1' -OutFile $bootstrap
    try {
        $env:UV_UNMANAGED_INSTALL = Join-Path $root 'tools\bin'
        & $bootstrap
        if ($LASTEXITCODE) { Fail 'Could not install private uv.' }
    } finally { Remove-Item -LiteralPath $bootstrap -Force -ErrorAction SilentlyContinue }
}
& $uv python install 3.13
if ($LASTEXITCODE) { Fail 'Could not install private Python.' }
$newVenv = Join-Path $root ("venvs\" + [Guid]::NewGuid().ToString('N'))
$installer = Join-Path $root 'installer.ps1'
$previousInstaller = if (Test-Path -LiteralPath $installer) { [IO.File]::ReadAllBytes($installer) } else { $null }
$env:VIRTUAL_ENV = $newVenv
$snapshot = $null
$commandInstalled = $false
try {
    $python = & $uv python find 3.13
    if ($LASTEXITCODE) { Fail 'Could not find private Python.' }
    $git = Resolve-Git
    $env:PATH = (Split-Path -Parent $git) + [IO.Path]::PathSeparator + $env:PATH
    $versionScript = Join-Path $checkout 'libs\python\version\src\matoloop_version\__init__.py'
    # Build from a compact snapshot; version stamping must precede removing Git metadata.
    $snapshot = Join-Path $root ('sources\' + [Guid]::NewGuid().ToString('N'))
    foreach ($relative in @('apps\matoloop-agent\src', 'apps\matoloop-agent\pyproject.toml',
        'apps\matoloop-agent\uv.lock', 'apps\matoloop-agent\README.md',
        'libs\python\version\src', 'libs\python\version\pyproject.toml',
        'scripts\install-matoloop-agent.ps1', 'LICENSE')) {
        $origin = Join-Path $checkout $relative
        $destination = Join-Path $snapshot $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $origin -Destination $destination -Recurse -Force
    }
    # Older target commits have no CLI launcher; retain the installed launcher on downgrade.
    foreach ($name in @('matoloop-agent.cmd', 'matoloop-agent-cli.ps1')) {
        $origin = Join-Path $checkout "scripts\$name"
        if (Test-Path -LiteralPath $origin -PathType Leaf) {
            Copy-Item -LiteralPath $origin -Destination (Join-Path $snapshot "scripts\$name") -Force
        }
    }
    $stamp = Join-Path $snapshot 'libs\python\version\src\matoloop_version\build-version.txt'
    & $python $versionScript --source $checkout --output $stamp
    if ($LASTEXITCODE) { Fail 'Could not determine the Agent version.' }
    if ($managedSource) { Remove-Item -LiteralPath $checkout -Recurse -Force }
    $checkout = $snapshot
    & $uv sync --directory (Join-Path $checkout 'apps\matoloop-agent') --frozen --no-dev --no-editable --active --refresh-package matoloop-agent --refresh-package matoloop-version
    if ($LASTEXITCODE) { Fail 'Could not prepare the new Agent environment.' }
    $executable = Join-Path $newVenv 'Scripts\matoloop-agent.exe'
    & $executable --help | Out-Null
    if ($LASTEXITCODE) { Fail 'The new Agent executable failed its startup check.' }
    $configFile = Join-Path $data 'config.json'
    $config = if (Test-Path -LiteralPath $configFile) {
        Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } else { [pscustomobject] @{} }
    $config | Add-Member -NotePropertyName homeDirectory -NotePropertyValue $HomeDirectory -Force
    $config | Add-Member -NotePropertyName allowInsecure -NotePropertyValue ($instance.Scheme -eq 'http') -Force
    $config | Add-Member -NotePropertyName shellExecutable -NotePropertyValue (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -Force
    $previousConfig = if (Test-Path -LiteralPath $configFile) { Get-Content -LiteralPath $configFile -Raw } else { $null }
    $sourceInstaller = Join-Path $checkout 'scripts\install-matoloop-agent.ps1'
    if (-not (Test-Path -LiteralPath $sourceInstaller -PathType Leaf)) {
        Fail 'The source checkout has no Windows installer.'
    }
    foreach ($path in @($commandDirectory, (Join-Path $commandDirectory 'matoloop-agent.cmd'), (Join-Path $commandDirectory 'cli.ps1'))) {
        if ((Test-Path -LiteralPath $path) -and
            ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            Fail 'The command path contains a reparse point.'
        }
    }
    # shortcut: compatible CLI setup survives activation failure; add rollback if the install.json contract changes.
    if (Test-Path -LiteralPath (Join-Path $checkout 'scripts\matoloop-agent.cmd') -PathType Leaf) {
        New-Item -ItemType Directory -Path $commandDirectory -Force | Out-Null
        foreach ($name in @('matoloop-agent.cmd', 'cli.ps1')) {
            $sourceName = if ($name -eq 'cli.ps1') { 'matoloop-agent-cli.ps1' } else { $name }
            $path = Join-Path $commandDirectory $name
            $sourcePath = Join-Path $checkout "scripts\$sourceName"
            if ((Test-Path -LiteralPath $path -PathType Leaf) -and
                (Get-FileHash -LiteralPath $path -ErrorAction Stop).Hash -eq (Get-FileHash -LiteralPath $sourcePath -ErrorAction Stop).Hash) { continue }
            $staged = Join-Path $snapshot "command-$name.new"
            Copy-Item -LiteralPath $sourcePath -Destination $staged -Force
            if (Test-Path -LiteralPath $path -PathType Leaf) { [IO.File]::Replace($staged, $path, [NullString]::Value) }
            else { [IO.File]::Move($staged, $path) }
        }
    }
    if (Test-Path -LiteralPath (Join-Path $commandDirectory 'matoloop-agent.cmd') -PathType Leaf) {
        $commandInstalled = $true
        Set-CommandPath $true
    }
    if (-not [string]::Equals($sourceInstaller, $installer, [StringComparison]::OrdinalIgnoreCase)) {
        Copy-Item -LiteralPath $sourceInstaller -Destination $installer -Force
    }
    if (-not (Task) -or -not (Get-ScheduledTask -TaskName $maintenanceTask -ErrorAction SilentlyContinue)) {
        $user = [Security.Principal.WindowsIdentity]::GetCurrent()
        $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        if (([Security.Principal.WindowsPrincipal]::new($user)).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
            Register-AgentTasks -InstallationRoot $root -ExpectedUserSid $user.User.Value
        } else {
            Write-Host 'Windows will request administrator approval to register the startup tasks. Use the same Windows account.'
            $registrationError = Join-Path $root 'task-registration-error.txt'
            Remove-Item -LiteralPath $registrationError -ErrorAction SilentlyContinue
            $registration = 'function Register-AgentTasks {' + ${function:Register-AgentTasks}.ToString() + "}`n" +
                "try { Register-AgentTasks -InstallationRoot '" + $root.Replace("'", "''") + "' -ExpectedUserSid '" + $user.User.Value + "'; exit 0 } " +
                "catch { [IO.File]::WriteAllText('" + $registrationError.Replace("'", "''") + "', `$_.Exception.Message); exit 1 }"
            $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($registration))
            try {
                $process = Start-Process -FilePath $powershell -Verb RunAs -Wait -PassThru `
                    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) -ErrorAction Stop
                if ($process.ExitCode) {
                    if (Test-Path -LiteralPath $registrationError -PathType Leaf) {
                        Fail ([IO.File]::ReadAllText($registrationError))
                    }
                    Fail 'Task registration failed. Elevation must use the installing Windows account.'
                }
            } finally {
                Remove-Item -LiteralPath $registrationError -ErrorAction SilentlyContinue
            }
        }
    }
    Stop-Agent
    try {
        [IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
        $newState = [pscustomobject] @{activeVenv=$newVenv; source=$checkout; sourceUrl=$sourceUrl;
            homeDirectory=$HomeDirectory; instanceUrl=$InstanceUrl.TrimEnd('/'); maintenanceReady=$true}
        Save-State $newState
        Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
        Start-Sleep -Seconds 2
        if ((Task).State -ne 'Running') { Fail 'The Agent task did not remain running.' }
    } catch {
        $failure = $_
        try {
            Stop-Agent
            if ($existing) { Save-State $existing } else { Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue }
            if ($previousConfig) { [IO.File]::WriteAllText($configFile, $previousConfig, [Text.UTF8Encoding]::new($false)) } else {
                Remove-Item -LiteralPath $configFile -ErrorAction SilentlyContinue
            }
            if ($previousInstaller) { [IO.File]::WriteAllBytes($installer, $previousInstaller) }
            if (-not $existing) {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $maintenanceTask -Confirm:$false -ErrorAction SilentlyContinue
            }
            if ($existing -and (Task)) { Start-ScheduledTask -TaskName $taskName -ErrorAction Stop }
        } catch {
            Write-Warning "The previous Agent could not be restored: $_"
        }
        throw $failure
    }
    if ($existing -and $existing.activeVenv -ne $newVenv -and
        ([IO.Path]::GetFullPath($existing.activeVenv)).StartsWith((Join-Path $root 'venvs') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $existing.activeVenv -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($existing -and $existing.source -ne $checkout -and
        ([IO.Path]::GetFullPath($existing.source)).StartsWith((Join-Path $root 'sources') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $existing.source -Recurse -Force -ErrorAction SilentlyContinue
    }
} catch {
    $failure = $_
    try {
        if (-not $existing -and $commandInstalled) { Set-CommandPath $false }
        Remove-Item -LiteralPath $newVenv -Recurse -Force -ErrorAction SilentlyContinue
        if ($snapshot) { Remove-Item -LiteralPath $snapshot -Recurse -Force -ErrorAction SilentlyContinue }
        if ($managedSource -and $checkout -ne $snapshot) { Remove-Item -LiteralPath $checkout -Recurse -Force -ErrorAction SilentlyContinue }
        if ($previousInstaller) { [IO.File]::WriteAllBytes($installer, $previousInstaller) } else {
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Warning "Installation cleanup failed: $_"
    }
    throw $failure
}
Write-Host "MatoLoop Agent installed. Workspace: $HomeDirectory"
$pairCommand = "& '$installer' pair"
if (Test-Path -LiteralPath (Join-Path $commandDirectory 'matoloop-agent.cmd') -PathType Leaf) {
    $pairCommand = 'matoloop-agent pair'
    Write-Host 'Commands: matoloop-agent pair | status | update | uninstall. Restart your terminal to use the command.'
} else {
    Write-Host "Pair with: $pairCommand"
}
if ($Command -eq 'install' -and -not $Yes -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and
    -not (Test-Path -LiteralPath (Join-Path $data 'agent-state\credential.dat'))) {
    Write-Host 'Create a computer under Einstellungen > Computer, then enter its values.'
    $env:MATOLOOP_AGENT_CONFIG = Join-Path $data 'config.json'
    & $executable pair --backend-url $websocket
    if ($LASTEXITCODE) { Fail "Pairing failed. Retry with $pairCommand; the Agent remains installed." }
}
} finally {
    if ($installerMutex) {
        $installerMutex.ReleaseMutex()
        $installerMutex.Dispose()
    }
}
