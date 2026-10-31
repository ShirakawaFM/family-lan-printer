#requires -Version 5.1
# 家庭打印 · 家里 Windows 电脑 frpc 一键配置脚本
# 运行：powershell -ExecutionPolicy Bypass -File ".\03-家里电脑-运行这个.ps1"

$ErrorActionPreference = "Stop"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "== $Text ==" -ForegroundColor Cyan
}

function Read-WithDefault {
    param([string]$Prompt, [string]$Default)
    $value = Read-Host "$Prompt（默认 $Default）"
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

function Assert-Port {
    param([string]$Value, [string]$Name)
    $number = 0
    if (-not [int]::TryParse($Value, [ref]$number) -or $number -lt 1 -or $number -gt 65535) {
        throw "$Name 不是有效端口（1-65535）：$Value"
    }
    return $number
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ToolRoot = [IO.Path]::GetFullPath((Join-Path $ScriptDir "..\.."))
if (-not (Test-Path (Join-Path $ToolRoot "print_server.py"))) {
    $candidate = [IO.Path]::GetFullPath((Join-Path $ScriptDir ".."))
    if (Test-Path (Join-Path $candidate "print_server.py")) {
        $ToolRoot = $candidate
    } else {
        throw "没有找到 print_server.py。请把 deploy-public 文件夹放到 print_server.py 所在目录后再运行。"
    }
}

Write-Host "家庭打印 · 公网部署（家里电脑部分）" -ForegroundColor Green
Write-Host "打印工具目录：$ToolRoot"
Write-Host "先把 01-先填-部署信息.txt 里的内容准备好。"

Write-Step "填写连接信息"
$ServerIp = Read-Host "云服务器公网 IP（请填写）"
if ([string]::IsNullOrWhiteSpace($ServerIp)) { throw "云服务器公网 IP 不能为空" }
$ServerIp = $ServerIp.Trim()

$BindPort = Assert-Port (Read-WithDefault "frp 控制端口" "7000") "frp 控制端口"
$Token = Read-Host "frp token（请粘贴服务器脚本显示的长串）"
if ([string]::IsNullOrWhiteSpace($Token)) { throw "frp token 不能为空" }
$Token = $Token.Trim()
if ($Token -match '[\s"]') { throw "frp token 不能包含空格或双引号" }

$RemotePort = Assert-Port (Read-WithDefault "外网访问端口" "6000") "外网访问端口"
$LocalPort = Assert-Port (Read-WithDefault "本机打印服务端口" "5000") "本机打印服务端口"
if ($BindPort -eq $RemotePort) { throw "控制端口和外网访问端口不能相同" }

$FrpDir = Join-Path $ToolRoot "frp"
$FrpcExe = Join-Path $FrpDir "frpc.exe"
$FrpcConfig = Join-Path $FrpDir "frpc.toml"
New-Item -ItemType Directory -Force -Path $FrpDir | Out-Null

if (-not (Test-Path $FrpcExe)) {
    Write-Step "下载 frpc"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $FrpVersion = "0.61.0"
    $Arch = "amd64"
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { $Arch = "arm64" }
    if ($env:PROCESSOR_ARCHITECTURE -eq "x86") { $Arch = "386" }
    $ZipUrl = "https://github.com/fatedier/frp/releases/download/v$FrpVersion/frp_${FrpVersion}_windows_$Arch.zip"
    $ZipPath = Join-Path $env:TEMP "frp_${FrpVersion}_windows_$Arch.zip"
    $ExtractDir = Join-Path $env:TEMP ("frp_extract_" + [guid]::NewGuid().ToString("N"))
    try {
        Write-Host "下载：$ZipUrl"
        Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipPath -UseBasicParsing
        Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force
        $found = Get-ChildItem -Path $ExtractDir -Recurse -Filter "frpc.exe" | Select-Object -First 1
        if (-not $found) { throw "压缩包里没有 frpc.exe" }
        Copy-Item -LiteralPath $found.FullName -Destination $FrpcExe -Force
        Write-Host "frpc.exe 已放到：$FrpcDir" -ForegroundColor Green
    } catch {
        throw "下载 frpc 失败：$($_.Exception.Message)。请手动下载 $ZipUrl，解压后把 frpc.exe 放到 $FrpDir 再重新运行。"
    } finally {
        Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $ExtractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "已存在 frpc.exe，跳过下载：$FrpcExe"
}

Write-Step "写入 frpc 配置"
$ConfigText = @"
serverAddr = "$ServerIp"
serverPort = $BindPort
auth.method = "token"
auth.token = "$Token"

log.to = "frpc.log"
log.level = "info"
log.maxDays = 3

[[proxies]]
name = "family-print"
type = "tcp"
localIP = "127.0.0.1"
localPort = $LocalPort
remotePort = $RemotePort
"@
[IO.File]::WriteAllText($FrpcConfig, $ConfigText, (New-Object System.Text.UTF8Encoding($false)))
Write-Host "配置已写入：$FrpcConfig" -ForegroundColor Green

Write-Step "查找 Python"
$Python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
$PythonW = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw "没有找到 python.exe，请先安装 Python 并勾选 Add Python to PATH" }
if (-not $PythonW) { $PythonW = $Python }
& $Python -c "import flask, win32print, PIL, qrcode, pypdfium2" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Python 依赖可能没装全。请先在打印工具目录运行：python -m pip install -r requirements.txt"
}
Write-Host "Python：$Python"

Write-Step "生成启动/停止脚本"
$StartupBat = Join-Path $ToolRoot "启动家庭打印.bat"
$StartBat = @"
@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" /min "$PythonW" "print_server.py"
timeout /t 4 >nul
start "" /min "%~dp0frp\frpc.exe" -c "%~dp0frp\frpc.toml"
"@
[IO.File]::WriteAllText($StartupBat, $StartBat, (New-Object System.Text.UTF8Encoding($false)))

$StopBat = Join-Path $ToolRoot "停止家庭打印.bat"
$StopText = @'
@echo off
taskkill /IM frpc.exe /F >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*print_server.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
'@
[IO.File]::WriteAllText($StopBat, $StopText, (New-Object System.Text.UTF8Encoding($false)))

$StartupFolder = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupFolder "家庭打印公网版.lnk"
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $StartupBat
$Shortcut.WorkingDirectory = $ToolRoot
$Shortcut.WindowStyle = 7
$Shortcut.Description = "家庭打印 + 公网 frpc"
$Shortcut.Save()
Write-Host "已创建开机启动快捷方式：$ShortcutPath" -ForegroundColor Green

Write-Step "立即启动并检查"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "`"$StartupBat`"" -WindowStyle Hidden
Start-Sleep -Seconds 8
try {
    Invoke-WebRequest -Uri "http://127.0.0.1:$LocalPort/" -UseBasicParsing -TimeoutSec 10 | Out-Null
    Write-Host "✅ 本机打印服务正常：http://127.0.0.1:$LocalPort" -ForegroundColor Green
} catch {
    Write-Warning "本机打印服务暂时没起来。请先手动运行 print_server.py 看错误，或检查端口 $LocalPort。"
}

Start-Sleep -Seconds 2
if (Get-Process -Name "frpc" -ErrorAction SilentlyContinue) {
    Write-Host "✅ frpc 已启动" -ForegroundColor Green
} else {
    Write-Warning "frpc 没有保持运行。请查看 $FrpDir\frpc.log，重点检查 token、服务器 IP 和端口。"
}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "✅ 家里电脑配置完成" -ForegroundColor Green
Write-Host "本机地址：http://127.0.0.1:$LocalPort"
Write-Host "公网地址：http://${ServerIp}:$RemotePort"
Write-Host "frpc 日志：$FrpDir\frpc.log"
Write-Host "下次登录会自动启动：$ShortcutPath"
Write-Host "停止：双击 $StopBat"
Write-Host "============================================================" -ForegroundColor Cyan
