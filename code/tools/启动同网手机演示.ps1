param([string]$BindAddress='', [int]$PrefixLength=24, [int]$Port=18703, [int]$PhonePort=18705)
$ErrorActionPreference='Stop'
$demoRoot=$PSScriptRoot
$pythonPath='C:\Python314\python.exe'
if (-not (Test-Path -LiteralPath (Join-Path $demoRoot 'run_fire_demo.py'))) { throw '请从交付目录运行本脚本。' }
if (Test-Path -LiteralPath (Join-Path $demoRoot 'process.lock')) { throw '演示已有运行标记，请先检查 runtime.json，并正常停止已有演示。' }
if (-not $BindAddress) {
    $candidates=@(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } | ForEach-Object {
        foreach ($address in $_.IPv4Address) {
            if ($address.IPAddress -match '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') {
                [pscustomobject]@{Address=$address.IPAddress;Prefix=$address.PrefixLength;Interface=$_.InterfaceAlias}
            }
        }
    })
    if ($candidates.Count -ne 1) { throw '存在多个或没有可用内网地址，请用 -BindAddress 和 -PrefixLength 指定连接手机Wi-Fi的网卡。' }
    $BindAddress=$candidates[0].Address;$PrefixLength=$candidates[0].Prefix
}
$matched=@(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $BindAddress -ErrorAction SilentlyContinue)
if ($matched.Count -ne 1 -or $matched[0].PrefixLength -ne $PrefixLength) {throw '所选IP及前缀必须属于本机当前网卡。'}
if ($BindAddress -notmatch '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' -or $PrefixLength -lt 16 -or $PrefixLength -gt 30 -or $PhonePort -lt 1024 -or $PhonePort -gt 65535 -or $Port -eq $PhonePort) {throw '内网地址、前缀或端口不正确。'}
$stopPath=Join-Path $demoRoot 'stop'
if (Test-Path -LiteralPath $stopPath) {Remove-Item -LiteralPath $stopPath}
$stamp=Get-Date -Format 'yyyyMMdd_HHmmss'
$started=Start-Process -FilePath $pythonPath -ArgumentList @('-B',('"'+(Join-Path $demoRoot 'run_fire_demo.py')+'"'),'--port',"$Port",'--wifi-address',$BindAddress,'--wifi-prefix',"$PrefixLength",'--phone-port',"$PhonePort") -WorkingDirectory $demoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $demoRoot ($stamp+'-wifi-stdout.log')) -RedirectStandardError (Join-Path $demoRoot ($stamp+'-wifi-stderr.log'))
$ready=$false
for ($attempt=0;$attempt -lt 30;$attempt++) {
    Start-Sleep -Milliseconds 400
    if ($started.HasExited) {throw ('启动未完成，请查看 '+$stamp+'-wifi-stderr.log')}
    $runtimePath=Join-Path $demoRoot 'runtime.json'
    if (Test-Path -LiteralPath $runtimePath) {
        $current=Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
        if ($current.pid -eq $started.Id -and $current.phone_origin -eq "http://${BindAddress}:$PhonePort") {$ready=$true;break}
    }
}
if (-not $ready) {throw '启动尚未完成，请核对本目录日志，勿重复启动。'}
Write-Host "电脑沙盘：http://127.0.0.1:$Port/sand"
Write-Host "电脑显示App安装二维码：http://127.0.0.1:$Port/wifi"
Write-Host "手机安卓安装包：http://${BindAddress}:$PhonePort/app/download"
Write-Host "App内电脑服务地址：http://${BindAddress}:$PhonePort"
Write-Host '电脑与手机连接同一个可信Wi-Fi；电脑保持运行。手机如打不开，请以管理员身份运行“允许同网手机访问.ps1”。'
