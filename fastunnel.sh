#!/usr/bin/env bash
# fastunnel 실행 래퍼 (Linux / macOS)
#   ./fastunnel.sh 8000-8005
#   sudo -E ./fastunnel.sh 8000-8005     # 방화벽 자동 설정까지 하려면 root 권한 필요
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "Python 3.8 이상이 필요합니다." >&2
    exit 1
fi

cd "$ROOT"
exec "$PYTHON" -m fastunnel "$@"
