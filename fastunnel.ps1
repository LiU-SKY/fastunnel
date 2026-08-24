#requires -Version 5.1
<#
.SYNOPSIS
  fastunnel 실행 래퍼 (Windows).
.DESCRIPTION
  python 을 찾아 python -m fastunnel 을 실행한다.
  -Elevated 스위치를 주면 방화벽 설정을 위해 관리자 권한으로 새 창에서 실행한다.
.EXAMPLE
  .\fastunnel.ps1 8000-8005
  .\fastunnel.ps1 -Elevated run -p 8000-8005 --proto tcp
#>
param(
    [switch]$Elevated,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) {
    Write-Error 'Python 3.8 이상이 필요합니다. https://www.python.org/downloads/ 에서 설치하세요.'
    exit 1
}

if ($Elevated) {
    $quoted = ($Args | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $inner = "-NoExit -Command `"Set-Location '$root'; & '$python' -m fastunnel $quoted`""
    Start-Process powershell -Verb RunAs -ArgumentList $inner
    exit 0
}

Push-Location $root
try {
    & $python -m fastunnel @Args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
