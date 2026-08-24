# -*- coding: utf-8 -*-
"""전체 테스트 실행기.

    python tests/run_all.py                 # 전부
    python tests/run_all.py --no-network    # 네트워크 없이 (core + notify + control)
    python tests/run_all.py --no-gui        # GUI 제외
    python tests/run_all.py core notify     # 원하는 것만

pytest 같은 외부 도구가 필요 없다. 표준 라이브러리만 쓴다.
"""
from __future__ import annotations

import importlib
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODULES = [
    ("core", "test_core", "네트워크 불필요"),
    ("notify", "test_notify", "로컬 수신 서버 사용"),
    ("control", "test_control", "로컬 수신 서버 사용 - 디스코드 명령"),
    ("slash", "test_slash", "네트워크 불필요 - 슬래시 명령/WebSocket"),
    ("tunnel", "test_tunnel", "네트워크 필요 - 실제 터널을 엽니다"),
    ("gui", "test_gui", "화면 환경 필요 - 창은 띄우지 않습니다"),
]

NETWORK_TESTS = {"tunnel"}
GUI_TESTS = {"gui"}


def main(argv) -> int:
    wanted = [a for a in argv if not a.startswith("-")]
    no_network = "--no-network" in argv
    no_gui = "--no-gui" in argv

    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0

    selected = []
    for name, module, note in MODULES:
        if wanted and name not in wanted:
            continue
        if no_network and name in NETWORK_TESTS:
            continue
        if no_gui and name in GUI_TESTS:
            continue
        selected.append((name, module, note))

    if not selected:
        print("실행할 테스트가 없습니다. (사용법: python tests/run_all.py --help)")
        return 1

    print("fastunnel 테스트: %s" % ", ".join(n for n, _, _ in selected))
    started = time.time()
    failed = []
    for name, module_name, _note in selected:
        try:
            module = importlib.import_module(module_name)
            if not module.run():
                failed.append(name)
        except Exception as exc:  # noqa: BLE001
            import traceback

            print("\n=== %s ===\n  [ERROR] %s" % (name, exc))
            traceback.print_exc()
            failed.append(name)

    took = time.time() - started
    print("\n" + "=" * 60)
    if failed:
        print("실패: %s  (%.1f초)" % (", ".join(failed), took))
        return 1
    print("전체 통과  (%.1f초)" % took)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
