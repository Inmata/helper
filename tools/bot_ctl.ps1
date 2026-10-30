<#
.SYNOPSIS
  后台启停微信机器人，进程不依赖当前终端。

.DESCRIPTION
  start    清理上次残留的桥接，用 Windows 计划任务启动 tools/bot_daemon.py
  stop     通过 .runtime/bot_stdin.txt 发送 quit，等待正常退出与桥接清理
  restart  先 stop 再 start
  status   查看运行状态、计划任务与最近日志
  command  向运行中的机器人发送一条控制台命令，例如 status、help

.EXAMPLE
  .\tools\bot_ctl.ps1 start
  .\tools\bot_ctl.ps1 status
  .\tools\bot_ctl.ps1 command -Command status
  .\tools\bot_ctl.ps1 stop
#>
param(
    [ValidateSet('start', 'stop', 'restart', 'status', 'command')]
    [string]$Action = 'status',
    [string]$Command,
    [string]$Python = $env:WXBOT_PYTHON,
    [switch]$SkipBridgeCheck
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$Root = Split-Path $PSScriptRoot -Parent
$Runtime = Join-Path $Root '.runtime'
$Control = Join-Path $Runtime 'bot_stdin.txt'
$Stdout = Join-Path $Runtime 'bot_stdout.log'
$DaemonLog = Join-Path $Runtime 'bot_daemon.log'
$BotLog = Join-Path $Root 'logs\bot.log'
$LockFile = Join-Path $Runtime 'bot.lock'
$TaskName = 'wechat-helper-bot'
$Utf8 = [System.Text.UTF8Encoding]::new($false)

function Resolve-Python {
    if ($Python) {
        if (Test-Path -LiteralPath $Python) { return (Resolve-Path -LiteralPath $Python).Path }
        $found = Get-Command $Python -ErrorAction SilentlyContinue
        if ($found) { return $found.Source }
        throw "找不到 Python：$Python"
    }
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) { return $venv }
    $found = Get-Command python -ErrorAction SilentlyContinue
    if (-not $found) { throw '请安装 64 位 Python，或用 -Python 指定解释器路径。' }
    return $found.Source
}

function Test-BotRunning {
    if (-not (Test-Path -LiteralPath $LockFile)) { return $false }
    try {
        $stream = [System.IO.File]::Open($LockFile, 'Open', 'ReadWrite', 'ReadWrite')
        try {
            $stream.Lock(0, 1)
            $stream.Unlock(0, 1)
            return $false
        }
        finally {
            $stream.Close()
        }
    }
    catch {
        return $true
    }
}

function Get-BotProcess {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'main\.py' } |
        Select-Object -First 1
}

function Get-DaemonProcess {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'bot_daemon\.py' } |
        Select-Object -First 1
}

function Stop-OrphanBot {
    if (-not (Test-BotRunning)) { return $false }
    if (Get-DaemonProcess) { return $false }
    Write-Warning '机器人进程还在，但后台守护已退出；结束残留进程并清理桥接...'
    Get-BotProcess | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline -and (Test-BotRunning)) {
        Start-Sleep -Milliseconds 300
    }
    return $true
}

function Show-Status {
    $running = Test-BotRunning
    Write-Host ("运行状态：{0}" -f $(if ($running) { '运行中' } else { '未运行' }))
    $process = Get-BotProcess
    if ($process) {
        Write-Host ("进程：PID={0}  启动于 {1}" -f $process.ProcessId, $process.CreationDate)
    }
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        Write-Host ("计划任务：{0}  上次结果={1}" -f $task.State, $info.LastTaskResult)
    }
    if (Test-Path -LiteralPath $Stdout) {
        Write-Host '--- .runtime/bot_stdout.log 末尾 ---'
        Get-Content -LiteralPath $Stdout -Tail 8 -Encoding utf8
    }
    if (Test-Path -LiteralPath $BotLog) {
        Write-Host '--- logs/bot.log 末尾 ---'
        Get-Content -LiteralPath $BotLog -Tail 5 -Encoding utf8
    }
    if (Test-Path -LiteralPath $DaemonLog) {
        Write-Host '--- .runtime/bot_daemon.log 末尾 ---'
        Get-Content -LiteralPath $DaemonLog -Tail 4 -Encoding utf8
    }
}

function Start-Bot {
    Stop-OrphanBot | Out-Null
    if (Test-BotRunning) {
        Write-Host '机器人已在运行。'
        Show-Status
        return
    }
    $pythonPath = Resolve-Python
    if (-not $SkipBridgeCheck) {
        Write-Host '检查并清理上次残留的桥接...'
        & $pythonPath -X utf8 (Join-Path $Root 'tools\recover_bridge.py')
        if ($LASTEXITCODE -ne 0) {
            throw '桥接检查未通过。确认微信已登录后重试，或加 -SkipBridgeCheck 跳过。'
        }
    }
    if (-not (Test-Path -LiteralPath $Runtime)) {
        New-Item -ItemType Directory -Path $Runtime | Out-Null
    }
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $pythonw = Join-Path (Split-Path $pythonPath -Parent) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $pythonPath }
    $daemon = Join-Path $Root 'tools\bot_daemon.py'
    $arguments = '-X utf8 "{0}" --python "{1}"' -f $daemon, $pythonPath
    try {
        $taskAction = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $Root
        $principal = New-ScheduledTaskPrincipal `
            -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
            -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Principal $principal `
            -Settings $settings -Description '微信机器人后台常驻；用 tools/bot_ctl.ps1 管理' -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
    }
    catch {
        Write-Warning ("计划任务启动失败（{0}），改用独立隐藏进程；它仍会比当前终端活得久。" -f $_.Exception.Message)
        Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden
    }

    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if (Test-BotRunning) {
            Write-Host '机器人已在后台启动，当前终端可以关闭。'
            Show-Status
            return
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warning '启动后 45 秒内未取得实例锁，请查看下面两个日志。'
    if (Test-Path -LiteralPath $DaemonLog) { Get-Content -LiteralPath $DaemonLog -Tail 20 -Encoding utf8 }
    if (Test-Path -LiteralPath $Stdout) { Get-Content -LiteralPath $Stdout -Tail 20 -Encoding utf8 }
    exit 1
}

function Stop-Bot {
    if (-not (Test-BotRunning)) {
        Write-Host '机器人未在运行。'
        return
    }
    if (-not (Get-DaemonProcess)) {
        Stop-OrphanBot | Out-Null
        $pythonPath = Resolve-Python
        & $pythonPath -X utf8 (Join-Path $Root 'tools\recover_bridge.py')
        Write-Host '残留进程已结束，桥接清理结果见上方输出。'
        return
    }
    if (-not (Test-Path -LiteralPath $Runtime)) {
        throw '找不到 .runtime 目录，无法发送退出命令。'
    }
    [System.IO.File]::AppendAllText($Control, "quit`n", $Utf8)
    Write-Host '已发送 quit，等待正常退出与桥接清理...'
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-BotRunning)) {
            Write-Host '机器人已停止；桥接已按退出流程清理。'
            return
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warning '90 秒内未完成退出。请查看 logs\bot.log 与 .runtime\bot_stdout.log；确认进程已消失后再运行 python tools\recover_bridge.py。'
    exit 1
}

function Send-Command {
    if (-not $Command) { throw '请用 -Command 指定要发送的控制台命令，例如 status。' }
    if (-not (Test-BotRunning)) { throw '机器人未在运行，无法发送控制台命令。' }
    [System.IO.File]::AppendAllText($Control, $Command.Trim() + "`n", $Utf8)
    Write-Host ("已发送：{0}" -f $Command.Trim())
}

switch ($Action) {
    'start' { Start-Bot }
    'stop' { Stop-Bot }
    'restart' { Stop-Bot; Start-Bot }
    'status' { Show-Status }
    'command' { Send-Command }
}
