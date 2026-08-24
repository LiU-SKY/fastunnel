#!/usr/bin/env bash
# fastunnel GUI 실행 (Linux / macOS)
#   ./fastunnel-gui.sh
#   sudo -E ./fastunnel-gui.sh   # 방화벽까지 자동 설정하려면
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PYTHON="$candidate"; break; fi
done
if [ -z "$PYTHON" ]; then
    echo "Python 3.8 이상이 필요합니다." >&2
    exit 1
fi

if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    echo "tkinter 가 없습니다. 다음 중 하나로 설치하세요:" >&2
    echo "  sudo apt install python3-tk      # 우분투/데비안" >&2
    echo "  sudo dnf install python3-tkinter # 페도라/RHEL" >&2
    exit 1
fi

cd "$ROOT"
exec "$PYTHON" -m fastunnel gui "$@"
