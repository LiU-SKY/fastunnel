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

  접속 키 인증 (서버가 run --auth 로 실행 중일 때)
    - 이 폴더의 client.key 로 서버의 인증 관문과 핸드셰이크를 한다(relay.cs).
    - cloudflared access 는 내부 포트에 띄우고, 내 PC 의 포트에서는 중계기가 받는다.
      중계기는 127.0.0.1 과 ::1 둘 다에서 받으므로 이때는 'localhost' 로 넣어도 된다.
    - 서버가 exact_ports 를 게시하면(또는 -ExactPorts) 포트를 옆 번호로 비키지 않는다.
      주소에 포트가 박힌 웹 앱은 번호가 바뀌면 동작하지 않기 때문이다.
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
    [switch]$NoWait,
    # 원격과 같은 포트만 쓴다(이미 쓰는 중이면 비키지 않고 멈춘다)
    [switch]$ExactPorts,
    # 접속 키 파일 (기본: 이 폴더의 client.key)
    [string]$KeyFile = ""
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Store = Join-Path $env:LOCALAPPDATA "tunnel-connect"
$CfPath = Join-Path $Store "cloudflared.exe"
$CachePath = Join-Path $Store "last-tunnels.json"
# 32비트 윈도우도 있으므로 받아올 파일을 골라 준다.
# (ARM 윈도우는 x64 에뮬레이션으로 amd64 가 그대로 돈다 - 전용 빌드가 없다)
$CfArch = if ([Environment]::Is64BitOperatingSystem) { "amd64" } else { "386" }
$CF_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-$CfArch.exe"

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
                Auth       = [bool]$t.auth
            }
        }
        $script:ServerExactPorts = [bool]$data.exact_ports
    } elseif ($data.host -or $data.address) {
        $list += [pscustomobject]@{
            RemotePort = if ($data.port) { [int]$data.port } else { 0 }
            Proto      = "tcp"
            Host       = Clean-Host ([string]($data.host, $data.address -ne $null)[0])
            Auth       = [bool]$data.auth
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

# ------------------------------------------------------------- 접속 키
function Get-ClientKey {
    $path = if ($KeyFile) { $KeyFile } else { Join-Path $Root "client.key" }
    if (-not (Test-Path $path)) {
        throw "이 서버는 접속 키가 필요합니다. 받은 폴더에 client.key 가 있는지 확인하세요."
    }
    try {
        $k = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
        $bytes = [Convert]::FromBase64String([string]$k.key)
    } catch {
        throw "client.key 를 읽지 못했습니다: $($_.Exception.Message)"
    }
    if (-not $k.id -or $bytes.Length -ne 32) { throw "client.key 형식이 올바르지 않습니다." }
    return [pscustomobject]@{ Id = [string]$k.id; Key = $bytes }
}

function Load-Relay {
    if ("FtRelay" -as [type]) { return }
    $src = Join-Path $Root "relay.cs"
    if (-not (Test-Path $src)) { throw "relay.cs 가 없습니다. 받은 폴더를 통째로 다시 풀어 주세요." }
    Add-Type -TypeDefinition (Get-Content $src -Raw -Encoding UTF8) -Language CSharp
}

# 중계기가 핸드셰이크만 해 본 결과를 표의 상태로 바꾼다.
function Relay-State([string]$probe) {
    if ($probe -eq "ok")        { return "정상" }
    if ($probe -eq "denied")    { return "키 거부" }
    if ($probe -eq "badserver") { return "서버 확인 실패" }
    return "응답 없음"
}

# ------------------------------------------------------------- cloudflared
function Ensure-Cloudflared {
    $found = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    if (Test-Path $CfPath) { return $CfPath }

    Info "접속 프로그램(cloudflared)을 내려받는 중 ... 처음 한 번만 받습니다 (약 50MB)"
    New-Item -ItemType Directory -Force -Path $Store | Out-Null
    $tmp = $CfPath + ".part"
    try {
        $ProgressPreference = "SilentlyContinue"
        # 옛 윈도우는 TLS 1.2 가 기본이 아니라 깃허브 연결이 막힌다
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
        Invoke-WebRequest -Uri $CF_URL -OutFile $tmp -TimeoutSec 300 -UseBasicParsing
    } catch {
        Remove-Item $tmp -ErrorAction SilentlyContinue
        throw "cloudflared 를 내려받지 못했습니다: $($_.Exception.Message)"
    }
    # 받다 만 파일을 다음 실행에서 정상으로 착각하지 않도록 다 받은 뒤에 이름을 바꾼다
    if ((Get-Item $tmp).Length -lt 1MB) {
        Remove-Item $tmp -ErrorAction SilentlyContinue
        throw "내려받은 파일이 온전하지 않습니다. 잠시 뒤 다시 시도해 주세요."
    }
    Move-Item $tmp $CfPath -Force
    Ok "접속 프로그램 준비 완료 ($CfArch)"
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
$relays = @()
$script:ServerExactPorts = $false
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

    $exact = $ExactPorts -or $script:ServerExactPorts
    $needAuth = @($tunnels | Where-Object { $_.Auth }).Count -gt 0
    $cred = $null
    if ($needAuth) {
        $cred = Get-ClientKey
        Load-Relay
        Ok "접속 키 확인 ($($cred.Id))"
    }

    $cf = Ensure-Cloudflared
    $used = Get-UsedPorts
    $rows = @()
    # 인증 터널의 cloudflared 는 사람이 쓰지 않는 내부 포트에 띄운다(중계기만 그 포트에 붙는다).
    $inner = 41000

    foreach ($t in $tunnels) {
        # 되도록 원격과 같은 번호를 쓴다. 이미 쓰는 중이면 옆 번호로 비킨다(고정 모드면 멈춘다).
        $want = if ($LocalPort -gt 0 -and $rows.Count -eq 0) { $LocalPort } else { $t.RemotePort }
        if ($want -le 0) { $want = 20000 }
        $local = $want
        if ($exact -and $used.ContainsKey($local)) {
            throw "내 PC 의 포트 $local 을 다른 프로그램이 쓰고 있습니다. 그 프로그램을 끄고 다시 실행하세요. (이 서버는 같은 번호로만 접속할 수 있습니다)"
        }
        while ($used.ContainsKey($local)) { $local++ }
        $used[$local] = $true

        Info "포트 $($t.RemotePort) 연결하는 중 ..."
        $cfPort = $local
        if ($t.Auth) {
            while ($used.ContainsKey($inner)) { $inner++ }
            $cfPort = $inner
            $used[$inner] = $true
        }
        $p = Start-Process -FilePath $cf -PassThru -WindowStyle Hidden -ArgumentList @(
            "access", "tcp", "--hostname", $t.Host, "--url", "127.0.0.1:$cfPort"
        )
        $procs += $p
        $ready = Wait-Port $cfPort 40
        if ($t.Auth) {
            $state = "실패"
            if ($ready) {
                $relay = [FtRelay]::new($local, "127.0.0.1", $cfPort, $t.RemotePort, $cred.Id, $cred.Key)
                $relay.Start()
                $relays += $relay
                $state = Relay-State ($relay.Probe(20000))
            }
        } else {
            $alive = if ($ready) { Test-Alive $local } else { $false }
            $state = if (-not $ready) { "실패" } elseif ($alive) { "정상" } else { "응답 없음" }
        }
        $rows += [pscustomobject]@{
            "원격 포트" = $t.RemotePort
            "접속 주소" = "127.0.0.1:$local"
            "상태"      = $state
        }
    }

    $rows = @($rows)
    Say ""
    $rows | Format-Table -AutoSize | Out-String | ForEach-Object { $_.TrimEnd() } | Write-Host
    Say ""

    $good = @($rows | Where-Object { $_."상태" -eq "정상" }).Count
    if ($good -eq $rows.Count) {
        Ok "모두 연결됐습니다. 위 '접속 주소' 를 그대로 넣어 주세요."
    } elseif (@($rows | Where-Object { $_."상태" -eq "키 거부" }).Count -gt 0) {
        Err "서버가 이 접속 키를 거부했습니다. 키가 회수됐거나 다른 서버의 키입니다. 보내 준 사람에게 문의하세요."
    } elseif ($good -gt 0) {
        Warn "일부만 연결됐습니다. '응답 없음' 은 저쪽 프로그램이 아직 안 켜졌을 수 있습니다."
    } else {
        Warn "연결하지 못했습니다. 서버가 꺼져 있거나 주소가 막 바뀌었을 수 있습니다."
        Info "  20초쯤 뒤에 이 창을 다시 실행해 보세요."
    }

    Say ""
    if ($needAuth) {
        Info "'127.0.0.1' 과 'localhost' 둘 다 됩니다."
    } else {
        Info "'localhost' 가 아니라 반드시 127.0.0.1 로 넣으세요."
        Info "  localhost 는 IPv6 로 먼저 시도해서 실패할 수 있습니다."
    }
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
    foreach ($r in $relays) {
        try { $r.Stop() } catch {}
    }
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) {
            try { $p.Kill() } catch {}
        }
    }
}
