# -*- coding: utf-8 -*-
"""GUI 검증 (화면 환경 필요, 창은 띄우지 않음).

- 창을 withdraw() 하거나 완전 투명하게 해서 사용자 화면을 방해하지 않는다.
- 모달 대화상자는 자동 응답으로 바꿔치기해 테스트가 멈추지 않게 한다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

from _harness import Results, free_port, has_display, has_tkinter, isolated_home, local_server

isolated_home("gui")


def run() -> bool:
    r = Results("gui - 화면/예외 처리")
    if not has_tkinter():
        r.skip("전체", "tkinter 없음")
        return r.summary()
    if not has_display():
        r.skip("전체", "그래픽 환경 없음 (DISPLAY 미설정)")
        return r.summary()

    from tkinter import messagebox
    import fastunnel.gui as g
    from fastunnel import util
    from fastunnel.providers import ngrok as ngrok_mod

    util.set_verbosity(quiet=True)

    dialogs = []
    messagebox.showinfo = lambda t, m, **k: dialogs.append(("info", t, m))
    messagebox.showwarning = lambda t, m, **k: dialogs.append(("warn", t, m))
    messagebox.showerror = lambda t, m, **k: dialogs.append(("error", t, m))
    messagebox.askyesno = lambda t, m, **k: (dialogs.append(("askyesno", t, m)), False)[1]
    messagebox.askokcancel = lambda t, m, **k: (dialogs.append(("askokcancel", t, m)), True)[1]

    port = free_port(8890)
    cfg_path = os.path.join(tempfile.gettempdir(), "fastunnel-gui-test.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"ports": str(port), "firewall": False, "notify": False, "update": "skip"}, fh)

    app = g.FastunnelApp(cfg_path)
    app.withdraw()

    def pump(seconds: float = 0.5) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.update()
            app._drain_events()
            time.sleep(0.05)

    def wait_idle(timeout: float = 60) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            app.update()
            app._drain_events()
            if not app.running and (app.worker is None or not app.worker.is_alive()):
                return True
            time.sleep(0.1)
        return False

    try:
        # ---------- 설정 반영 ----------
        r.check("설정 파일이 화면에 반영", app.var_ports.get() == str(port) and app.var_firewall.get() is False)

        # ---------- 입력 검증 ----------
        for bad, label in (("abc", "숫자 아님"), ("", "빈 값")):
            dialogs.clear()
            app.var_ports.set(bad)
            app.on_start()
            r.check("잘못된 포트 거부(%s)" % label,
                    not app.running and any(d[0] == "warn" for d in dialogs))

        # ---------- 표 갱신 ----------
        app._update_table([
            {"port": 1, "proto": "tcp", "status": "online", "status_text": "연결됨",
             "address": "a.trycloudflare.com", "url": "https://a.trycloudflare.com",
             "client_hint": "cloudflared access tcp ...", "restarts": 0},
            {"port": 2, "proto": "tcp", "status": "error", "status_text": "오류",
             "address": "", "url": "", "client_hint": "", "restarts": 2, "error": "테스트"},
        ])
        r.check("표 갱신", len(app.tree.get_children()) == 2 and app.lbl_count.cget("text") == "1/2 연결됨")

        # ---------- 잘못된 실행 파일 경로 ----------
        dialogs.clear()
        app.var_ports.set(str(port))
        app.cfg.data["bin_path"] = os.path.join(tempfile.gettempdir(), "없는파일.exe")
        app.on_start()
        wait_idle()
        r.check("실행 파일 오류 안내", any(d[0] == "error" for d in dialogs),
                dialogs[-1][2][:50] if dialogs else "")
        r.check("실패 후 버튼 복구", not app.running and str(app.btn_start.cget("state")) == "normal")
        app.cfg.data["bin_path"] = ""

        # ---------- ngrok 토큰 없음 ----------
        dialogs.clear()
        if ngrok_mod.default_config_paths():
            r.skip("ngrok 토큰 안내", "이 PC에 ngrok 기본 설정이 이미 있음")
        else:
            app.var_provider.set("ngrok")
            app.on_start()
            wait_idle()
            errors = [d for d in dialogs if d[0] == "error"]
            r.check("ngrok 토큰 안내", bool(errors) and "토큰" in errors[-1][2])
            app.var_provider.set("cloudflared")

        # ---------- 작업 스레드 예외 ----------
        dialogs.clear()
        original = g.TunnelSession.prepare
        g.TunnelSession.prepare = lambda self: (_ for _ in ()).throw(RuntimeError("의도된 오류"))
        try:
            app.on_start()
            wait_idle()
        finally:
            g.TunnelSession.prepare = original
        errors = [d for d in dialogs if d[0] == "error"]
        r.check("스레드 예외를 창으로 전달", bool(errors) and "의도된 오류" in errors[-1][2])
        r.check("예외 후에도 UI 사용 가능", not app.running)

        # ---------- 권한 부족 → '방화벽 없이 계속' ----------
        dialogs.clear()
        app.var_firewall.set(True)
        asked = {"n": 0}
        app._ask_elevation = lambda: (asked.__setitem__("n", asked["n"] + 1), "without")[1]
        if util.is_admin():
            r.skip("권한 부족 안내", "이미 관리자/root 로 실행 중")
            app.var_firewall.set(False)
        else:
            app.on_start()
            pump(2.0)
            r.check("권한 부족 → 방화벽 없이 계속",
                    asked["n"] == 1 and app.var_firewall.get() is False)
            app.on_stop()
            wait_idle()

        # ---------- 실제 터널 → 복사 → 실행 중 창 닫기 ----------
        dialogs.clear()
        with local_server(port):
            app.var_ports.set(str(port))
            app.var_firewall.set(False)
            app.on_start()
            deadline = time.time() + 90
            while time.time() < deadline:
                app.update()
                app._drain_events()
                if any(row.get("url") for row in app.rows):
                    break
                time.sleep(0.2)

            online = [row for row in app.rows if row.get("url")]
            r.check("GUI에서 터널 개통", bool(online), online[0]["address"] if online else "")

            if online:
                app.tree.selection_set(str(online[0]["port"]))
                app.copy_address()
                try:
                    r.check("클립보드 복사", app.clipboard_get() == online[0]["address"])
                except Exception as exc:  # noqa: BLE001
                    r.skip("클립보드 복사", str(exc)[:40])

                app.on_close()   # askokcancel -> True
                end = time.time() + 30
                alive = True
                while time.time() < end:
                    try:
                        app.update()
                        app._drain_events()
                        if not app.winfo_exists():
                            alive = False
                            break
                    except Exception:  # noqa: BLE001  - 창이 파괴되면 예외 = 정상 종료
                        alive = False
                        break
                    time.sleep(0.2)
                r.check("실행 중 종료 → 정리 후 창 닫힘",
                        not alive and any(d[0] == "askokcancel" for d in dialogs))
            else:
                app._finish()

        lock = os.path.join(util.state_dir(), "gui.lock")
        r.check("잠금 파일 해제", not os.path.exists(lock))

        # ---------- 죽은 PID 의 잠금은 무시 ----------
        util.write_json_file(lock, {"pid": 999999, "started": 0})
        r.check("죽은 잠금 무시", g.acquire_lock() is None)
        g.release_lock()
    finally:
        try:
            if app.winfo_exists():
                app._finish()
        except Exception:  # noqa: BLE001
            pass

    return r.summary()


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
