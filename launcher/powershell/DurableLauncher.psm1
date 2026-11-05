Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:DurableLauncherVersion = '1.8.1'

function Get-DurableLauncherVersion {
    return $script:DurableLauncherVersion
}

function Get-DurableSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    $Resolved = [System.IO.Path]::GetFullPath($Path)
    $Stream = [System.IO.File]::Open(
        $Resolved,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $Algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $Bytes = $Algorithm.ComputeHash($Stream)
        return ([System.BitConverter]::ToString($Bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $Algorithm.Dispose()
        $Stream.Dispose()
    }
}

function Get-DurableDriveSnapshot {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    $Resolved = [System.IO.Path]::GetFullPath($Path)
    $Root = [System.IO.Path]::GetPathRoot($Resolved)
    if ([string]::IsNullOrWhiteSpace($Root) -or $Root.StartsWith('\\')) {
        throw "Durable launcher paths must use a local drive: $Path"
    }
    $Drive = New-Object System.IO.DriveInfo($Root)
    if (-not $Drive.IsReady) { throw "Durable launcher drive is unavailable: $Root" }
    return [pscustomobject]@{
        Root = $Drive.RootDirectory.FullName
        FreeGiB = [math]::Round($Drive.AvailableFreeSpace / 1GB, 2)
    }
}

function Test-DurableInteractiveInput {
    if (-not [Environment]::UserInteractive) { return $false }
    try { return -not [Console]::IsInputRedirected }
    catch { return $true }
}

function Write-DurableAtomicJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Value
    )
    $Directory = Split-Path -Parent $Path
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    }
    $Temporary = Join-Path $Directory ((Split-Path -Leaf $Path) + ".tmp.$PID.$([DateTime]::UtcNow.Ticks)")
    $Bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes(
        ($Value | ConvertTo-Json -Depth 16) + [Environment]::NewLine
    )
    $Stream = New-Object System.IO.FileStream(
        $Temporary,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    try {
        $Stream.Write($Bytes, 0, $Bytes.Length)
        $Stream.Flush($true)
    }
    finally { $Stream.Dispose() }
    for ($Attempt = 0; $Attempt -le 20; $Attempt++) {
        try {
            if (Test-Path -LiteralPath $Path -PathType Leaf) {
                $Backup = Join-Path $Directory ((Split-Path -Leaf $Path) + ".backup.$PID.$([DateTime]::UtcNow.Ticks)")
                [System.IO.File]::Replace($Temporary, $Path, $Backup)
                for ($CleanupAttempt = 0; $CleanupAttempt -le 20; $CleanupAttempt++) {
                    try {
                        if ([System.IO.File]::Exists($Backup)) { [System.IO.File]::Delete($Backup) }
                        break
                    }
                    catch [System.IO.IOException], [System.UnauthorizedAccessException] {
                        if ($CleanupAttempt -ge 20) {
                            throw "Atomic write committed, but owned backup cleanup failed: $Backup"
                        }
                        Start-Sleep -Milliseconds 50
                    }
                }
            }
            else {
                [System.IO.File]::Move($Temporary, $Path)
            }
            return
        }
        catch [System.IO.IOException], [System.UnauthorizedAccessException] {
            if ($Attempt -ge 20) {
                if (Test-Path -LiteralPath $Temporary -PathType Leaf) {
                    [System.IO.File]::Delete($Temporary)
                }
                throw
            }
            Start-Sleep -Milliseconds 50
        }
    }
}

function Read-DurableValidatedChoice {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string[]]$Choices,
        [hashtable]$Aliases = @{},
        [scriptblock]$InputProvider
    )
    $Canonical = @{}
    foreach ($Choice in $Choices) { $Canonical[$Choice.ToUpperInvariant()] = $Choice }
    while ($true) {
        $Raw = if ($null -ne $InputProvider) { & $InputProvider $Prompt } else { Read-Host $Prompt }
        $Text = if ($null -eq $Raw) { '' } else { ([string]$Raw).Trim() }
        $Upper = $Text.ToUpperInvariant()
        if ($Canonical.ContainsKey($Upper)) { return $Canonical[$Upper] }
        if ($Aliases.ContainsKey($Upper)) {
            $Target = ([string]$Aliases[$Upper]).ToUpperInvariant()
            if ($Canonical.ContainsKey($Target)) { return $Canonical[$Target] }
        }
        Write-Host ("Invalid input '{0}'. Enter one of: {1}." -f $Text, ($Choices -join ', ')) -ForegroundColor Yellow
    }
}

function Resolve-DurableMonitorCommand {
    [CmdletBinding()]
    param([AllowEmptyString()][string]$Command)
    $Value = if ($null -eq $Command) { '' } else { $Command.Trim().ToLowerInvariant() }
    switch ($Value) {
        'pause' { return 'pause' }
        'p' { return 'pause' }
        'status' { return 'status' }
        's' { return 'status' }
        'help' { return 'help' }
        '?' { return 'help' }
        'quit' { return 'quit' }
        'q' { return 'quit' }
        'close' { return 'quit' }
        'resume' { return 'resume' }
        'r' { return 'resume' }
        '' { return 'empty' }
        default { return 'invalid' }
    }
}

function Show-DurableLauncherFailure {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord,
        [string]$LauncherPath,
        [ValidateSet('active', 'absent', 'unknown')][string]$DriverState = 'unknown',
        [switch]$NoHold,
        [switch]$NoTopMost,
        [scriptblock]$InputProvider
    )
    if (-not $NoTopMost) { [void](Set-DurableConsoleTopMost -Enabled $true) }
    try {
        $Exception = $ErrorRecord.Exception
        Write-Host '========================================' -ForegroundColor Red
        Write-Host '        LAUNCHER STARTUP FAILED         ' -ForegroundColor Red
        Write-Host '========================================' -ForegroundColor Red
        Write-Host ("Type:     {0}" -f $Exception.GetType().FullName) -ForegroundColor Red
        Write-Host ("Message:  {0}" -f $Exception.Message) -ForegroundColor Red
        if (-not [string]::IsNullOrWhiteSpace($LauncherPath)) {
            Write-Host ("Launcher: {0}" -f $LauncherPath)
        }
        Write-Host ("Category: {0}" -f $ErrorRecord.CategoryInfo.Category)
        if ($DriverState -eq 'active') {
            Write-Host 'An exact driver is still active. Do not start another launcher.' -ForegroundColor Yellow
        }
        elseif ($DriverState -eq 'absent') {
            Write-Host 'No active exact driver was detected after this failure.' -ForegroundColor Yellow
        }
        else {
            Write-Host 'Exact driver state could not be verified. Do not start another launcher until ownership is checked.' -ForegroundColor Yellow
        }
        if ($NoHold) { return 'no_hold' }
        $CanRead = $null -ne $InputProvider -or (Test-DurableInteractiveInput)
        if (-not $CanRead) { return 'noninteractive' }
        while ($true) {
            $Raw = if ($null -ne $InputProvider) { & $InputProvider 'Enter quit to close' } else { Read-Host 'Enter quit to close' }
            $Command = if ($null -eq $Raw) { '' } else { ([string]$Raw).Trim().ToLowerInvariant() }
            if ($Command -in @('quit', 'q', 'close')) { return 'quit' }
            Write-Host ("Invalid input '{0}'. Enter quit." -f $Command) -ForegroundColor Yellow
        }
    }
    finally {
        if (-not $NoTopMost) { [void](Set-DurableConsoleTopMost -Enabled $false) }
    }
}

function Get-DurableTerminalDisposition {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Snapshot)
    if ([bool]$Snapshot.Running) { return 'running' }
    $Status = ([string]$Snapshot.Status).Trim().ToLowerInvariant()
    if (
        [int]$Snapshot.Completed -ge [int]$Snapshot.Planned -and
        $Status -in @(
            'complete',
            'completed',
            'succeeded',
            'success',
            'solver_and_postprocess_complete_unclassified'
        )
    ) {
        return 'success'
    }
    if ($Status -match 'gate_failed|threshold_not_met|quality_not_met|policy_refused|scientific_not_accepted|acceptance_failed|mesh_not_converged') {
        return 'not_accepted'
    }
    if ($Status -match 'fail|error|abort|cancel') { return 'failed' }
    if ($Status -match 'pause|wall_budget|partial') { return 'paused' }
    return 'stopped'
}

function Set-DurableConsoleTopMost {
    [CmdletBinding()]
    param([bool]$Enabled = $true)
    if (-not ('DurableLauncher.NativeWindow' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace DurableLauncher {
    public static class NativeWindow {
        [DllImport("kernel32.dll")]
        public static extern IntPtr GetConsoleWindow();
        [DllImport("user32.dll")]
        public static extern bool SetWindowPos(
            IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint flags);
    }
}
'@
    }
    $Handle = [DurableLauncher.NativeWindow]::GetConsoleWindow()
    if ($Handle -eq [IntPtr]::Zero) { return $false }
    $InsertAfter = if ($Enabled) { [IntPtr](-1) } else { [IntPtr](-2) }
    $NoMoveSizeActivate = 0x0001 -bor 0x0002 -bor 0x0010
    return [DurableLauncher.NativeWindow]::SetWindowPos(
        $Handle, $InsertAfter, 0, 0, 0, 0, $NoMoveSizeActivate
    )
}

function Get-DurableDriverProcesses {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$DriverPath)
    $Normalized = [System.IO.Path]::GetFullPath($DriverPath)
    $CurrentPid = $PID
    $DriverProcesses = @(Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $CurrentPid -and
        $_.Name -match '^python(w)?\.exe$' -and
        ([string]$_.CommandLine).IndexOf($Normalized, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
    return @($DriverProcesses | Where-Object {
        $Child = $_
        $Redirected = $false
        foreach ($Parent in $DriverProcesses) {
            if ($Child.ParentProcessId -ne $Parent.ProcessId -or
                $Child.CommandLine -cne $Parent.CommandLine -or
                [string]::IsNullOrWhiteSpace([string]$Parent.ExecutablePath) -or
                [string]::IsNullOrWhiteSpace([string]$Child.ExecutablePath)) { continue }
            $Scripts = Split-Path -Parent $Parent.ExecutablePath
            if ((Split-Path -Leaf $Scripts) -ine 'Scripts') { continue }
            $ConfigPath = Join-Path (Split-Path -Parent $Scripts) 'pyvenv.cfg'
            if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { continue }
            $Homes = @(Get-Content -LiteralPath $ConfigPath | Where-Object { $_ -match '^home\s*=' })
            if ($Homes.Count -ne 1) { continue }
            $BaseHome = ($Homes[0] -split '=', 2)[1].Trim()
            $Expected = Join-Path $BaseHome (Split-Path -Leaf $Parent.ExecutablePath)
            if ([System.IO.Path]::GetFullPath($Expected) -ieq $Child.ExecutablePath -and
                $Parent.ExecutablePath -ine $Child.ExecutablePath -and
                $Parent.CreationDate -le $Child.CreationDate) { $Redirected = $true }
        }
        -not $Redirected
    })
}

function Get-DurableDescendantPids {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][int]$RootPid)
    $All = @(Get-CimInstance Win32_Process)
    $Found = New-Object System.Collections.Generic.List[int]
    $Queue = New-Object System.Collections.Generic.Queue[int]
    $Queue.Enqueue($RootPid)
    while ($Queue.Count -gt 0) {
        $Parent = $Queue.Dequeue()
        foreach ($Child in @($All | Where-Object { $_.ParentProcessId -eq $Parent })) {
            $Id = [int]$Child.ProcessId
            if (-not $Found.Contains($Id)) {
                $Found.Add($Id)
                $Queue.Enqueue($Id)
            }
        }
    }
    return @($Found)
}

function Get-DurableOwnerProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DriverPath,
        [Parameter(Mandatory = $true)][string]$OwnerPath
    )
    if (Test-Path -LiteralPath $OwnerPath -PathType Leaf) {
        try {
            $Owner = Get-Content -LiteralPath $OwnerPath -Raw | ConvertFrom-Json
            $Candidate = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$Owner.pid)" -ErrorAction SilentlyContinue
            $Normalized = [System.IO.Path]::GetFullPath($DriverPath)
            $CreationMatches = $null -ne $Candidate -and
                -not [string]::IsNullOrWhiteSpace([string]$Owner.creation_date) -and
                [string]$Candidate.CreationDate -eq [string]$Owner.creation_date
            if ($CreationMatches -and ([string]$Candidate.CommandLine).IndexOf($Normalized, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                return $Candidate
            }
        }
        catch {}
    }
    $Processes = @(Get-DurableDriverProcesses -DriverPath $DriverPath)
    if ($Processes.Count -eq 1) { return $Processes[0] }
    return $null
}

function Get-DurableLineCount {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 0 }
    try { return @(Get-Content -LiteralPath $Path | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count }
    catch { return 0 }
}

function Get-DurableOptionalProperty {
    param(
        [object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $InputObject) { return $null }
    $Property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $Property) { return $null }
    return $Property.Value
}

function ConvertTo-DurableBoundedText {
    param(
        [object]$Value,
        [int]$MaximumLength = 240
    )
    if ($null -eq $Value) { return $null }
    $RawText = if ($Value -is [double] -or $Value -is [single]) {
        $Value.ToString('R', [Globalization.CultureInfo]::InvariantCulture)
    }
    else { [string]$Value }
    $Text = $RawText -replace '[\r\n\t]+', ' '
    $Text = ($Text -replace '\s{2,}', ' ').Trim()
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    if ($Text.Length -le $MaximumLength) { return $Text }
    return $Text.Substring(0, [Math]::Max(0, $MaximumLength - 3)) + '...'
}

function Get-DurableTerminalExplanation {
    param(
        [object]$StatusData,
        [string]$StatusText
    )
    $Reason = $null
    foreach ($Name in @('reason', 'failure_reason', 'gate_reason', 'message', 'error')) {
        $Candidate = Get-DurableOptionalProperty -InputObject $StatusData -Name $Name
        $Reason = ConvertTo-DurableBoundedText -Value $Candidate -MaximumLength 180
        if ($null -ne $Reason) { break }
    }
    if ($null -eq $Reason) { $Reason = ConvertTo-DurableBoundedText -Value $StatusText -MaximumLength 180 }

    $Details = $null
    $GateName = if ($StatusText -match '^(?<name>.+)_failed$') { $Matches['name'] } else { $null }
    if (-not [string]::IsNullOrWhiteSpace($GateName)) {
        $Gate = Get-DurableOptionalProperty -InputObject $StatusData -Name $GateName
        if ($null -ne $Gate) {
            $Parts = [System.Collections.Generic.List[string]]::new()
            foreach ($Property in $Gate.PSObject.Properties) {
                if ($Property.Name -in @('evidence', 'results', 'rows', 'artifacts')) { continue }
                if ($Property.Value -is [System.Collections.IEnumerable] -and -not ($Property.Value -is [string])) { continue }
                $Value = ConvertTo-DurableBoundedText -Value $Property.Value -MaximumLength 60
                if ($null -ne $Value) { [void]$Parts.Add(("{0}={1}" -f $Property.Name, $Value)) }
                if ($Parts.Count -ge 5) { break }
            }
            if ($Parts.Count -gt 0) {
                $Details = ConvertTo-DurableBoundedText -Value ($Parts -join '; ') -MaximumLength 300
            }
        }
    }
    return [pscustomobject]@{ Reason = $Reason; Details = $Details }
}

function Get-DurableJobSnapshot {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][hashtable]$Config)
    $OwnerPath = Join-Path $Config.Output 'launcher_owner.json'
    $Owner = Get-DurableOwnerProcess -DriverPath $Config.Driver -OwnerPath $OwnerPath
    $Running = $null -ne $Owner
    $StatusData = $null
    if (Test-Path -LiteralPath $Config.StatusPath -PathType Leaf) {
        try { $StatusData = Get-Content -LiteralPath $Config.StatusPath -Raw | ConvertFrom-Json }
        catch {}
    }
    $StatusCompleted = Get-DurableOptionalProperty -InputObject $StatusData -Name 'completed'
    $StatusPlanned = Get-DurableOptionalProperty -InputObject $StatusData -Name 'planned'
    $StatusLatest = Get-DurableOptionalProperty -InputObject $StatusData -Name 'latest_point_id'
    $StatusElapsed = Get-DurableOptionalProperty -InputObject $StatusData -Name 'elapsed_seconds'
    $StatusState = Get-DurableOptionalProperty -InputObject $StatusData -Name 'status'
    $StatusSpecId = Get-DurableOptionalProperty -InputObject $StatusData -Name 'spec_id'
    $Completed = if ($null -ne $StatusCompleted) {
        [int]$StatusCompleted
    }
    else { Get-DurableLineCount -Path $Config.ResultsPath }
    $Planned = if ($null -ne $StatusPlanned) {
        [int]$StatusPlanned
    }
    else { [int]$Config.TotalPoints }
    $Latest = if ($null -ne $StatusLatest) {
        [string]$StatusLatest
    }
    else { '-' }
    $ElapsedSeconds = if ($null -ne $StatusElapsed) {
        [double]$StatusElapsed
    }
    else { 0.0 }
    $AverageSeconds = if ($Completed -gt 0 -and $ElapsedSeconds -gt 0) {
        $ElapsedSeconds / $Completed
    }
    elseif ($Config.ContainsKey('PointEstimateSeconds')) { [double]$Config.PointEstimateSeconds }
    else { 0.0 }
    $RemainingSeconds = if ($AverageSeconds -gt 0) {
        [math]::Max(0.0, ($Planned - $Completed) * $AverageSeconds)
    }
    else { 0.0 }
    $CpuSeconds = 0.0
    $WorkingSetBytes = 0L
    $ProcessIds = @()
    if ($Running) {
        $ProcessIds = @([int]$Owner.ProcessId) + @(Get-DurableDescendantPids -RootPid ([int]$Owner.ProcessId))
        foreach ($Id in $ProcessIds) {
            $Native = Get-Process -Id $Id -ErrorAction SilentlyContinue
            if ($null -ne $Native) {
                if ($null -ne $Native.CPU) { $CpuSeconds += [double]$Native.CPU }
                $WorkingSetBytes += [long]$Native.WorkingSet64
            }
        }
    }
    $Os = Get-CimInstance Win32_OperatingSystem
    $SystemDrive = Get-DurableDriveSnapshot -Path ([Environment]::SystemDirectory)
    $OutputDrive = Get-DurableDriveSnapshot -Path $Config.Output
    $SystemCpu = @(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
    $CommitRemainingGiB = $null
    try {
        $Memory = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
        $CommitRemainingGiB = [math]::Round(([double]$Memory.CommitLimit - [double]$Memory.CommittedBytes) / 1GB, 2)
    }
    catch {}
    $StatusText = if ($null -ne $StatusState) {
        [string]$StatusState
    }
    elseif ($Running) { 'running' }
    elseif ($Completed -ge $Planned) { 'completed' }
    elseif ($Completed -gt 0) { 'partial_not_running' }
    else { 'not_running' }
    $Explanation = Get-DurableTerminalExplanation -StatusData $StatusData -StatusText $StatusText
    return [pscustomobject]@{
        Running = $Running
        Status = $StatusText
        Completed = $Completed
        Planned = $Planned
        LatestPointId = $Latest
        ElapsedSeconds = $ElapsedSeconds
        AverageSeconds = $AverageSeconds
        RemainingSeconds = $RemainingSeconds
        OwnerPid = if ($Running) { [int]$Owner.ProcessId } else { $null }
        ProcessIds = $ProcessIds
        ProcessCpuSeconds = $CpuSeconds
        ProcessWorkingSetGiB = [math]::Round($WorkingSetBytes / 1GB, 3)
        SystemCpuPercent = if ($null -eq $SystemCpu) { $null } else { [math]::Round([double]$SystemCpu, 1) }
        FreeRamGiB = [math]::Round($Os.FreePhysicalMemory / 1MB, 2)
        CommitRemainingGiB = $CommitRemainingGiB
        SystemDriveRoot = $SystemDrive.Root
        SystemFreeGiB = $SystemDrive.FreeGiB
        OutputDriveRoot = $OutputDrive.Root
        OutputFreeGiB = $OutputDrive.FreeGiB
        SpecId = if ($null -ne $StatusSpecId) { [string]$StatusSpecId } else { $null }
        TerminalReason = $Explanation.Reason
        TerminalDetails = $Explanation.Details
    }
}

function Write-DurablePauseRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][hashtable]$Config,
        [string]$ExpectedSpecId
    )
    $Requests = Join-Path $Config.ControlDirectory 'requests'
    $Acks = Join-Path $Config.ControlDirectory 'acks'
    foreach ($Path in @($Requests, $Acks)) {
        if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
            New-Item -ItemType Directory -Path $Path -Force | Out-Null
        }
    }
    foreach ($Existing in @(Get-ChildItem -LiteralPath $Requests -File -Filter '*.json' -ErrorAction SilentlyContinue)) {
        try {
            $Request = Get-Content -LiteralPath $Existing.FullName -Raw | ConvertFrom-Json
            $Ack = Join-Path $Acks ("$($Request.request_id).json")
            if ($Request.job_id -eq $Config.JobId -and -not (Test-Path -LiteralPath $Ack -PathType Leaf)) {
                return [pscustomobject]@{ RequestId = [string]$Request.request_id; Path = $Existing.FullName; Existing = $true }
            }
        }
        catch {}
    }
    $RequestId = 'pause-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 10)
    $Path = Join-Path $Requests ("$RequestId.json")
    $Payload = [ordered]@{
        schema_name = 'durable_launcher.pause_request.v1'
        schema_version = 1
        request_id = $RequestId
        action = 'pause_after_current_point'
        job_id = [string]$Config.JobId
        expected_spec_id = if ([string]::IsNullOrWhiteSpace($ExpectedSpecId)) { $null } else { $ExpectedSpecId }
        requested_at_utc = [DateTime]::UtcNow.ToString('o')
        requester_pid = $PID
    }
    Write-DurableAtomicJson -Path $Path -Value $Payload
    return [pscustomobject]@{ RequestId = $RequestId; Path = $Path; Existing = $false }
}

function Write-DurableMonitorFrame {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][AllowEmptyString()][string[]]$Lines,
        [Parameter(Mandatory = $true)][hashtable]$State,
        [hashtable]$Highlights = @{}
    )
    $Redirected = $false
    try { $Redirected = [Console]::IsOutputRedirected }
    catch {}
    if ($Redirected) {
        [Console]::WriteLine(($Lines -join [Environment]::NewLine))
        return
    }

    try {
        $Width = [Math]::Max(40, [int]$Host.UI.RawUI.WindowSize.Width - 1)
        $PreviousCount = if ($State.ContainsKey('LineCount')) { [int]$State.LineCount } else { 0 }
        $Count = [Math]::Max($PreviousCount, $Lines.Count)
        $Padded = [System.Collections.Generic.List[string]]::new()
        for ($Index = 0; $Index -lt $Count; $Index++) {
            $Line = if ($Index -lt $Lines.Count) { [string]$Lines[$Index] } else { '' }
            if ($Line.Length -gt $Width) { $Line = $Line.Substring(0, $Width) }
            [void]$Padded.Add($Line.PadRight($Width))
        }
        if (-not $State.ContainsKey('Initialized') -or -not [bool]$State.Initialized) {
            Clear-Host
            $State.Initialized = $true
            $State.Top = 0
        }
        [Console]::SetCursorPosition(0, [int]$State.Top)
        $OldColor = $Host.UI.RawUI.ForegroundColor
        try {
            [Console]::Write(($Padded -join [Environment]::NewLine))
        }
        finally { $Host.UI.RawUI.ForegroundColor = $OldColor }
        foreach ($Entry in $Highlights.GetEnumerator()) {
            $LineIndex = [int]$Entry.Key
            if ($LineIndex -lt 0 -or $LineIndex -ge $Lines.Count) { continue }
            [Console]::SetCursorPosition(0, [int]$State.Top + $LineIndex)
            try {
                $Host.UI.RawUI.ForegroundColor = [ConsoleColor]([string]$Entry.Value)
                [Console]::Write($Padded[$LineIndex])
            }
            finally { $Host.UI.RawUI.ForegroundColor = $OldColor }
        }
        $State.LineCount = $Lines.Count
        $PromptLength = if ($Lines.Count -gt 0) { ([string]$Lines[-1]).Length } else { 0 }
        [Console]::SetCursorPosition([Math]::Min($PromptLength, $Width - 1), [int]$State.Top + $Lines.Count - 1)
    }
    catch {
        [Console]::WriteLine(($Lines -join [Environment]::NewLine))
    }
}

function Show-DurableJobMonitor {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][hashtable]$Config,
        [switch]$NoTopMost
    )
    if (-not $NoTopMost) { [void](Set-DurableConsoleTopMost -Enabled $true) }
    $Buffer = ''
    $Message = 'Commands: pause, status, help, resume, quit'
    $LastRender = [DateTime]::MinValue
    $Snapshot = $null
    $Disposition = 'running'
    $LastDisposition = $null
    $RenderState = @{ Initialized = $false; Top = 0; LineCount = 0 }
    try {
        while ($true) {
            $Submitted = $null
            try {
                while ([Console]::KeyAvailable) {
                    $Key = [Console]::ReadKey($true)
                    if ($Key.Key -eq [ConsoleKey]::Enter) {
                        $Submitted = $Buffer
                        $Buffer = ''
                        $LastRender = [DateTime]::MinValue
                        break
                    }
                    if ($Key.Key -eq [ConsoleKey]::Backspace) {
                        if ($Buffer.Length -gt 0) {
                            $Buffer = $Buffer.Substring(0, $Buffer.Length - 1)
                            $LastRender = [DateTime]::MinValue
                        }
                        continue
                    }
                    if (-not [char]::IsControl($Key.KeyChar)) {
                        $Buffer += $Key.KeyChar
                        $LastRender = [DateTime]::MinValue
                    }
                }
            }
            catch {}
            if ($null -ne $Submitted) {
                $Command = Resolve-DurableMonitorCommand -Command $Submitted
                switch ($Command) {
                    'pause' {
                        $Snapshot = Get-DurableJobSnapshot -Config $Config
                        if ($Snapshot.Running) {
                            $Request = Write-DurablePauseRequest -Config $Config -ExpectedSpecId $Snapshot.SpecId
                            $Message = "Pause requested: $($Request.RequestId). Current point will finish first."
                        }
                        else { $Message = 'No active worker. Pause was not requested.' }
                    }
                    'status' { $Message = 'Status refreshed.'; $LastRender = [DateTime]::MinValue }
                    'help' { $Message = 'pause: stop after current durable point; resume: restart exact job; quit: close monitor only.' }
                    'resume' {
                        $Snapshot = Get-DurableJobSnapshot -Config $Config
                        $Disposition = Get-DurableTerminalDisposition -Snapshot $Snapshot
                        if ($Snapshot.Running) { $Message = 'The job is already running; resume is not applicable.' }
                        elseif ($Disposition -eq 'success') { $Message = 'The job is already complete. Enter quit to close this window.' }
                        else { return 'resume' }
                    }
                    'quit' { return 'quit' }
                    'empty' { $Message = 'Enter pause, status, help, resume, or quit.' }
                    default { $Message = "Unknown command '$Submitted'. Enter pause, status, help, resume, or quit." }
                }
            }
            if (([DateTime]::UtcNow - $LastRender).TotalSeconds -ge [double]$Config.MonitorIntervalSeconds) {
                try {
                    $Snapshot = Get-DurableJobSnapshot -Config $Config
                    $Disposition = Get-DurableTerminalDisposition -Snapshot $Snapshot
                }
                catch {
                    $RefreshError = $_.Exception.Message
                    $Message = "Monitor refresh failed; terminal remains open: $RefreshError"
                    try { $Host.UI.RawUI.WindowTitle = "$($Config.JobName) MONITOR REFRESH FAILED" } catch {}
                    try {
                        $Frame = [System.Collections.Generic.List[string]]::new()
                        $Highlights = @{}
                        [void]$Frame.Add($Config.JobName)
                        [void]$Frame.Add('========================================')
                        [void]$Frame.Add('        MONITOR REFRESH FAILED          ')
                        [void]$Frame.Add('========================================')
                        [void]$Frame.Add(("Message: {0}" -f $RefreshError))
                        foreach ($LineIndex in 1..4) { $Highlights[$LineIndex] = 'Red' }
                        if ($null -ne $Snapshot) {
                            [void]$Frame.Add(("Last verified progress: {0}/{1}; status {2}" -f $Snapshot.Completed, $Snapshot.Planned, $Snapshot.Status))
                        }
                        [void]$Frame.Add(("Status path: {0}" -f $Config.StatusPath))
                        $Highlights[$Frame.Count] = 'Yellow'
                        [void]$Frame.Add('The displayed progress is not current. Do not start another launcher.')
                        [void]$Frame.Add(("command> {0}" -f $Buffer))
                        Write-DurableMonitorFrame -Lines $Frame.ToArray() -State $RenderState -Highlights $Highlights
                    }
                    catch {
                        try {
                            [Console]::WriteLine("MONITOR REFRESH FAILED: $RefreshError")
                            [Console]::WriteLine('The displayed progress is not current. Do not start another launcher.')
                        }
                        catch {}
                    }
                    $LastRender = [DateTime]::UtcNow
                    Start-Sleep -Milliseconds 100
                    continue
                }
                if ($Disposition -ne $LastDisposition) {
                    switch ($Disposition) {
                        'success' { $Message = 'Completed successfully. Enter quit to close this window.' }
                        'failed' { $Message = 'The driver failed. Reason and detailed log paths are shown in red.' }
                        'not_accepted' { $Message = 'Scientific or quality requirements were not met. Reason and detailed log paths are shown in yellow.' }
                        'paused' { $Message = 'Stopped at a durable boundary. Enter resume to continue or quit to close.' }
                        'stopped' { $Message = 'Driver stopped without a verified success state. Review logs before closing.' }
                    }
                    $LastDisposition = $Disposition
                }
                $Percent = if ($Snapshot.Planned -gt 0) { 100.0 * $Snapshot.Completed / $Snapshot.Planned } else { 0.0 }
                $TitleState = if ($Disposition -eq 'success') { 'COMPLETED' } elseif ($Disposition -eq 'failed') { 'FAILED' } elseif ($Disposition -eq 'not_accepted') { 'NOT ACCEPTED' } else { $Snapshot.Status }
                try { $Host.UI.RawUI.WindowTitle = "$($Config.JobName) $TitleState $($Snapshot.Completed)/$($Snapshot.Planned)" } catch {}
                $Frame = [System.Collections.Generic.List[string]]::new()
                $Highlights = @{}
                [void]$Frame.Add($Config.JobName)
                switch ($Disposition) {
                    'success' {
                        [void]$Frame.Add('========================================')
                        [void]$Frame.Add('         COMPLETED SUCCESSFULLY         ')
                        [void]$Frame.Add('========================================')
                        foreach ($LineIndex in 1..3) { $Highlights[$LineIndex] = 'Green' }
                    }
                    'failed' {
                        [void]$Frame.Add('========================================')
                        [void]$Frame.Add('                 FAILED                 ')
                        [void]$Frame.Add(("Reason: {0}" -f $Snapshot.TerminalReason))
                        [void]$Frame.Add('========================================')
                        foreach ($LineIndex in 1..4) { $Highlights[$LineIndex] = 'Red' }
                    }
                    'not_accepted' {
                        [void]$Frame.Add('========================================')
                        [void]$Frame.Add('    SCIENTIFIC / QUALITY GATE NOT MET   ')
                        [void]$Frame.Add(("Reason: {0}" -f $Snapshot.TerminalReason))
                        [void]$Frame.Add('========================================')
                        foreach ($LineIndex in 1..4) { $Highlights[$LineIndex] = 'Yellow' }
                    }
                    'paused' {
                        if ($Snapshot.Status -match 'pause') {
                            [void]$Frame.Add('========================================')
                            [void]$Frame.Add('                 PAUSED                 ')
                            [void]$Frame.Add(("Reason: {0}" -f $Snapshot.TerminalReason))
                            [void]$Frame.Add('========================================')
                            foreach ($LineIndex in 1..4) { $Highlights[$LineIndex] = 'Blue' }
                        }
                        else {
                            [void]$Frame.Add('PAUSED AT DURABLE BOUNDARY')
                            $Highlights[1] = 'Yellow'
                        }
                    }
                    'stopped' {
                        [void]$Frame.Add('========================================')
                        [void]$Frame.Add('STOPPED WITHOUT VERIFIED SUCCESS')
                        [void]$Frame.Add(("Reason: {0}" -f $Snapshot.TerminalReason))
                        [void]$Frame.Add('========================================')
                        foreach ($LineIndex in 1..4) { $Highlights[$LineIndex] = 'Red' }
                    }
                }
                [void]$Frame.Add(("Status:   {0}" -f $Snapshot.Status))
                $Highlights[$Frame.Count - 1] = if ($Disposition -eq 'success') { 'Green' } elseif ($Disposition -eq 'failed' -or $Disposition -eq 'stopped') { 'Red' } elseif ($Disposition -eq 'paused' -and $Snapshot.Status -match 'pause') { 'Blue' } else { 'Yellow' }
                if ($Disposition -eq 'failed' -and -not [string]::IsNullOrWhiteSpace($Snapshot.TerminalDetails)) {
                    $DetailsLine = $Frame.Count
                    [void]$Frame.Add(("Details:  {0}" -f $Snapshot.TerminalDetails))
                    $Highlights[$DetailsLine] = 'Red'
                }
                if ($Disposition -eq 'not_accepted' -and -not [string]::IsNullOrWhiteSpace($Snapshot.TerminalDetails)) {
                    $DetailsLine = $Frame.Count
                    [void]$Frame.Add(("Details:  {0}" -f $Snapshot.TerminalDetails))
                    $Highlights[$DetailsLine] = 'Yellow'
                }
                if ($Disposition -in @('failed', 'not_accepted', 'paused', 'stopped')) {
                    $TerminalPathColor = if ($Disposition -eq 'failed' -or $Disposition -eq 'stopped') { 'Red' } elseif ($Disposition -eq 'paused' -and $Snapshot.Status -match 'pause') { 'Blue' } else { 'Yellow' }
                    foreach ($PathLine in @(
                        ("Status JSON: {0}" -f $Config.StatusPath),
                        ("Driver log:  {0}" -f $Config.StdoutPath),
                        ("Error log:   {0}" -f $Config.StderrPath)
                    )) {
                        $PathLineIndex = $Frame.Count
                        [void]$Frame.Add($PathLine)
                        $Highlights[$PathLineIndex] = $TerminalPathColor
                    }
                }
                [void]$Frame.Add(("Progress: {0}/{1} ({2:N1}%)" -f $Snapshot.Completed, $Snapshot.Planned, $Percent))
                [void]$Frame.Add(("Latest:   {0}" -f $Snapshot.LatestPointId))
                [void]$Frame.Add(("Elapsed:  {0}" -f [TimeSpan]::FromSeconds($Snapshot.ElapsedSeconds)))
                [void]$Frame.Add(("ETA:      {0}" -f [TimeSpan]::FromSeconds($Snapshot.RemainingSeconds)))
                [void]$Frame.Add(("Owner:    PID {0}; tree {1}" -f $(if ($null -eq $Snapshot.OwnerPid) { '-' } else { $Snapshot.OwnerPid }), $(if ($Snapshot.ProcessIds.Count -eq 0) { '-' } else { $Snapshot.ProcessIds -join ',' })))
                [void]$Frame.Add(("Process:  CPU {0:N1} s; working set {1:N3} GiB" -f $Snapshot.ProcessCpuSeconds, $Snapshot.ProcessWorkingSetGiB))
                [void]$Frame.Add(("System:   CPU {0}%; free RAM {1:N2} GiB; commit left {2} GiB" -f $Snapshot.SystemCpuPercent, $Snapshot.FreeRamGiB, $(if ($null -eq $Snapshot.CommitRemainingGiB) { '-' } else { $Snapshot.CommitRemainingGiB })))
                [void]$Frame.Add(("Storage:  system {0} {1:N2} GiB free; output {2} {3:N2} GiB free" -f $Snapshot.SystemDriveRoot, $Snapshot.SystemFreeGiB, $Snapshot.OutputDriveRoot, $Snapshot.OutputFreeGiB))
                if (Test-Path -LiteralPath $Config.StdoutPath -PathType Leaf) {
                    [void]$Frame.Add('')
                    [void]$Frame.Add('Recent log:')
                    foreach ($Line in @(Get-Content -LiteralPath $Config.StdoutPath -Tail 6)) { [void]$Frame.Add([string]$Line) }
                }
                if ((Test-Path -LiteralPath $Config.StderrPath -PathType Leaf) -and (Get-Item -LiteralPath $Config.StderrPath).Length -gt 0) {
                    [void]$Frame.Add('')
                    [void]$Frame.Add('Errors:')
                    foreach ($Line in @(Get-Content -LiteralPath $Config.StderrPath -Tail 6)) { [void]$Frame.Add([string]$Line) }
                }
                [void]$Frame.Add('')
                $MessageLineIndex = $Frame.Count
                [void]$Frame.Add($Message)
                [void]$Frame.Add(("command> {0}" -f $Buffer))
                $Highlights[$MessageLineIndex] = if ($Disposition -eq 'success') { 'Green' } elseif ($Disposition -in @('failed','stopped')) { 'Red' } elseif ($Disposition -eq 'paused' -and $Snapshot.Status -match 'pause') { 'Blue' } else { 'Yellow' }
                Write-DurableMonitorFrame -Lines $Frame.ToArray() -State $RenderState -Highlights $Highlights
                $LastRender = [DateTime]::UtcNow
            }
            Start-Sleep -Milliseconds 100
        }
    }
    finally {
        if (-not $NoTopMost) { [void](Set-DurableConsoleTopMost -Enabled $false) }
    }
}

function Test-DurableResourcePolicy {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][hashtable]$Config)
    $Os = Get-CimInstance Win32_OperatingSystem
    $Ram = $Os.FreePhysicalMemory / 1MB
    $SystemDrive = Get-DurableDriveSnapshot -Path ([Environment]::SystemDirectory)
    $OutputDrive = Get-DurableDriveSnapshot -Path $Config.Output
    if ($Ram -lt [double]$Config.MinimumFreeRamGiB) { throw "Available RAM is below $($Config.MinimumFreeRamGiB) GiB." }
    if ($SystemDrive.FreeGiB -lt [double]$Config.MinimumFreeSystemDriveGiB) {
        throw "System-drive free space is below $($Config.MinimumFreeSystemDriveGiB) GiB."
    }
    if ($OutputDrive.FreeGiB -lt [double]$Config.MinimumFreeOutputDriveGiB) {
        throw "Output-drive free space is below $($Config.MinimumFreeOutputDriveGiB) GiB."
    }
    return [pscustomobject]@{
        FreeRamGiB = $Ram
        SystemDriveRoot = $SystemDrive.Root
        SystemFreeGiB = $SystemDrive.FreeGiB
        OutputDriveRoot = $OutputDrive.Root
        OutputFreeGiB = $OutputDrive.FreeGiB
    }
}

function Test-DurableSolverProcess {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Process)
    $Name = [string]$Process.Name
    $CommandLine = [string]$Process.CommandLine

    # The MCP console entry point is a solver-free stdio host until it creates
    # an actual COMSOL/MPh/Java child.  Treating the host executable as a solver
    # would reject every launcher run while an idle MCP client is connected.
    if ($Name -ieq 'comsol-mcp.exe') { return $false }

    return (
        $Name -match '^comsol.*\.exe$' -or
        $Name -match '^mphserver.*\.exe$' -or
        (($Name -in @('java.exe', 'javaw.exe')) -and $CommandLine -match 'comsol|mphserver')
    )
}

function Test-DurableSolverCollision {
    [CmdletBinding()]
    param([object[]]$ProcessInventory)
    if (-not $PSBoundParameters.ContainsKey('ProcessInventory')) {
        $ProcessInventory = @(Get-CimInstance Win32_Process)
    }
    return @($ProcessInventory | Where-Object { Test-DurableSolverProcess -Process $_ })
}

function Invoke-DurableDriverValidation {
    param([hashtable]$Config)
    $Previous = @{}
    try {
        foreach ($Name in $Config.ValidateEnvironment.Keys) {
            $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
            [Environment]::SetEnvironmentVariable($Name, [string]$Config.ValidateEnvironment[$Name], 'Process')
        }
        & $Config.Python $Config.Driver
        if ($LASTEXITCODE -ne 0) { throw "Driver validation failed with exit code $LASTEXITCODE." }
    }
    finally {
        foreach ($Name in $Previous.Keys) { [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], 'Process') }
    }
}

function Start-DurableDriver {
    param([hashtable]$Config)
    if (-not (Test-Path -LiteralPath $Config.Output -PathType Container)) {
        New-Item -ItemType Directory -Path $Config.Output -Force | Out-Null
    }
    # Complete immutable provenance reads before creating a detached process.
    $LauncherHash = if ($Config.ContainsKey('LauncherPath') -and (Test-Path -LiteralPath $Config.LauncherPath -PathType Leaf)) {
        Get-DurableSha256 -Path $Config.LauncherPath
    }
    else { $null }
    $ModuleHash = if ($Config.ContainsKey('ModulePath') -and (Test-Path -LiteralPath $Config.ModulePath -PathType Leaf)) {
        Get-DurableSha256 -Path $Config.ModulePath
    }
    else { $null }
    $Previous = @{}
    try {
        foreach ($Name in $Config.RunEnvironment.Keys) {
            $Previous[$Name] = [Environment]::GetEnvironmentVariable($Name, 'Process')
            [Environment]::SetEnvironmentVariable($Name, [string]$Config.RunEnvironment[$Name], 'Process')
        }
        $Process = Start-Process -FilePath $Config.Python -ArgumentList @('"' + $Config.Driver + '"') -WorkingDirectory (Split-Path -Parent $Config.Driver) -RedirectStandardOutput $Config.StdoutPath -RedirectStandardError $Config.StderrPath -WindowStyle Hidden -PassThru
    }
    finally {
        foreach ($Name in $Previous.Keys) { [Environment]::SetEnvironmentVariable($Name, $Previous[$Name], 'Process') }
    }
    Start-Sleep -Milliseconds 300
    $Native = Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.Id)" -ErrorAction SilentlyContinue
    if ($null -eq $Native) { throw 'Driver process exited before ownership could be recorded.' }
    $Owner = [ordered]@{
        schema_name = 'durable_launcher.owner.v1'
        job_id = $Config.JobId
        pid = $Process.Id
        creation_date = [string]$Native.CreationDate
        command_line = [string]$Native.CommandLine
        driver_path = $Config.Driver
        started_at_utc = [DateTime]::UtcNow.ToString('o')
        stdout_path = $Config.StdoutPath
        stderr_path = $Config.StderrPath
        launcher_path = if ($Config.ContainsKey('LauncherPath')) { $Config.LauncherPath } else { $null }
        launcher_sha256 = $LauncherHash
        module_path = if ($Config.ContainsKey('ModulePath')) { $Config.ModulePath } else { $null }
        module_sha256 = $ModuleHash
    }
    Write-DurableAtomicJson -Path (Join-Path $Config.Output 'launcher_owner.json') -Value $Owner
    return $Process
}

function Invoke-DurableJobLauncher {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][hashtable]$Config,
        [switch]$Run,
        [switch]$ValidateOnly,
        [switch]$Monitor,
        [switch]$NoTopMost
    )
    $SelectedModeCount = [int]$Run.IsPresent + [int]$ValidateOnly.IsPresent + [int]$Monitor.IsPresent
    if ($SelectedModeCount -gt 1) {
        throw '-Run, -Monitor, and -ValidateOnly are mutually exclusive. Use -Run to start/resume and monitor; use -Monitor only to inspect.'
    }
    foreach ($Name in @('JobName','JobId','Python','Driver','Output','TotalPoints','StatusPath','ResultsPath','ControlDirectory','StdoutPath','StderrPath','MonitorIntervalSeconds','MinimumFreeRamGiB','MinimumFreeSystemDriveGiB','MinimumFreeOutputDriveGiB','ValidateEnvironment','RunEnvironment','RequiredFiles')) {
        if (-not $Config.ContainsKey($Name)) { throw "Launcher configuration is missing '$Name'." }
    }
    foreach ($Path in @($Config.Python, $Config.Driver) + @($Config.RequiredFiles)) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "Required file is missing: $Path" }
    }
    if (-not (Test-Path -LiteralPath $Config.Output -PathType Container)) {
        New-Item -ItemType Directory -Path $Config.Output -Force | Out-Null
    }
    $Active = @(Get-DurableDriverProcesses -DriverPath $Config.Driver)
    if ($Active.Count -gt 1) { throw "Multiple exact driver processes exist: $(($Active.ProcessId) -join ',')." }
    if ($Active.Count -eq 1) {
        if ($ValidateOnly) { Write-Host "LAUNCHER_VALIDATE_PASS active driver collision detected at PID $($Active[0].ProcessId)"; return }
        [void](Show-DurableJobMonitor -Config $Config -NoTopMost:$NoTopMost)
        return
    }
    Invoke-DurableDriverValidation -Config $Config
    if ($ValidateOnly) { Write-Host 'LAUNCHER_VALIDATE_PASS no solver client created'; return }
    if ($Monitor) {
        $MonitorResult = Show-DurableJobMonitor -Config $Config -NoTopMost:$NoTopMost
        if ($MonitorResult -ne 'resume') { return }
        $Run = $true
    }
    if (-not $Run) {
        if (-not (Test-DurableInteractiveInput)) { Write-Host 'Interactive input is unavailable; use -Run, -Monitor, or -ValidateOnly.'; return }
        $Choice = Read-DurableValidatedChoice -Prompt 'Enter RUN to start/resume, MONITOR to inspect, or CANCEL' -Choices @('RUN','MONITOR','CANCEL') -Aliases @{ M='MONITOR'; Q='CANCEL' }
        if ($Choice -eq 'CANCEL') { Write-Host 'Not started.'; return }
        if ($Choice -eq 'MONITOR') {
            $MonitorResult = Show-DurableJobMonitor -Config $Config -NoTopMost:$NoTopMost
            if ($MonitorResult -ne 'resume') { return }
        }
    }
    while ($true) {
        [void](Test-DurableResourcePolicy -Config $Config)
        $Collisions = @(Test-DurableSolverCollision)
        if ($Collisions.Count -gt 0) { throw "A COMSOL solver process already exists: $(($Collisions.ProcessId) -join ',')." }
        [void](Start-DurableDriver -Config $Config)
        $MonitorResult = Show-DurableJobMonitor -Config $Config -NoTopMost:$NoTopMost
        if ($MonitorResult -ne 'resume') { return }
        $Active = @(Get-DurableDriverProcesses -DriverPath $Config.Driver)
        if ($Active.Count -gt 0) { Write-Host 'Resume ignored because the exact driver is still running.' -ForegroundColor Yellow; continue }
    }
}

Export-ModuleMember -Function @(
    'Get-DurableLauncherVersion',
    'Get-DurableTerminalDisposition',
    'Get-DurableDriverProcesses',
    'Get-DurableJobSnapshot',
    'Invoke-DurableJobLauncher',
    'Read-DurableValidatedChoice',
    'Resolve-DurableMonitorCommand',
    'Show-DurableLauncherFailure',
    'Set-DurableConsoleTopMost',
    'Show-DurableJobMonitor',
    'Test-DurableInteractiveInput',
    'Write-DurableAtomicJson',
    'Write-DurablePauseRequest'
)
