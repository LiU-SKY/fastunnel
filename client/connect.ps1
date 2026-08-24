# -*- coding: utf-8 -*-
<#
  터널 접속 도우미 (접속하는 사람용)

  하는 일
    1. 고정 URL에서 "지금 열려 있는 터널 목록"을 받아온다
    2. cloudflared 가 없으면 자동으로 내려받는다
    3. 열려 있는 터널마다 통로를 만들어 내 PC의 포트에 연결한다
    4. 어떤 주소로 접속하면 되는지 표로 보여 주고, 창을 닫을 때까지 유지한다

  connect.cmd 를 더블클릭하기만 하면 된다. 파이썬이나 별도 설치는 필요 없다.

  주의: 통로는 IPv4(127.0.0.1)에만 열린다. 'localhost' 는 윈도우에서 IPv6(::1)로
  먼저 풀리기 때문에 접속이 실패할 수 있다. 그래서 항상 127.0.0.1 을 안내한다.
#>
[CmdletBinding()]
param(
    # 터널 목록을 알려 주는 고정 URL (server.txt 로도 지정 가능)
    [string]$SourceUrl = "",
    # 특정 원격 포트만 연결 (예: -Only 25565,8080)
    [int[]]$Only = @(),
    # 내 PC에서 쓸 첫 포트를 강제 지정
    [int]$LocalPort = 0,
    # 연결만 하고 바로 종료 (테스트용)
    [switch]$NoWait
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Store = Join-Path $env:LOCALAPPDATA "tunnel-connect"
$CfPath = Join-Path $Store "cloudflared.exe"
$CachePath = Join-Path $Store "last-tunnels.json"
$CF_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

function Say([string]$t, [string]$c = "Gray") { Write-Host $t -ForegroundColor $c }
function Ok([string]$t)   { Say "  [+] $t" "Green" }
function Info([string]$t) { Say "  [i] $t" "Gray" }
function Warn([string]$t) { Say "  [!] $t" "Yellow" }
function Err([string]$t)  { Say "  [x] $t" "Red" }

# ---------------------------------------------------------------- 설정 읽기
function Get-SourceUrl {
    if ($SourceUrl) { return $SourceUrl }
    $f = Join-Path $Root "server.txt"
    if (Test-Path $f) {
        foreach ($line in Get-Content $f -Encoding UTF8) {
            $t = $line.Trim()
            if ($t -and -not $t.StartsWith("#")) { return $t }
        }
    }
    return ""
}

function Clean-Host([string]$value) {
    $h = $value -replace '^(tcp|https?)://', ''
    $h = $h.TrimEnd('/')
    if ($h.Contains(":")) { $h = $h.Split(":")[0] }
    return $h
}

# --------------------------------------------------------- 터널 목록 가져오기
function Get-Tunnels {
    $url = Get-SourceUrl
    if (-not $url) { throw "접속 정보를 알 수 없습니다. server.txt 에 주소 URL 을 넣어 주세요." }

    Info "터널 정보를 확인하는 중 ..."
    $data = $null
    try {
        # raw.githubusercontent 는 캐시가 길어서 매번 다른 쿼리를 붙인다
        $bust = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $sep = if ($url.Contains("?")) { "&" } else { "?" }
        $data = Invoke-RestMethod -Uri ($url + $sep + "t=" + $bust) -TimeoutSec 20 -Headers @{
            "Cache-Control" = "no-cache"; "User-Agent" = "tunnel-connect"
        }
    } catch {
        Warn "정보를 받아오지 못했습니다: $($_.Exception.Message)"
        if (Test-Path $CachePath) {
            Warn "마지막으로 알던 정보로 시도합니다."
            $data = Get-Content $CachePath -Raw -Encoding UTF8 | ConvertFrom-Json
        } else {
            throw "정보를 받아올 수 없고 저장된 것도 없습니다."
        }
    }

    $list = @()
    if ($data.tunnels) {
        foreach ($t in $data.tunnels) {
            if (-not $t.address) { continue }
            if ($t.status -and $t.status -ne "online") { continue }
            $list += [pscustomobject]@{
                RemotePort = [int]$t.port
                Proto      = if ($t.proto) { [string]$t.proto } else { "tcp" }
                Host       = Clean-Host ([string]$t.address)
            }
        }
    } elseif ($data.host -or $data.address) {
        $list += [pscustomobject]@{
            RemotePort = if ($data.port) { [int]$data.port } else { 0 }
            Proto      = "tcp"
            Host       = Clean-Host ([string]($data.host, $data.address -ne $null)[0])
        }
    }

    if ($data.online -eq $false -and $list.Count -eq 0) {
        throw "지금 열려 있는 터널이 없습니다. 서버가 꺼져 있는 것 같습니다."
    }
    if ($list.Count -eq 0) { throw "터널 정보를 해석하지 못했습니다." }

    New-Item -ItemType Directory -Force -Path $Store | Out-Null
    $data | ConvertTo-Json -Depth 6 | Set-Content $CachePath -Encoding UTF8
    # 앞의 콤마가 없으면 원소가 하나일 때 배열이 풀려 .Count 가 사라진다
    return ,$list
}

# ------------------------------------------------------------- cloudflared
function Ensure-Cloudflared {
    $found = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    if (Test-Path $CfPath) { return $CfPath }

    Info "접속 프로그램(cloudflared)을 내려받는 중 ... 처음 한 번만 받습니다"
    New-Item -ItemType Directory -Force -Path $Store | Out-Null
    try {
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $CF_URL -OutFile $CfPath -TimeoutSec 180 -UseBasicParsing
    } catch {
        throw "cloudflared 를 내려받지 못했습니다: $($_.Exception.Message)"
    }
    Ok "접속 프로그램 준비 완료"
    return $CfPath
}

# ------------------------------------------------------------- 포트 다루기
function Get-UsedPorts {
    $used = @{}
    try {
        foreach ($e in [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()) {
            $used[$e.Port] = $true
        }
    } catch {}
    return $used
}

function Wait-Port([int]$port, [int]$seconds = 30) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $c.Connect("127.0.0.1", $port)
            $c.Close()
            return $true
        } catch { Start-Sleep -Milliseconds 700 }
    }
    return $false
}

# 포트가 열린 것과 반대편 서비스가 살아 있는 것은 다르다. cloudflared 는 저쪽이
# 죽어 있어도 이쪽 포트는 열어 준다. 붙어서 잠깐 기다렸을 때 상대가 끊지 않으면
# 살아 있는 것으로 본다.
function Test-Alive([int]$port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $port)
        Start-Sleep -Milliseconds 1500
        $closed = $c.Client.Poll(0, [System.Net.Sockets.SelectMode]::SelectRead) -and ($c.Client.Available -eq 0)
        $c.Close()
        return (-not $closed)
    } catch {
        return $false
    }
}

# ------------------------------------------------------------------- 실행
$procs = @()
try {
    Say ""
    Say "  터널 접속 도우미" "Cyan"
    Say "  ----------------" "DarkGray"

    $tunnels = @(Get-Tunnels)
    if ($Only.Count -gt 0) {
        $tunnels = @($tunnels | Where-Object { $Only -contains $_.RemotePort })
        if ($tunnels.Count -eq 0) { throw "지정한 포트가 열려 있지 않습니다." }
    }
    Ok "열려 있는 터널 $($tunnels.Count)개"

    $cf = Ensure-Cloudflared
    $used = Get-UsedPorts
    $rows = @()

    foreach ($t in $tunnels) {
        # 되도록 원격과 같은 번호를 쓴다. 이미 쓰는 중이면 옆 번호로 비킨다.
        $want = if ($LocalPort -gt 0 -and $rows.Count -eq 0) { $LocalPort } else { $t.RemotePort }
        if ($want -le 0) { $want = 20000 }
        $local = $want
        while ($used.ContainsKey($local)) { $local++ }
        $used[$local] = $true

        Info "포트 $($t.RemotePort) 연결하는 중 ..."
        $p = Start-Process -FilePath $cf -PassThru -WindowStyle Hidden -ArgumentList @(
            "access", "tcp", "--hostname", $t.Host, "--url", "127.0.0.1:$local"
        )
        $procs += $p
        $ready = Wait-Port $local 40
        $alive = if ($ready) { Test-Alive $local } else { $false }
        $rows += [pscustomobject]@{
            "원격 포트" = $t.RemotePort
            "접속 주소" = "127.0.0.1:$local"
            "상태"      = if (-not $ready) { "실패" } elseif ($alive) { "정상" } else { "응답 없음" }
        }
    }

    $rows = @($rows)
    Say ""
    $rows | Format-Table -AutoSize | Out-String | ForEach-Object { $_.TrimEnd() } | Write-Host
    Say ""

    $good = @($rows | Where-Object { $_."상태" -eq "정상" }).Count
    if ($good -eq $rows.Count) {
        Ok "모두 연결됐습니다. 위 '접속 주소' 를 그대로 넣어 주세요."
    } elseif ($good -gt 0) {
        Warn "일부만 연결됐습니다. '응답 없음' 은 저쪽 프로그램이 아직 안 켜졌을 수 있습니다."
    } else {
        Warn "연결하지 못했습니다. 서버가 꺼져 있거나 주소가 막 바뀌었을 수 있습니다."
        Info "  20초쯤 뒤에 이 창을 다시 실행해 보세요."
    }

    Say ""
    Info "'localhost' 가 아니라 반드시 127.0.0.1 로 넣으세요."
    Info "  localhost 는 IPv6 로 먼저 시도해서 실패할 수 있습니다."
    Warn "이 창을 닫으면 접속이 끊깁니다. 쓰는 동안 켜 두세요."
    Warn "접속하려는 프로그램은 이 창을 띄운 것과 같은 PC 에서 실행해야 합니다."
    Say ""

    if ($NoWait) { exit 0 }
    while ($true) {
        Start-Sleep -Seconds 3
        if (@($procs | Where-Object { -not $_.HasExited }).Count -eq 0) {
            Warn "접속이 모두 끊어졌습니다."
            break
        }
    }
    Read-Host "  엔터를 누르면 닫힙니다"
} catch {
    Err $_.Exception.Message
    Say ""
    if (-not $NoWait) { Read-Host "  엔터를 누르면 닫힙니다" }
    exit 1
} finally {
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) {
            try { $p.Kill() } catch {}
        }
    }
}
