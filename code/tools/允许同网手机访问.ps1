#Requires -RunAsAdministrator
param([string]$BindAddress='', [int]$PrefixLength=24, [int]$PhonePort=18705)
$ErrorActionPreference='Stop'
if (-not $BindAddress) {
    $runtime=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'runtime.json') -Raw | ConvertFrom-Json
    if (-not $runtime.phone_origin) {throw '请先启动同网手机演示。'}
    $origin=[uri]$runtime.phone_origin;$BindAddress=$origin.Host;$PhonePort=$origin.Port
    $addresses=@(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $BindAddress -ErrorAction Stop)
    if ($addresses.Count -ne 1) {throw '演示地址已变化，请重新启动同网手机演示。'}
    $PrefixLength=$addresses[0].PrefixLength
}
if ($BindAddress -notmatch '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)' -or $PrefixLength -lt 16 -or $PrefixLength -gt 30 -or $PhonePort -lt 1024 -or $PhonePort -gt 65535) {throw '只允许明确的本机内网和演示端口。'}
$local=@(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $BindAddress -ErrorAction Stop)
if ($local.Count -ne 1 -or $local[0].PrefixLength -ne $PrefixLength) {throw '地址或前缀与当前网卡不一致。'}
$pythonPath='C:\Python314\python.exe'
$subnet=& $pythonPath -B -c 'import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1]+"/"+sys.argv[2],strict=False))' $BindAddress "$PrefixLength"
if ($LASTEXITCODE -ne 0) {throw '无法核对网段。'}
$ruleName='PZ-Captain-WiFi-'+$BindAddress.Replace('.','-')+'-'+$PhonePort
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {Write-Host '该同网演示规则已存在。';return}
New-NetFirewallRule -Name $ruleName -DisplayName '破局智援参谋：同网队长演示' -Description '仅本机内网地址、同一子网及手机演示端口；不关闭防火墙。' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $BindAddress -LocalPort $PhonePort -RemoteAddress $subnet -Program $pythonPath -Profile Any | Out-Null
Write-Host "已仅允许 $subnet 访问 ${BindAddress}:$PhonePort。请在可信演示Wi-Fi下使用。"
