param(
    [string]$TestRoot,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
$Package = Split-Path -Parent $PSScriptRoot
$Module = Join-Path $Package 'powershell\DurableLauncher.psm1'
$HelperDir = Join-Path $Package 'python'
$Driver = Join-Path $Package 'tests\fake_durable_driver.py'
$Python = if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    (Get-Command python.exe -ErrorAction Stop).Source
}
else { [System.IO.Path]::GetFullPath($PythonPath) }
if ([string]::IsNullOrWhiteSpace($TestRoot)) {
    $TestRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) (
        'comsol_launcher_tests\v1_8_' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    )
}
$TestRoot = [System.IO.Path]::GetFullPath($TestRoot)
if (Test-Path -LiteralPath $TestRoot) { Remove-Item -LiteralPath $TestRoot -Recurse -Force }
New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
Import-Module $Module -Force
if ((Get-DurableLauncherVersion) -ne '1.8.1') { throw 'Unexpected launcher version.' }

& (Get-Module DurableLauncher) {
    param($Root, $DriverPath)
    $Scripts = Join-Path $Root 'redirector\Scripts'
    $BaseHome = Join-Path $Root 'base-python'
    New-Item -ItemType Directory -Path $Scripts -Force | Out-Null
    Set-Content -LiteralPath (Join-Path (Split-Path -Parent $Scripts) 'pyvenv.cfg') -Value "home = $BaseHome"
    $Parent = [pscustomobject]@{
        ProcessId = 100; ParentProcessId = 50; Name = 'python.exe'
        CommandLine = "python $DriverPath"; ExecutablePath = (Join-Path $Scripts 'python.exe')
        CreationDate = [datetime]'2026-01-01T01:00:00'
    }
    $Child = [pscustomobject]@{
        ProcessId = 101; ParentProcessId = 100; Name = 'python.exe'
        CommandLine = $Parent.CommandLine; ExecutablePath = (Join-Path $BaseHome 'python.exe')
        CreationDate = [datetime]'2026-01-01T01:00:01'
    }
    function Get-CimInstance { param($Class) @($Parent, $Child) }
    if (@(Get-DurableDriverProcesses $DriverPath).Count -ne 1) { throw 'Verified redirector was not collapsed.' }
    $Child.ParentProcessId = 50
    if (@(Get-DurableDriverProcesses $DriverPath).Count -ne 2) { throw 'Independent launch was hidden.' }
    $Child.ParentProcessId = 100
    $Child.CommandLine += ' --second'
    if (@(Get-DurableDriverProcesses $DriverPath).Count -ne 2) { throw 'Different child command was hidden.' }
    $Child.CommandLine = $Parent.CommandLine
    $Child.ExecutablePath = $Parent.ExecutablePath
    if (@(Get-DurableDriverProcesses $DriverPath).Count -ne 2) { throw 'Recursive interpreter launch was hidden.' }
} $TestRoot $Driver

$ModuleText = [IO.File]::ReadAllText($Module)
$MonitorStart = $ModuleText.IndexOf('function Show-DurableJobMonitor', [StringComparison]::Ordinal)
$MonitorEnd = $ModuleText.IndexOf('function Test-DurableResourcePolicy', $MonitorStart, [StringComparison]::Ordinal)
$FrameStart = $ModuleText.IndexOf('function Write-DurableMonitorFrame', [StringComparison]::Ordinal)
if ($MonitorStart -lt 0 -or $MonitorEnd -le $MonitorStart -or $FrameStart -lt 0) { throw 'Monitor function boundaries were not found.' }
$MonitorBody = $ModuleText.Substring($MonitorStart, $MonitorEnd - $MonitorStart)
$FrameBody = $ModuleText.Substring($FrameStart, $MonitorStart - $FrameStart)
if ($MonitorBody -match '(?im)^\s*Clear-Host\s*$') { throw 'Monitor refresh loop still clears the terminal.' }
if ($MonitorBody -match '(?im)^\s*Write-Host\b') { throw 'Monitor refresh loop still emits sequential host writes.' }
if ([regex]::Matches($FrameBody, [regex]::Escape('[Console]::Write(($Padded -join [Environment]::NewLine))')).Count -ne 1) {
    throw 'Monitor frame is not emitted through exactly one buffered console write.'
}
& (Get-Module DurableLauncher) {
    $State = @{ Initialized = $true; Top = 0; LineCount = 0 }
    Write-DurableMonitorFrame -Lines @('frame top', '', 'frame bottom') -State $State -Highlights @{ 2 = 'Green' }
}

function Wait-Until {
    param([scriptblock]$Condition, [int]$TimeoutSeconds = 30)
    $Deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $Deadline) {
        if (& $Condition) { return }
        Start-Sleep -Milliseconds 50
    }
    throw 'Timed out waiting for the test condition.'
}

function Get-TestSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return & (Get-Module DurableLauncher) {
        param($Value)
        Get-DurableSha256 -Path $Value
    } $Path
}

function Start-FakeDriver {
    $Previous = @{}
    $Environment = @{
        DURABLE_TEST_ROOT = $TestRoot
        DURABLE_TEST_HELPER_DIR = $HelperDir
        DURABLE_TEST_JOB_ID = 'durable-launcher-test-v1-8'
        DURABLE_TEST_SPEC_ID = 'fake-spec-v1'
        DURABLE_TEST_POINTS = '8'
        DURABLE_TEST_POINT_SECONDS = '0.2'
    }
    try {
        foreach ($Name in $Environment.Keys) {
            $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
            [Environment]::SetEnvironmentVariable($Name, $Environment[$Name], 'Process')
        }
        $Process = Start-Process -FilePath $Python -ArgumentList @('"' + $Driver + '"') -WorkingDirectory (Split-Path -Parent $Driver) -RedirectStandardOutput (Join-Path $TestRoot 'fake.stdout.log') -RedirectStandardError (Join-Path $TestRoot 'fake.stderr.log') -WindowStyle Hidden -PassThru
        return $Process
    }
    finally {
        foreach ($Name in $Previous.Keys) { [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], 'Process') }
    }
}

New-Item -ItemType Directory -Path $TestRoot -Force | Out-Null
$Config = @{
    JobName = 'Durable launcher synthetic test'
    JobId = 'durable-launcher-test-v1-8'
    LauncherPath = $PSCommandPath
    ModulePath = $Module
    Python = $Python
    Driver = $Driver
    Output = $TestRoot
    TotalPoints = 8
    StatusPath = Join-Path $TestRoot 'status.json'
    ResultsPath = Join-Path $TestRoot 'results.jsonl'
    ControlDirectory = Join-Path $TestRoot 'control'
    StdoutPath = Join-Path $TestRoot 'fake.stdout.log'
    StderrPath = Join-Path $TestRoot 'fake.stderr.log'
    MonitorIntervalSeconds = 1
    PointEstimateSeconds = 0.2
    RunEnvironment = @{
        DURABLE_TEST_ROOT = $TestRoot
        DURABLE_TEST_HELPER_DIR = $HelperDir
        DURABLE_TEST_JOB_ID = 'durable-launcher-test-v1-8'
        DURABLE_TEST_SPEC_ID = 'fake-spec-v1'
        DURABLE_TEST_POINTS = '8'
        DURABLE_TEST_POINT_SECONDS = '0.2'
    }
}

$OptionalRoot = Join-Path $TestRoot 'optional_terminal_status'
New-Item -ItemType Directory -Path $OptionalRoot -Force | Out-Null
$OptionalStatusPath = Join-Path $OptionalRoot 'status.json'
$OptionalResultsPath = Join-Path $OptionalRoot 'results.jsonl'
Write-DurableAtomicJson -Path $OptionalStatusPath -Value ([ordered]@{
    status = 'running'
    completed = 0
    planned = 1
})
Write-DurableAtomicJson -Path $OptionalStatusPath -Value ([ordered]@{
    status = 'complete'
    completed = 1
    planned = 1
    elapsed_seconds = 1.0
})
[IO.File]::WriteAllText($OptionalResultsPath, "{`"point_id`":`"p1`"}`n", [Text.UTF8Encoding]::new($false))
$OptionalConfig = @{
    JobName = 'Optional terminal status test'
    JobId = 'optional-terminal-status-v1-3'
    Driver = $Driver
    Output = $OptionalRoot
    TotalPoints = 1
    StatusPath = $OptionalStatusPath
    ResultsPath = $OptionalResultsPath
    ControlDirectory = Join-Path $OptionalRoot 'control'
    StdoutPath = Join-Path $OptionalRoot 'stdout.log'
    StderrPath = Join-Path $OptionalRoot 'stderr.log'
    MonitorIntervalSeconds = 1
    PointEstimateSeconds = 1
}
$OptionalSnapshot = Get-DurableJobSnapshot -Config $OptionalConfig
if ($OptionalSnapshot.Running -or $OptionalSnapshot.Completed -ne 1 -or $OptionalSnapshot.Planned -ne 1 -or $OptionalSnapshot.LatestPointId -ne '-') {
    throw 'Optional terminal status fields were not handled safely under StrictMode.'
}
if ((Get-DurableTerminalDisposition -Snapshot $OptionalSnapshot) -ne 'success') { throw 'Optional terminal status was not classified as success.' }
if ([string]::IsNullOrWhiteSpace($OptionalSnapshot.SystemDriveRoot) -or
    [string]::IsNullOrWhiteSpace($OptionalSnapshot.OutputDriveRoot)) {
    throw 'Portable system/output drive discovery failed.'
}
$ResourceConfig = @{
    Output = $OptionalRoot
    MinimumFreeRamGiB = 0
    MinimumFreeSystemDriveGiB = 0
    MinimumFreeOutputDriveGiB = 0
}
$ResourceSnapshot = & (Get-Module DurableLauncher) {
    param($Value)
    Test-DurableResourcePolicy -Config $Value
} $ResourceConfig
if ($ResourceSnapshot.SystemDriveRoot -ne $OptionalSnapshot.SystemDriveRoot -or
    $ResourceSnapshot.OutputDriveRoot -ne $OptionalSnapshot.OutputDriveRoot) {
    throw 'Resource policy and monitor disagree on portable drive identity.'
}
if (@(Get-ChildItem -LiteralPath $OptionalRoot -File | Where-Object { $_.Name -match '\.(tmp|backup)\.' }).Count -ne 0) {
    throw 'Atomic overwrite left a temporary or backup artifact.'
}

$script:Answers = [System.Collections.Generic.Queue[string]]::new()
$script:Answers.Enqueue('invalid')
$script:Answers.Enqueue('RUN')
$Choice = Read-DurableValidatedChoice -Prompt 'test' -Choices @('RUN','CANCEL') -InputProvider { param($Prompt) $script:Answers.Dequeue() }
if ($Choice -ne 'RUN') { throw 'Validated-choice retry test failed.' }
if ((Resolve-DurableMonitorCommand -Command 'not-a-command') -ne 'invalid') { throw 'Invalid monitor command did not remain nonterminal.' }
if ((Resolve-DurableMonitorCommand -Command 'PAUSE') -ne 'pause') { throw 'Pause command normalization failed.' }
$SyntheticInventory = @(
    [pscustomobject]@{ ProcessId = 1001; Name = 'comsol-mcp.exe'; CommandLine = 'comsol-mcp.exe' },
    [pscustomobject]@{ ProcessId = 1002; Name = 'COMSOL-MCP.EXE'; CommandLine = 'COMSOL-MCP.EXE' },
    [pscustomobject]@{ ProcessId = 2001; Name = 'comsol.exe'; CommandLine = 'comsol.exe' },
    [pscustomobject]@{ ProcessId = 2002; Name = 'comsolbatch.exe'; CommandLine = 'comsolbatch.exe' },
    [pscustomobject]@{ ProcessId = 2003; Name = 'comsolmphserver.exe'; CommandLine = 'comsolmphserver.exe' },
    [pscustomobject]@{ ProcessId = 2004; Name = 'mphserver.exe'; CommandLine = 'mphserver.exe' },
    [pscustomobject]@{ ProcessId = 2005; Name = 'java.exe'; CommandLine = 'java.exe comsol runtime' },
    [pscustomobject]@{ ProcessId = 3001; Name = 'java.exe'; CommandLine = 'java.exe unrelated.jar' },
    [pscustomobject]@{ ProcessId = 3002; Name = 'python.exe'; CommandLine = 'python.exe server.py' }
)
$SolverCollisions = @(& (Get-Module DurableLauncher) {
    param([object[]]$Inventory)
    Test-DurableSolverCollision -ProcessInventory $Inventory
} $SyntheticInventory)
$CollisionIds = @($SolverCollisions.ProcessId | Sort-Object)
if (($CollisionIds -join ',') -ne '2001,2002,2003,2004,2005') {
    throw "Solver collision classification mismatch: $($CollisionIds -join ',')"
}
$ModeCombinationRejected = $false
try {
    Invoke-DurableJobLauncher -Config @{} -Run -Monitor -NoTopMost
}
catch {
    if ($_.Exception.Message -notmatch 'mutually exclusive') { throw }
    $ModeCombinationRejected = $true
}
if (-not $ModeCombinationRejected) { throw 'Combined -Run -Monitor mode was not rejected.' }
$script:FailureAnswers = [System.Collections.Generic.Queue[string]]::new()
$script:FailureAnswers.Enqueue('invalid')
$script:FailureAnswers.Enqueue('quit')
$FailureRecord = New-Object System.Management.Automation.ErrorRecord(
    (New-Object System.IO.FileNotFoundException('Required file is missing: missing_driver.py')),
    'MissingDriver',
    [System.Management.Automation.ErrorCategory]::ObjectNotFound,
    $null
)
$FailureHold = Show-DurableLauncherFailure -ErrorRecord $FailureRecord -LauncherPath 'test_launcher.ps1' -NoTopMost -InputProvider { param($Prompt) $script:FailureAnswers.Dequeue() }
if ($FailureHold -ne 'quit' -or $script:FailureAnswers.Count -ne 0) { throw 'Fatal-error invalid-input retry or quit latch failed.' }
$SuccessDisposition = Get-DurableTerminalDisposition -Snapshot ([pscustomobject]@{ Running = $false; Status = 'solver_and_postprocess_complete_unclassified'; Completed = 8; Planned = 8 })
if ($SuccessDisposition -ne 'success') { throw 'Successful terminal disposition was not recognized.' }
$FailureDisposition = Get-DurableTerminalDisposition -Snapshot ([pscustomobject]@{ Running = $false; Status = 'failed'; Completed = 3; Planned = 8 })
if ($FailureDisposition -ne 'failed') { throw 'Failed terminal disposition was not recognized.' }
$PausedDisposition = Get-DurableTerminalDisposition -Snapshot ([pscustomobject]@{ Running = $false; Status = 'paused_after_point'; Completed = 3; Planned = 8 })
if ($PausedDisposition -ne 'paused') { throw 'Paused terminal disposition was not recognized.' }

$First = $null
$Second = $null
try {
    $First = Start-FakeDriver
    Wait-Until -Condition {
        if (-not (Test-Path -LiteralPath $Config.StatusPath -PathType Leaf)) { return $false }
        try { return $null -ne ((Get-Content -LiteralPath $Config.StatusPath -Raw | ConvertFrom-Json).active_point_id) }
        catch { return $false }
    }
    $Detected = @(Get-DurableDriverProcesses -DriverPath $Driver)
    if ($Detected.Count -ne 1 -or $Detected[0].ProcessId -ne $First.Id) { throw 'Exact duplicate-driver detection failed.' }
    $Request = Write-DurablePauseRequest -Config $Config -ExpectedSpecId 'fake-spec-v1'
    Wait-Until -Condition { $First.Refresh(); return $First.HasExited }
    $First.WaitForExit()
    if ($null -ne $First.ExitCode -and $First.ExitCode -ne 0) { throw "First fake driver failed with exit code $($First.ExitCode)." }
    $Paused = Get-Content -LiteralPath $Config.StatusPath -Raw | ConvertFrom-Json
    $PausedRows = @(Get-Content -LiteralPath $Config.ResultsPath).Count
    if ($Paused.status -ne 'paused_after_point') { throw 'Pause did not reach a terminal durable-boundary state.' }
    if ($Paused.completed -lt 0 -or $Paused.completed -ge 8) { throw 'Pause acknowledgement did not precede completion.' }
    if ($PausedRows -ne $Paused.completed) { throw 'Pause status and durable row count disagree.' }
    if (Test-Path -LiteralPath (Join-Path $TestRoot 'run.lock')) { throw 'Pause left the fake owner lock behind.' }
    $Ack = Join-Path $Config.ControlDirectory ("acks\$($Request.RequestId).json")
    if (-not (Test-Path -LiteralPath $Ack -PathType Leaf)) { throw 'Pause acknowledgement is missing.' }
    $AckValue = Get-Content -LiteralPath $Ack -Raw | ConvertFrom-Json
    if ($AckValue.completed -ne $Paused.completed) { throw 'Pause acknowledgement and status count disagree.' }

    $Second = & (Get-Module DurableLauncher) { param($Value) Start-DurableDriver -Config $Value } $Config
    Wait-Until -Condition { $Second.Refresh(); return $Second.HasExited }
    $Second.WaitForExit()
    if ($null -ne $Second.ExitCode -and $Second.ExitCode -ne 0) { throw "Resume fake driver failed with exit code $($Second.ExitCode)." }
    $Complete = Get-Content -LiteralPath $Config.StatusPath -Raw | ConvertFrom-Json
    $FinalRows = @(Get-Content -LiteralPath $Config.ResultsPath).Count
    if ($Complete.status -ne 'complete' -or $Complete.completed -ne 8 -or $FinalRows -ne 8) { throw 'Exact resume did not complete all eight unique rows.' }
    if (Test-Path -LiteralPath (Join-Path $TestRoot 'run.lock')) { throw 'Resume left the fake owner lock behind.' }

    $Snapshot = Get-DurableJobSnapshot -Config $Config
    if ($Snapshot.Running -or $Snapshot.Completed -ne 8 -or $Snapshot.Status -ne 'complete') { throw 'Completed snapshot is inconsistent.' }
    $TerminalDisposition = Get-DurableTerminalDisposition -Snapshot $Snapshot
    if ($TerminalDisposition -ne 'success') { throw 'Completed live snapshot was not latched as success.' }
}
finally {
    foreach ($OwnedProcess in @($First, $Second)) {
        if ($null -eq $OwnedProcess) { continue }
        try { $OwnedProcess.Refresh() } catch { }
        if (-not $OwnedProcess.HasExited) {
            Stop-Process -Id $OwnedProcess.Id -Force -ErrorAction SilentlyContinue
            try { $OwnedProcess.WaitForExit() } catch { }
        }
    }
    Remove-Item -LiteralPath (Join-Path $TestRoot 'run.lock') -Force -ErrorAction SilentlyContinue
}
$Receipt = [ordered]@{
    schema_name = 'durable_launcher.synthetic_test_receipt.v1'
    status = 'pass'
    launcher_version = Get-DurableLauncherVersion
    test_root = $TestRoot
    invalid_input_retried = $true
    invalid_monitor_command_nonterminal = $true
    fatal_error_invalid_input_retried = $true
    fatal_error_quit_latched = $true
    optional_terminal_fields_safe = $true
    portable_drive_policy = $true
    atomic_overwrite_backup_cleanup = $true
    monitor_refresh_has_no_clear_host = $true
    monitor_refresh_has_no_sequential_host_writes = $true
    monitor_frame_single_buffered_write = $true
    monitor_frame_accepts_blank_separator_lines = $true
    monitor_frame_supports_selective_highlights = $true
    terminal_success_latched = $true
    failed_terminal_classified = $true
    paused_terminal_classified = $true
    exact_duplicate_detection = $true
    idle_mcp_host_not_solver_collision = $true
    real_solver_processes_rejected = $true
    launcher_modes_mutually_exclusive = $true
    shared_module_start_driver = $true
    pause_request_id = $Request.RequestId
    paused_after_rows = [int]$Paused.completed
    pause_ack_sha256 = Get-TestSha256 -Path $Ack
    resume_rows = $FinalRows
    lock_absent_after_pause_and_resume = $true
    final_status_sha256 = Get-TestSha256 -Path $Config.StatusPath
}
$ReceiptPath = Join-Path $TestRoot 'test_receipt.json'
Write-DurableAtomicJson -Path $ReceiptPath -Value $Receipt
Write-Host "DURABLE_LAUNCHER_TEST_PASS receipt=$ReceiptPath sha256=$(Get-TestSha256 -Path $ReceiptPath)"
