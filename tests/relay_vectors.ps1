# -*- coding: utf-8 -*-
# test_relay.py 가 부르는 도우미. relay.cs 의 MAC 계산을 테스트 벡터와 대조한다.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Add-Type -TypeDefinition (Get-Content (Join-Path $root "client\relay.cs") -Raw -Encoding UTF8) -Language CSharp
$data = Get-Content (Join-Path $root "tests\auth_vectors.json") -Raw -Encoding UTF8 | ConvertFrom-Json

function FromHex([string]$h) {
    $b = New-Object byte[] ($h.Length / 2)
    for ($i = 0; $i -lt $b.Length; $i++) { $b[$i] = [Convert]::ToByte($h.Substring($i * 2, 2), 16) }
    return ,$b
}
function ToHex([byte[]]$b) { return (($b | ForEach-Object { $_.ToString("x2") }) -join "") }

foreach ($v in $data.vectors) {
    $key = [Convert]::FromBase64String($v.key_b64)
    $sn = FromHex $v.server_nonce_hex
    $cn = FromHex $v.client_nonce_hex
    $id = [Text.Encoding]::UTF8.GetBytes($v.client_id)
    $c = ToHex ([FtRelay]::ComputeMac($key, "c", [int]$v.port, $sn, $cn, $id))
    $s = ToHex ([FtRelay]::ComputeMac($key, "s", [int]$v.port, $sn, $cn, $id))
    $ok = ($c -eq $v.client_mac_hex) -and ($s -eq $v.server_mac_hex)
    Write-Output ("VECTOR " + $v.client_id + " " + $ok)
}
