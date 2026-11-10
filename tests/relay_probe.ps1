# -*- coding: utf-8 -*-
# test_relay.py 가 부르는 도우미. client/relay.cs 를 윈도우 파워셸 5.1 로 컴파일해서
# 중계기를 띄우고, 핸드셰이크 결과를 한 줄로 알린 뒤 지정한 시간 동안 살아 있는다.
param(
    [int]$LocalPort,
    [int]$GatePort,
    [int]$RemotePort,
    [string]$KeyFile,
    [int]$Seconds = 20
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Add-Type -TypeDefinition (Get-Content (Join-Path $root "client\relay.cs") -Raw -Encoding UTF8) -Language CSharp
$k = Get-Content $KeyFile -Raw -Encoding UTF8 | ConvertFrom-Json
$key = [Convert]::FromBase64String([string]$k.key)
$relay = [FtRelay]::new($LocalPort, "127.0.0.1", $GatePort, $RemotePort, [string]$k.id, $key)
$relay.Start()
Write-Output ("PROBE=" + $relay.Probe(5000))
Write-Output ("IPV6=" + $relay.ListensOnIPv6)
[Console]::Out.Flush()
Start-Sleep -Seconds $Seconds
Write-Output ("STATS=" + $relay.Accepted + "/" + $relay.Denied)
$relay.Stop()
