# -*- coding: utf-8 -*-
<#
  마인크래프트 원클릭 접속 클라이언트 (플레이어용)

  하는 일
    1. 고정 URL에서 "지금 열려 있는 터널 주소"를 받아온다
    2. cloudflared 가 없으면 자동으로 내려받는다
    3. cloudflared access tcp 를 띄워 로컬 포트로 연결한다
    4. 마인크래프트에서 넣을 주소를 알려 주고, 창을 닫을 때까지 유지한다

  플레이어는 이 파일을 더블클릭(mc-connect.cmd)하기만 하면 된다.
  파워셸/파이썬 설치 불필요 - 윈도우 기본 파워셸로 동작한다.
#>
[CmdletBinding()]
param(
    # 주소를 알려 주는 고정 URL (server.txt 로도 지정 가능)
    [string]$SourceUrl = "",
    # 터널 주소를 직접 지정 (URL 없이 쓸 때)
    [string]$Hostname = "",
    # 마인크래프트에서 접속할 로컬 포트
    [int]$LocalPort = 0,
    # 실행 후 바로 종료 (테스트용)
    [switch]$NoWait
)

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Home2 = Join-Path $env:LOCALAPPDATA "mc-connect"
$CfPath = Join-Path $Home2 "cloudflared.exe"
$CachePath = Join-Path $Home2 "last-address.json"
$CF_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

function Say([string]$text, [string]$color = "Gray") { Write-Host $text -ForegroundColor $color }
function Ok([string]$t)   { Say "  [+] $t" "Green" }
function Info([string]$t) { Say "  [i] $t" "Gray" }
function Warn([string]$t) { Say "  [!] $t" "Yellow" }
function Err([string]$t)  { Say "  [x] $t" "Red" }

function Show-Banner {
    Say ""
    Say "  마인크래프트 접속 도우미" "Cyan"
    Say "  ------------------------" "DarkGray"
}

# ---------------------------------------------------------------- 설정 읽기
function Get-SourceUrl {
    if ($SourceUrl) { return $SourceUrl }
    # 같은 폴더의 server.txt 첫 줄. 배포할 때 이 파일만 바꿔 주면 된다.
    $f = Join-Path $Root "server.txt"
    if (Test-Path $f) {
        foreach ($line in Get-Content $f -Encoding UTF8) {
            $t = $line.Trim()
            if ($t -and -not $t.StartsWith("#")) { return $t }
        }
    }
    return ""
}

# ------------------------------------------------------------ 주소 가져오기
function Get-Address {
    if ($Hostname) {
        return @{ host = $Hostname; port = 25565; source = "직접 지정" }
    }
    $url = Get-SourceUrl
    if (-not $url) {
        throw "접속 주소를 알 수 없습니다. server.txt 에 주소 URL 을 넣어 주세요."
    }

    Info "서버 주소 확인 중 ..."
    try {
        # 캐시를 타지 않도록 쿼리를 붙인다 (raw.githubusercontent 는 캐시가 길다)
        $bust = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $sep = if ($url.Contains("?")) { "&" } else { "?" }
        $fetchUrl = $url + $sep + "t=" + $bust
        $data = Invoke-RestMethod -Uri $fetchUrl -TimeoutSec 20 -Headers @{
            "Cache-Control" = "no-cache"
            "User-Agent"    = "mc-connect"
        }
    } catch {
        Warn "주소를 받아오지 못했습니다: $($_.Exception.Message)"
        if (Test-Path $CachePath) {
            Warn "마지막으로 알던 주소로 시도합니다."
            $data = Get-Content $CachePath -Raw -Encoding UTF8 | ConvertFrom-Json
        } else {
            throw "주소를 받아올 수 없고 저장된 주소도 없습니다."
        }
    }

    # 받아들이는 형태
    #   {"host": "...", "port": 25565}
    #   {"tunnels": [{"address": "...", "port": 25565}]}   <- fastunnel 상태 파일
    $h = $null; $p = 25565
    if ($data.host) { $h = [string]$data.host; if ($data.port) { $p = [int]$data.port } }
    elseif ($data.address) { $h = [string]$data.address; if ($data.port) { $p = [int]$data.port } }
    elseif ($data.tunnels) {
        foreach ($t in $data.tunnels) {
            if ($t.address) { $h = [string]$t.address; if ($t.port) { $p = [int]$t.port }; break }
        }
    }
    if (-not $h) { throw "응답에서 주소를 찾지 못했습니다." }

    $h = $h -replace '^(tcp|https?)://', ''
    $h = $h.TrimEnd('/')
    if ($h.Contains(":")) { $h = $h.Split(":")[0] }

    New-Item -ItemType Directory -Force -Path $Home2 | Out-Null
    @{ host = $h; port = $p } | ConvertTo-Json | Set-Content $CachePath -Encoding UTF8
    return @{ host = $h; port = $p; source = $url }
}

# ------------------------------------------------------------- cloudflared
function Ensure-Cloudflared {
    # 이미 설치돼 있으면 그걸 쓴다
    $found = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    if (Test-Path $CfPath) { return $CfPath }

    Info "접속 프로그램(cloudflared)을 내려받는 중 ... 처음 한 번만 받습니다"
    New-Item -ItemType Directory -Force -Path $Home2 | Out-Null
    try {
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $CF_URL -OutFile $CfPath -TimeoutSec 180 -UseBasicParsing
    } catch {
        throw "cloudflared 를 내려받지 못했습니다: $($_.Exception.Message)"
    }
    if (-not (Test-Path $CfPath)) { throw "cloudflared 내려받기에 실패했습니다." }
    Ok "접속 프로그램 준비 완료"
    return $CfPath
}

# ------------------------------------------------------------- 로컬 포트
function Get-FreePort([int]$preferred) {
    foreach ($p in @($preferred) + (($preferred + 1)..($preferred + 20))) {
        $used = $false
        try {
            $l = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
            foreach ($e in $l) { if ($e.Port -eq $p) { $used = $true; break } }
        } catch {}
        if (-not $used) { return $p }
    }
    return $preferred
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

# --------------------------------------------------- 서버가 진짜 살아있는지
# 포트가 열린 것과 서버가 응답하는 것은 다른 문제다. cloudflared 는 서버가 죽어 있어도
# 로컬 포트를 열어 준다. 그래서 마인크래프트가 쓰는 '서버 목록 핑'을 직접 보내 확인한다.
function Write-VarInt([int]$value) {
    $out = New-Object System.Collections.Generic.List[byte]
    $v = [uint32]$value
    while ($true) {
        $b = [byte]($v -band 0x7F)
        $v = $v -shr 7
        if ($v -ne 0) { $out.Add([byte]($b -bor 0x80)) } else { $out.Add($b); break }
    }
    # 앞의 콤마가 없으면 파워셸이 배열을 풀어헤쳐 Object[] 로 돌려준다.
    # 그러면 List[byte].AddRange 와 Stream.Write 가 받지 못한다.
    return ,$out.ToArray()
}

function Read-VarInt($stream) {
    $num = 0
    for ($i = 0; $i -lt 5; $i++) {
        $b = $stream.ReadByte()
        if ($b -lt 0) { throw "연결이 끊겼습니다" }
        $num = $num -bor (($b -band 0x7F) -shl (7 * $i))
        if (($b -band 0x80) -eq 0) { return $num }
    }
    throw "잘못된 응답"
}

function Test-Minecraft([int]$port, [int]$timeoutSec = 12) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.ReceiveTimeout = $timeoutSec * 1000
        $client.SendTimeout = $timeoutSec * 1000
        $client.Connect("127.0.0.1", $port)
        $s = $client.GetStream()

        $addrBytes = [Text.Encoding]::UTF8.GetBytes("localhost")
        $body = New-Object System.Collections.Generic.List[byte]
        $body.Add(0x00)                                    # 패킷 ID: 핸드셰이크
        $body.AddRange((Write-VarInt 770))                 # 프로토콜 버전
        $body.AddRange((Write-VarInt $addrBytes.Length))
        $body.AddRange($addrBytes)
        $pb = [BitConverter]::GetBytes([uint16]$port)
        $body.AddRange([byte[]]@($pb[1], $pb[0]))   # 빅엔디언 2바이트
        $body.AddRange((Write-VarInt 1))                   # 다음 상태: 조회
        # [byte[]] 로 명시적으로 감싼다. 파워셸에서 byte[] + byte[] 는 Object[] 가 되어
        # Stream.Write 가 받지 못한다.
        $packet = [byte[]]((Write-VarInt $body.Count) + $body.ToArray())
        $s.Write($packet, 0, $packet.Length)

        $req = [byte[]]((Write-VarInt 1) + @([byte]0x00))  # 상태 요청
        $s.Write($req, 0, $req.Length)
        $s.Flush()

        [void](Read-VarInt $s)                             # 전체 길이
        if ((Read-VarInt $s) -ne 0) { throw "예상치 못한 응답" }
        $len = Read-VarInt $s
        $buf = New-Object byte[] $len
        $got = 0
        while ($got -lt $len) {
            $n = $s.Read($buf, $got, $len - $got)
            if ($n -le 0) { break }
            $got += $n
        }
        $client.Close()
        $json = [Text.Encoding]::UTF8.GetString($buf, 0, $got) | ConvertFrom-Json
        return @{
            version = $json.version.name
            online  = $json.players.online
            max     = $json.players.max
        }
    } catch {
        return $null
    }
}

# ------------------------------------------------------------------- 실행
try {
    Show-Banner
    $addr = Get-Address
    Ok "서버 주소: $($addr.host)"

    $cf = Ensure-Cloudflared
    $port = if ($LocalPort -gt 0) { $LocalPort } else { Get-FreePort $addr.port }
    if ($port -ne $addr.port) { Info "$($addr.port) 번이 사용 중이라 $port 번을 씁니다" }

    Info "연결하는 중 ..."
    # 반드시 127.0.0.1 로 명시한다. 'localhost' 로 두면 cloudflared 는 IPv4 에만 붙는데
    # 윈도우에서 localhost 는 ::1(IPv6) 로 먼저 풀려서, 게임이 IPv6 를 먼저 시도하면
    # 연결이 실패한다. 실제로 이것 때문에 "포트는 열렸는데 접속이 안 되는" 신고가 있었다.
    $args = @("access", "tcp", "--hostname", $addr.host, "--url", "127.0.0.1:$port")
    $proc = Start-Process -FilePath $cf -ArgumentList $args -PassThru -WindowStyle Hidden

    if (-not (Wait-Port $port 40)) {
        Err "연결에 실패했습니다."
        Info "서버가 꺼져 있거나 주소가 바뀌었을 수 있습니다. 잠시 후 다시 시도해 주세요."
        if ($proc -and -not $proc.HasExited) { $proc.Kill() }
        if (-not $NoWait) { Read-Host "  엔터를 누르면 닫힙니다" }
        exit 1
    }

    Info "서버 응답 확인 중 ..."
    $info = Test-Minecraft $port
    Say ""
    if ($info) {
        Ok "연결됐습니다!  (서버 $($info.version) · 접속자 $($info.online)/$($info.max))"
    } else {
        Warn "통로는 열렸지만 마인크래프트 서버가 응답하지 않습니다."
        Info "  서버가 아직 켜지는 중이거나 꺼져 있을 수 있습니다."
        Info "  그래도 아래 주소로 시도해 보세요. 안 되면 잠시 뒤 이 창을 다시 실행하세요."
    }
    Say ""
    Say "  마인크래프트 -> 멀티플레이 -> 서버 추가에 이 주소를 넣으세요" "White"
    Say ""
    Say "      127.0.0.1:$port" "Yellow"
    Say ""
    Info "'localhost' 말고 반드시 위 숫자 주소를 넣으세요. localhost 는 IPv6 로 먼저"
    Info "  연결을 시도해서 실패할 수 있습니다."
    Say ""
    Warn "이 창을 닫으면 접속이 끊깁니다. 게임하는 동안 켜 두세요."
    Warn "마인크래프트는 반드시 이 창을 띄운 것과 같은 PC 에서 실행해야 합니다."
    Say ""

    if ($NoWait) { if ($proc -and -not $proc.HasExited) { $proc.Kill() }; exit 0 }

    try {
        while (-not $proc.HasExited) { Start-Sleep -Seconds 2 }
        Warn "접속이 끊어졌습니다."
    } finally {
        if ($proc -and -not $proc.HasExited) { $proc.Kill() }
    }
    Read-Host "  엔터를 누르면 닫힙니다"
} catch {
    Err $_.Exception.Message
    Say ""
    if (-not $NoWait) { Read-Host "  엔터를 누르면 닫힙니다" }
    exit 1
}
