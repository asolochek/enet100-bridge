<#
.SYNOPSIS
  Install, update, start, stop or remove enet100-bridge on Windows.

.DESCRIPTION
  Install (the default):
    * creates a Python virtual environment in -InstallDir and installs the bridge into it
    * adds Windows Firewall rules for the portmapper (111), the VXI-11 ports and
      discovery replies (first, since discovery replies are broadcasts)
    * runs `enet100-bridge configure`: finds GPIB-ENET/100 boxes by discovery, lets
      you pick and name them (or enter an IP address), scans their buses, and writes
      C:\ProgramData\enet100-bridge\config.toml. -EnetHost <ip> skips the prompts.
    * registers a scheduled task that runs the bridge as SYSTEM at boot and restarts
      it if it exits, then starts it
  Running it again updates the installed copy and keeps the config.

.EXAMPLE
  # From an elevated PowerShell, in the repository folder:
  powershell -ExecutionPolicy Bypass -File install\install-windows.ps1
  powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -EnetHost 10.0.0.50
  powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Action Status
  powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Action Uninstall
#>
[CmdletBinding()]
param(
    [ValidateSet('Install', 'Uninstall', 'Start', 'Stop', 'Restart', 'Status')]
    [string]$Action = 'Install',
    [string]$EnetHost = '',
    [string]$InstallDir = "$env:ProgramFiles\enet100-bridge",
    [string]$ConfigDir = "$env:ProgramData\enet100-bridge",
    [string]$Python = '',
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'
$TaskName = 'enet100-bridge'
$RuleGroup = 'enet100-bridge'
$RepoDir = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $InstallDir 'venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$ConfigFile = Join-Path $ConfigDir 'config.toml'

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated (Administrator) PowerShell.'
    }
}

function Find-Python {
    if ($Python) { return $Python }
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { $candidates += $p.Trim() }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike '*WindowsApps*') { $candidates += $cmd.Source }
    foreach ($c in $candidates) {
        & $c -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { return $c }
    }
    throw 'Python 3.11 or newer was not found. Install it from python.org (for all users), or pass -Python <path to python.exe>.'
}

function Invoke-Bridge([string[]]$BridgeArgs) {
    & $VenvPy -m enet100_bridge @BridgeArgs
}

function Stop-Bridge {
    if (Test-Path $VenvPy) {
        # Ask for a clean shutdown first: the box must see its sessions closed properly.
        Invoke-Bridge @('stop', '--config', $ConfigFile) 2>$null | Out-Null
        for ($i = 0; $i -lt 20; $i++) {
            $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            if (-not $t -or $t.State -ne 'Running') { break }
            Start-Sleep -Milliseconds 500
        }
    }
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t -and $t.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName }
}

function Write-Config {
    New-Item -ItemType Directory -Force $ConfigDir | Out-Null
    $log = (Join-Path $ConfigDir 'bridge.log') -replace '\\', '/'
    if ($EnetHost) {
        if (Test-Path $ConfigFile) {
            Write-Host "Keeping existing config: $ConfigFile (run 'enet100-bridge configure' to change it)"
            return
        }
        Invoke-Bridge @('configure', '--config', $ConfigFile, '--add', $EnetHost, '--log-file', $log)
        return
    }
    if (Test-Path $ConfigFile) {
        $ans = Read-Host "A config already exists ($ConfigFile). Review or change it now? (y/N)"
        if ($ans -notmatch '^[yY]') { return }
    }
    # Interactive: discovery, pick list, naming, bus scan.
    Invoke-Bridge @('configure', '--config', $ConfigFile, '--log-file', $log)
    if (-not (Test-Path $ConfigFile)) {
        throw "No config was saved. Run the installer again, or pass -EnetHost <ip>."
    }
}

function Test-BlockRules {
    # A Block rule on the bridge's python.exe (e.g. from cancelling the firewall prompt)
    # silently drops discovery replies and VXI-11 connections.
    $blocks = Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
        Where-Object { $_.Program -and ($_.Program -ieq $VenvPy) } |
        Get-NetFirewallRule | Where-Object { $_.Action -eq 'Block' -and $_.Enabled -eq 'True' }
    foreach ($r in $blocks) {
        Write-Warning "Firewall rule '$($r.DisplayName)' blocks $VenvPy. Discovery and clients won't work until it is removed or set to Allow."
    }
}

function Set-Firewall {
    Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    $cfg = if (Test-Path $ConfigFile) { Get-Content $ConfigFile -Raw } else { '' }
    $core = if ($cfg -match '(?m)^\s*core_port\s*=\s*(\d+)') { $Matches[1] } else { '9010' }
    $abort = if ($cfg -match '(?m)^\s*abort_port\s*=\s*(\d+)') { $Matches[1] } else { '9011' }
    $common = @{ Group = $RuleGroup; Direction = 'Inbound'; Action = 'Allow'; Profile = 'Any' }
    New-NetFirewallRule @common -DisplayName 'enet100-bridge portmapper (TCP 111)' -Protocol TCP -LocalPort 111 | Out-Null
    New-NetFirewallRule @common -DisplayName 'enet100-bridge portmapper (UDP 111)' -Protocol UDP -LocalPort 111 | Out-Null
    New-NetFirewallRule @common -DisplayName 'enet100-bridge VXI-11' -Protocol TCP -LocalPort $core, $abort | Out-Null
    # Discovery answers arrive as UDP broadcasts to an ephemeral port.
    New-NetFirewallRule @common -DisplayName 'enet100-bridge discovery replies' -Protocol UDP -RemotePort 44515 -Program $VenvPy | Out-Null
    Write-Verbose "Firewall: allowed TCP/UDP 111, TCP $core,$abort, UDP from :44515"
}

function Register-Task {
    $exe = $VenvPy
    $arg = "-m enet100_bridge run --config `"$ConfigFile`""
    $action = New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $ConfigDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -StartWhenAvailable -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'VXI-11 bridge for NI GPIB-ENET/100 (enet100-bridge)' -Force | Out-Null
    Write-Host "Scheduled task '$TaskName' registered (runs at boot as SYSTEM)"
}

function Test-Port111 {
    $l = Get-NetTCPConnection -LocalPort 111 -State Listen -ErrorAction SilentlyContinue
    if ($l) {
        $procs = $l | ForEach-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName } | Sort-Object -Unique
        if ($procs -notcontains 'python') {
            Write-Warning "Port 111 is already used by: $($procs -join ', '). The bridge needs it for the VXI-11 portmapper."
        }
    }
}

function Show-Status {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host 'Not installed.'; return }
    Write-Host "Task state: $($t.State)"
    if (Test-Path $VenvPy) { Invoke-Bridge @('status', '--config', $ConfigFile) }
    Write-Host "Config: $ConfigFile"
    $log = Join-Path $ConfigDir 'bridge.log'
    if (Test-Path $log) { Write-Host "Log:    $log"; Get-Content $log -Tail 8 }
}

switch ($Action) {
    'Status' { Show-Status; return }
    'Start' { Assert-Admin; Start-ScheduledTask -TaskName $TaskName; Write-Host 'Started.'; return }
    'Stop' { Assert-Admin; Stop-Bridge; Write-Host 'Stopped.'; return }
    'Restart' { Assert-Admin; Stop-Bridge; Start-ScheduledTask -TaskName $TaskName; Write-Host 'Restarted.'; return }
    'Uninstall' {
        Assert-Admin
        Stop-Bridge
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Get-NetFirewallRule -Group $RuleGroup -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
        if ($Purge -and (Test-Path $ConfigDir)) { Remove-Item -Recurse -Force $ConfigDir }
        Write-Host 'Uninstalled.' $(if (-not $Purge) { "Config kept in $ConfigDir (use -Purge to remove it)." })
        return
    }
}

# Install / update
Assert-Admin
$py = Find-Python
Write-Host "Using Python: $py"
Stop-Bridge
New-Item -ItemType Directory -Force $InstallDir | Out-Null
if (-not (Test-Path $VenvPy)) {
    & $py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Creating the virtual environment failed.' }
}
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet --upgrade $RepoDir
if ($LASTEXITCODE -ne 0) { throw 'pip install failed.' }
Write-Host "Installed into $Venv"

Test-Port111
# Firewall first: discovery replies are broadcasts and are dropped without a rule.
Set-Firewall
Test-BlockRules
Write-Config
Set-Firewall          # again, in case configure changed the ports
Register-Task
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Show-Status
Write-Host ''
Write-Host 'Instruments on the box are now TCPIP::<this computer>::gpib0,<address>::INSTR'
