"""fastunnel GUI (윈도우 우선, tkinter 기반 - 추가 설치 불필요).

설계 원칙
- 터널 작업은 전부 작업 스레드에서 돌리고, 위젯은 메인 스레드에서만 건드린다.
  스레드 → GUI 전달은 큐 하나로만 한다 (tkinter 는 스레드 안전하지 않음).
- 어떤 단계가 실패해도 창이 닫히거나 멈추지 않는다. 실패는 로그 + 안내 대화상자로 보여주고
  버튼 상태를 원래대로 되돌린다.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from typing import Any, Callable, Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError as exc:  # pragma: no cover - tkinter 없는 파이썬
    raise SystemExit(
        "GUI를 쓰려면 tkinter가 필요합니다.\n"
        "  · 윈도우: python.org 설치본을 쓰거나 설치 시 'tcl/tk and IDLE' 옵션을 켜세요.\n"
        "  · 우분투/데비안: sudo apt install python3-tk\n"
        "  · 페도라/RHEL: sudo dnf install python3-tkinter\n"
        "원인: %s" % exc
    )

from . import __version__, binaries, config as config_mod, firewall as fw_mod, util
from .notify import Notifier
from .runner import SessionError, TunnelSession

PAD = 8
LOCK_FILE = "gui.lock"

STATUS_TAGS = {
    "online": ("#1a7f37", ""),
    "error": ("#b42318", ""),
    "starting": ("#9a6700", ""),
    "restarting": ("#9a6700", ""),
    "stopped": ("#57606a", ""),
}


# ----------------------------------------------------------------------
# 단일 실행 잠금
# ----------------------------------------------------------------------


def acquire_lock() -> Optional[str]:
    """이미 GUI가 떠 있으면 그 PID 를 문자열로 반환한다."""
    path = os.path.join(util.state_dir(), LOCK_FILE)
    data = util.read_json_file(path, default=None)
    if isinstance(data, dict):
        pid = int(data.get("pid", 0) or 0)
        if pid and pid != os.getpid() and util.pid_alive(pid):
            return str(pid)
    try:
        util.write_json_file(path, {"pid": os.getpid(), "started": time.time()})
    except OSError:
        pass
    return None


def release_lock() -> None:
    try:
        os.remove(os.path.join(util.state_dir(), LOCK_FILE))
    except OSError:
        pass


# ----------------------------------------------------------------------
# 보조 대화상자
# ----------------------------------------------------------------------


class FieldDialog(tk.Toplevel):
    """라벨 + 입력칸 목록을 받아 값을 편집하는 공용 대화상자."""

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        fields: List[Dict[str, Any]],
        values: Dict[str, Any],
        extra_button: Optional[Dict[str, Any]] = None,
        note: str = "",
    ):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.result: Optional[Dict[str, Any]] = None
        self.vars: Dict[str, tk.Variable] = {}

        frame = ttk.Frame(self, padding=PAD * 2)
        frame.pack(fill="both", expand=True)

        if note:
            ttk.Label(frame, text=note, foreground="#57606a", wraplength=520).grid(
                row=0, column=0, columnspan=3, sticky="w", pady=(0, PAD)
            )

        for index, field in enumerate(fields, start=1):
            key = field["key"]
            ttk.Label(frame, text=field["label"]).grid(
                row=index, column=0, sticky="w", padx=(0, PAD), pady=3
            )
            kind = field.get("kind", "text")
            if kind == "bool":
                var: tk.Variable = tk.BooleanVar(value=bool(values.get(key, False)))
                ttk.Checkbutton(frame, variable=var).grid(row=index, column=1, sticky="w")
            elif kind == "choice":
                var = tk.StringVar(value=str(values.get(key, "")))
                ttk.Combobox(
                    frame,
                    textvariable=var,
                    values=field["values"],
                    state="readonly",
                    width=field.get("width", 44),
                ).grid(row=index, column=1, sticky="we", pady=3)
            else:
                var = tk.StringVar(value=str(values.get(key, "") or ""))
                entry = ttk.Entry(
                    frame,
                    textvariable=var,
                    width=field.get("width", 46),
                    show="*" if field.get("secret") else "",
                )
                entry.grid(row=index, column=1, sticky="we", pady=3)
                if field.get("browse"):
                    ttk.Button(
                        frame, text="찾기", width=6, command=lambda v=var: self._browse(v)
                    ).grid(row=index, column=2, padx=(4, 0))
            self.vars[key] = var
            if field.get("help"):
                ttk.Label(
                    frame, text=field["help"], foreground="#57606a", font=("Segoe UI", 8)
                ).grid(row=index, column=1, columnspan=2, sticky="w", pady=(0, 4))

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields) + 2, column=0, columnspan=3, sticky="e", pady=(PAD, 0))
        if extra_button:
            ttk.Button(
                buttons, text=extra_button["text"], command=lambda: extra_button["command"](self)
            ).pack(side="left", padx=(0, PAD))
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="left", padx=4)
        ttk.Button(buttons, text="확인", command=self._accept).pack(side="left")

        self.bind("<Return>", lambda _e: self._accept())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.update_idletasks()
        self._center(master)

    def _center(self, master: tk.Misc) -> None:
        try:
            x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
            y = master.winfo_rooty() + 80
            self.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
        except Exception:  # noqa: BLE001
            pass

    def _browse(self, var: tk.Variable) -> None:
        path = filedialog.askopenfilename(parent=self, title="실행 파일 선택")
        if path:
            var.set(path)

    def current(self) -> Dict[str, Any]:
        return {key: var.get() for key, var in self.vars.items()}

    def _accept(self) -> None:
        self.result = self.current()
        self.destroy()


# ----------------------------------------------------------------------
# 메인 창
# ----------------------------------------------------------------------


class FastunnelApp(tk.Tk):
    def __init__(self, config_path: Optional[str] = None):
        super().__init__()
        self.config_path = config_path
        self.cfg = config_mod.load(None, config_path)

        self.title("fastunnel %s" % __version__)
        self.minsize(940, 660)
        self.geometry("1040x760")

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.session: Optional[TunnelSession] = None
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.running = False
        self.closing = False
        self.rows: List[Dict[str, Any]] = []
        self._pending_notify = False
        self._last_notify = 0.0

        self._build_style()
        self._build_widgets()
        self._load_into_widgets()

        # 스레드 어디서 로그가 나오든 큐로만 받는다.
        util.add_sink(self._log_sink)
        self.report_callback_exception = self._on_tk_error
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(120, self._drain_events)
        self.after(400, self._startup_checks)

    # ------------------------------------------------------------------
    # UI 구성
    # ------------------------------------------------------------------
    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            if util.IS_WINDOWS and "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:  # noqa: BLE001
            pass
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Hint.TLabel", foreground="#57606a")

    def _build_widgets(self) -> None:
        root = ttk.Frame(self, padding=PAD)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=3)
        root.rowconfigure(5, weight=2)

        # --- 터널 설정 -------------------------------------------------
        box = ttk.LabelFrame(root, text=" 터널 설정 ", padding=PAD)
        box.grid(row=0, column=0, sticky="we")
        for col in (1, 7):
            box.columnconfigure(col, weight=1)

        ttk.Label(box, text="포트").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.var_ports = tk.StringVar()
        entry = ttk.Entry(box, textvariable=self.var_ports, width=34)
        entry.grid(row=0, column=1, sticky="we", padx=(0, PAD))
        entry.bind("<Return>", lambda _e: self.on_start())
        self.entry_ports = entry

        ttk.Label(box, text="프로토콜").grid(row=0, column=2, sticky="w", padx=(0, 6))
        self.var_proto = tk.StringVar(value="tcp")
        ttk.Combobox(
            box, textvariable=self.var_proto, values=["tcp", "http", "https"],
            state="readonly", width=8,
        ).grid(row=0, column=3, padx=(0, PAD))

        ttk.Label(box, text="제공자").grid(row=0, column=4, sticky="w", padx=(0, 6))
        self.var_provider = tk.StringVar(value="cloudflared")
        ttk.Combobox(
            box, textvariable=self.var_provider, values=["cloudflared", "ngrok"],
            state="readonly", width=13,
        ).grid(row=0, column=5, padx=(0, PAD))

        ttk.Label(box, text="업데이트").grid(row=0, column=6, sticky="w", padx=(0, 6))
        self.var_update = tk.StringVar(value="auto")
        ttk.Combobox(
            box, textvariable=self.var_update, values=["auto", "skip", "force"],
            state="readonly", width=8,
        ).grid(row=0, column=7, sticky="w")

        ttk.Label(
            box,
            text="예: 8000-8005,9000  ·  범위와 개별 포트를 쉼표로 섞어 쓸 수 있습니다",
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))

        # --- 옵션 ------------------------------------------------------
        opts = ttk.LabelFrame(root, text=" 옵션 ", padding=PAD)
        opts.grid(row=1, column=0, sticky="we", pady=(PAD, 0))
        opts.columnconfigure(8, weight=1)

        self.var_firewall = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts, text="방화벽 자동 설정", variable=self.var_firewall,
            command=self._refresh_status,
        ).grid(row=0, column=0, sticky="w", padx=(0, PAD))

        ttk.Label(opts, text="방향").grid(row=0, column=1, sticky="w", padx=(0, 4))
        self.var_direction = tk.StringVar(value="both")
        ttk.Combobox(
            opts, textvariable=self.var_direction, values=["both", "in", "out"],
            state="readonly", width=7,
        ).grid(row=0, column=2, padx=(0, PAD))

        self.var_persist = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="종료 후 규칙 유지", variable=self.var_persist).grid(
            row=0, column=3, sticky="w", padx=(0, PAD * 2)
        )

        self.var_notify = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="알림 보내기", variable=self.var_notify).grid(
            row=0, column=4, sticky="w", padx=(0, 4)
        )
        ttk.Button(opts, text="알림 설정…", command=self.on_notify_settings).grid(
            row=0, column=5, padx=(0, 4)
        )
        ttk.Button(opts, text="고급…", command=self.on_advanced_settings).grid(
            row=0, column=6, padx=(0, PAD)
        )
        self.lbl_channels = ttk.Label(opts, text="", style="Hint.TLabel")
        self.lbl_channels.grid(row=0, column=7, sticky="w")

        # --- 실행 버튼 -------------------------------------------------
        bar = ttk.Frame(root)
        bar.grid(row=2, column=0, sticky="we", pady=(PAD, 0))
        bar.columnconfigure(6, weight=1)

        self.btn_start = ttk.Button(
            bar, text="▶  시작", style="Accent.TButton", width=14, command=self.on_start
        )
        self.btn_start.grid(row=0, column=0, padx=(0, 6))
        self.btn_stop = ttk.Button(bar, text="■  중지", width=12, command=self.on_stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(0, PAD))

        ttk.Button(bar, text="환경 진단", command=self.on_doctor).grid(row=0, column=2, padx=3)
        ttk.Button(bar, text="방화벽 정리", command=self.on_firewall_cleanup).grid(row=0, column=3, padx=3)
        ttk.Button(bar, text="설정 저장", command=self.on_save_config).grid(row=0, column=4, padx=3)
        self.btn_elevate = ttk.Button(bar, text="관리자 권한으로 다시 실행", command=self.on_elevate)
        self.btn_elevate.grid(row=0, column=5, padx=3)

        self.lbl_perm = ttk.Label(bar, text="", style="Hint.TLabel", anchor="e")
        self.lbl_perm.grid(row=0, column=6, sticky="e")

        # --- 터널 목록 -------------------------------------------------
        list_box = ttk.LabelFrame(root, text=" 열린 터널 ", padding=(PAD, PAD, PAD, 4))
        list_box.grid(row=3, column=0, sticky="nsew", pady=(PAD, 0))
        list_box.columnconfigure(0, weight=1)
        list_box.rowconfigure(0, weight=1)

        columns = ("port", "proto", "status", "address", "restarts")
        self.tree = ttk.Treeview(list_box, columns=columns, show="headings", height=8)
        headings = {
            "port": ("포트", 70, "center"),
            "proto": ("프로토콜", 80, "center"),
            "status": ("상태", 90, "center"),
            "address": ("공개 주소", 520, "w"),
            "restarts": ("재시작", 60, "center"),
        }
        for key, (text, width, anchor) in headings.items():
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "address"))
        self.tree.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(list_box, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        for status, (color, _) in STATUS_TAGS.items():
            self.tree.tag_configure(status, foreground=color)

        self.tree.bind("<Double-1>", lambda _e: self.copy_address())
        self.tree.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="공개 주소 복사", command=self.copy_address)
        self.menu.add_command(label="접속 명령 복사", command=self.copy_hint)
        self.menu.add_command(label="브라우저에서 열기", command=self.open_browser)
        self.menu.add_separator()
        self.menu.add_command(label="전체 목록 복사", command=self.copy_all)

        actions = ttk.Frame(list_box)
        actions.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(actions, text="주소 복사", command=self.copy_address).pack(side="left", padx=(0, 4))
        ttk.Button(actions, text="접속 명령 복사", command=self.copy_hint).pack(side="left", padx=4)
        ttk.Button(actions, text="브라우저 열기", command=self.open_browser).pack(side="left", padx=4)
        ttk.Button(actions, text="전체 복사", command=self.copy_all).pack(side="left", padx=4)
        self.lbl_count = ttk.Label(actions, text="", style="Hint.TLabel")
        self.lbl_count.pack(side="left", padx=PAD)

        # --- 로그 ------------------------------------------------------
        log_box = ttk.LabelFrame(root, text=" 로그 ", padding=(PAD, PAD, PAD, 4))
        log_box.grid(row=5, column=0, sticky="nsew", pady=(PAD, 0))
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)

        self.log = tk.Text(log_box, height=10, wrap="word", state="disabled",
                           font=("Consolas", 9), background="#111418", foreground="#d5dae0")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.tag_configure("info", foreground="#7fc8ff")
        self.log.tag_configure("ok", foreground="#6ee787")
        self.log.tag_configure("warn", foreground="#ffd166")
        self.log.tag_configure("error", foreground="#ff8080")
        self.log.tag_configure("debug", foreground="#8b949e")

        log_actions = ttk.Frame(log_box)
        log_actions.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(log_actions, text="로그 지우기", command=self.clear_log).pack(side="left", padx=(0, 4))
        ttk.Button(log_actions, text="로그 폴더 열기", command=self.open_log_dir).pack(side="left", padx=4)
        ttk.Button(log_actions, text="로그 복사", command=self.copy_log).pack(side="left", padx=4)
        self.var_debug = tk.BooleanVar(value=False)
        ttk.Checkbutton(log_actions, text="상세 로그", variable=self.var_debug).pack(side="left", padx=PAD)

        # --- 상태줄 ----------------------------------------------------
        self.status = ttk.Label(self, text="준비됨", relief="sunken", anchor="w", padding=(PAD, 3))
        self.status.pack(side="bottom", fill="x")

    # ------------------------------------------------------------------
    # 설정 <-> 위젯
    # ------------------------------------------------------------------
    def _load_into_widgets(self) -> None:
        cfg = self.cfg
        self.var_ports.set(str(cfg.get("ports", "") or ""))
        self.var_proto.set(str(cfg.get("proto", "tcp") or "tcp"))
        self.var_provider.set(str(cfg.get("provider", "cloudflared") or "cloudflared"))
        update = str(cfg.get("update", "auto") or "auto")
        self.var_update.set(update if update in ("auto", "skip", "force") else "auto")
        self.var_firewall.set(cfg.bool("firewall"))
        self.var_direction.set(str(cfg.get("firewall_direction", "both") or "both"))
        self.var_persist.set(cfg.bool("firewall_persist"))
        self.var_notify.set(cfg.bool("notify"))
        self._refresh_status()

    def _collect_overrides(self) -> Dict[str, Any]:
        return {
            "ports": self.var_ports.get().strip(),
            "proto": self.var_proto.get(),
            "provider": self.var_provider.get(),
            "update": self.var_update.get(),
            "firewall": bool(self.var_firewall.get()),
            "firewall_direction": self.var_direction.get(),
            "firewall_persist": bool(self.var_persist.get()),
            "notify": bool(self.var_notify.get()),
        }

    def _current_config(self):
        """화면 값 + 대화상자에서 편집한 값을 합쳐 실제 사용할 설정을 만든다.

        파일을 다시 읽지 않는다. 그러면 '알림 설정'·'고급 설정'에서 방금 입력한 값이
        저장 전이라는 이유로 무시되기 때문이다.
        """
        data = dict(self.cfg.data)
        data.update(self._collect_overrides())
        return config_mod.from_dict(data, self.cfg.sources + ["화면 입력"])

    def _refresh_status(self) -> None:
        admin = util.is_admin()
        if util.IS_WINDOWS:
            perm = "관리자 권한" if admin else "일반 사용자"
        else:
            perm = "root" if admin else ("sudo 사용 가능" if util.has_sudo() else "일반 사용자")
        backend = fw_mod.backend_label(fw_mod.detect_backend()) if self.var_firewall.get() else "사용 안 함"
        self.lbl_perm.configure(text="%s  ·  방화벽: %s" % (perm, backend))
        self.btn_elevate.configure(
            state="disabled" if (admin or self.running) else "normal"
        )
        try:
            channels = Notifier(self._current_config()).channels()
        except Exception:  # noqa: BLE001
            channels = []
        self.lbl_channels.configure(
            text=("알림 채널: " + ", ".join(channels)) if channels else "알림 채널 없음"
        )

    # ------------------------------------------------------------------
    # 시작할 때 점검
    # ------------------------------------------------------------------
    def _startup_checks(self) -> None:
        util.info("fastunnel %s GUI 시작" % __version__)
        util.info("작업 폴더: %s" % util.home_dir())
        leftovers = []
        try:
            leftovers = fw_mod.list_leftovers()
        except Exception as exc:  # noqa: BLE001
            util.debug("잔여 규칙 확인 실패: %s" % exc)
        if leftovers:
            count = sum(len(d.get("rules") or []) for _, d in leftovers)
            if messagebox.askyesno(
                "방화벽 규칙 정리",
                "이전에 비정상 종료되어 남은 방화벽 규칙 %d개가 있습니다.\n지금 정리할까요?" % count,
                parent=self,
            ):
                self.on_firewall_cleanup()

    # ------------------------------------------------------------------
    # 로그 / 이벤트 큐
    # ------------------------------------------------------------------
    def _log_sink(self, level: str, message: str) -> None:
        """util 로그 훅. 작업 스레드에서 호출되므로 큐에 넣기만 한다."""
        if level == "progress":
            self.events.put(("progress", message))
        else:
            self.events.put(("log", level, message))

    def _post(self, *event: Any) -> None:
        self.events.put(event)

    def _drain_events(self) -> None:
        try:
            for _ in range(200):
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                self._handle_event(event)
        except Exception:  # noqa: BLE001  - 이벤트 처리 실패로 루프가 멈추면 안 된다
            self._append_log("error", "이벤트 처리 오류:\n%s" % traceback.format_exc())
        finally:
            if not self.closing:
                self.after(120, self._drain_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "log":
            _, level, message = event
            if level == "debug" and not self.var_debug.get():
                return
            self._append_log(level, message)
        elif kind == "progress":
            self.status.configure(text=event[1] or self._idle_status())
        elif kind == "tunnels":
            self._update_table(event[1])
        elif kind == "state":
            self._set_running(event[1] == "running")
        elif kind == "fatal":
            _, title, message, hint = event
            self._set_running(False)
            self.status.configure(text="오류: %s" % title)
            messagebox.showerror(title, message + (("\n\n" + hint) if hint else ""), parent=self)
        elif kind == "info_box":
            messagebox.showinfo(event[1], event[2], parent=self)

    def _append_log(self, level: str, message: str) -> None:
        try:
            self.log.configure(state="normal")
            self.log.insert("end", "%s %s\n" % (time.strftime("%H:%M:%S"), message), level)
            # 너무 길어지면 앞부분을 버린다 (메모리 보호)
            if int(self.log.index("end-1c").split(".")[0]) > 2000:
                self.log.delete("1.0", "500.0")
            self.log.see("end")
        finally:
            self.log.configure(state="disabled")

    def _idle_status(self) -> str:
        return "실행 중 · %d/%d 연결됨" % (
            len([r for r in self.rows if r.get("url")]),
            len(self.rows),
        ) if self.running else "준비됨"

    # ------------------------------------------------------------------
    # 터널 목록
    # ------------------------------------------------------------------
    def _update_table(self, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows
        selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            status = row.get("status", "starting")
            display = row.get("address") or ("(%s)" % row.get("error", "")[:60] if row.get("error") else "…")
            item = self.tree.insert(
                "",
                "end",
                iid=str(row["port"]),
                values=(
                    row["port"],
                    str(row.get("proto", "")).upper(),
                    row.get("status_text", status),
                    display,
                    row.get("restarts", 0) or "",
                ),
                tags=(status,),
            )
            if item in selected:
                self.tree.selection_add(item)
        online = len([r for r in rows if r.get("url")])
        self.lbl_count.configure(text="%d/%d 연결됨" % (online, len(rows)))
        self.status.configure(text=self._idle_status())

    def _selected_row(self) -> Optional[Dict[str, Any]]:
        selection = self.tree.selection()
        if not selection:
            if len(self.rows) == 1:
                return self.rows[0]
            messagebox.showinfo("선택 필요", "목록에서 터널을 먼저 선택하세요.", parent=self)
            return None
        port = selection[0]
        for row in self.rows:
            if str(row["port"]) == port:
                return row
        return None

    def _popup_menu(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _copy(self, text: str, label: str) -> None:
        if not text:
            messagebox.showinfo("복사할 내용 없음", "%s 가 아직 없습니다." % label, parent=self)
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            self.status.configure(text="%s 복사됨: %s" % (label, text[:70]))
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("복사 실패", "클립보드에 복사하지 못했습니다.\n%s" % exc, parent=self)

    def copy_address(self) -> None:
        row = self._selected_row()
        if row:
            self._copy(row.get("address", ""), "공개 주소")

    def copy_hint(self) -> None:
        row = self._selected_row()
        if not row:
            return
        hint = row.get("client_hint") or row.get("address", "")
        self._copy(hint, "접속 명령")

    def open_browser(self) -> None:
        row = self._selected_row()
        if not row:
            return
        url = row.get("url", "")
        if not url:
            messagebox.showinfo("주소 없음", "아직 공개 주소가 없습니다.", parent=self)
            return
        if row.get("proto") not in ("http", "https"):
            if not messagebox.askyesno(
                "TCP 터널",
                "TCP 터널은 브라우저로 바로 열 수 없습니다.\n"
                "접속하려면 '접속 명령 복사'로 나온 명령을 접속하는 쪽에서 실행하세요.\n\n"
                "그래도 주소를 브라우저로 열어볼까요?",
                parent=self,
            ):
                return
        try:
            webbrowser.open(url if url.startswith("http") else "https://" + url)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("열기 실패", str(exc), parent=self)

    def copy_all(self) -> None:
        if not self.rows:
            messagebox.showinfo("복사할 내용 없음", "열린 터널이 없습니다.", parent=self)
            return
        lines = []
        for row in self.rows:
            lines.append("%s %s → %s" % (str(row.get("proto", "")).upper(), row["port"],
                                         row.get("address") or "(대기 중)"))
            if row.get("client_hint"):
                lines.append("    %s" % row["client_hint"])
        self._copy("\n".join(lines), "터널 목록")

    # ------------------------------------------------------------------
    # 시작 / 중지
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        if self.running:
            return
        try:
            cfg = self._current_config()
            session = TunnelSession(cfg)
        except SessionError as exc:
            messagebox.showwarning("설정 확인", str(exc) + (("\n\n" + exc.hint) if exc.hint else ""), parent=self)
            self.entry_ports.focus_set()
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("설정 오류", "설정을 읽지 못했습니다.\n%s" % exc, parent=self)
            return

        # 방화벽을 켰는데 권한이 없으면 어떻게 할지 물어본다.
        if session.needs_elevation():
            choice = self._ask_elevation()
            if choice == "cancel":
                return
            if choice == "elevate":
                self.on_elevate()
                return
            if choice == "without":
                self.var_firewall.set(False)
                cfg = self._current_config()
                try:
                    session = TunnelSession(cfg)
                except SessionError as exc:
                    messagebox.showwarning("설정 확인", str(exc), parent=self)
                    return

        self._save_config(silent=True)
        self.session = session
        self.stop_event.clear()
        self._pending_notify = False
        self._last_notify = 0.0
        self._set_running(True)
        self.status.configure(text="터널 준비 중 …")
        util.info("시작: %s" % session.summary())

        self.worker = threading.Thread(target=self._run_session, args=(session,), daemon=True)
        self.worker.start()

    def _ask_elevation(self) -> str:
        dialog = tk.Toplevel(self)
        dialog.title("권한 필요")
        dialog.transient(self)
        dialog.resizable(False, False)
        result = {"value": "cancel"}

        frame = ttk.Frame(dialog, padding=PAD * 2)
        frame.pack(fill="both", expand=True)
        need = "관리자" if util.IS_WINDOWS else "root"
        ttk.Label(
            frame,
            text="방화벽 규칙을 추가하려면 %s 권한이 필요합니다.\n어떻게 할까요?" % need,
            justify="left",
        ).pack(anchor="w", pady=(0, PAD))

        def pick(value: str) -> None:
            result["value"] = value
            dialog.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e")
        ttk.Button(buttons, text="%s 권한으로 다시 실행" % need, command=lambda: pick("elevate")).pack(side="left", padx=4)
        ttk.Button(buttons, text="방화벽 없이 계속", command=lambda: pick("without")).pack(side="left", padx=4)
        ttk.Button(buttons, text="취소", command=lambda: pick("cancel")).pack(side="left", padx=4)
        dialog.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
        dialog.grab_set()
        self.wait_window(dialog)
        return result["value"]

    def _snapshot(self, session: TunnelSession) -> List[Dict[str, Any]]:
        rows = []
        for tunnel in session.tunnels:
            try:
                data = tunnel.to_dict()
                data["status_text"] = tunnel.status_text()
                rows.append(data)
            except Exception as exc:  # noqa: BLE001
                util.debug("스냅샷 실패: %s" % exc)
        return rows

    def _run_session(self, session: TunnelSession) -> None:
        """작업 스레드: 준비 → 시작 → 감시. 위젯은 절대 직접 만지지 않는다."""
        try:
            session.prepare()
            self._post("tunnels", self._snapshot(session))
            session.start()
            self._post("state", "running")
            session.wait_ready()
            self._post("tunnels", self._snapshot(session))
            session.save_state()

            online = session.online_count()
            if online:
                util.ok("터널 %d/%d개 연결됨" % (online, len(session.tunnels)))
            else:
                util.warn("아직 연결된 터널이 없습니다. 로그를 확인하세요.")
            session.notify_started()

            while not self.stop_event.is_set():
                if self.stop_event.wait(1.0):
                    break
                changed = session.poll()
                if changed:
                    self._post("tunnels", self._snapshot(session))
                    session.save_state()
                    self._pending_notify = True
                if self._pending_notify and time.time() - self._last_notify > 5:
                    session.notify_changed()
                    self._last_notify = time.time()
                    self._pending_notify = False
        except SessionError as exc:
            self._post("fatal", "터널을 시작하지 못했습니다", str(exc), exc.hint)
        except Exception as exc:  # noqa: BLE001
            util.debug(traceback.format_exc())
            self._post("fatal", "예기치 못한 오류", str(exc),
                       "‘상세 로그’를 켜고 다시 시도하면 원인을 볼 수 있습니다.")
        finally:
            try:
                session.stop()
            except Exception as exc:  # noqa: BLE001
                util.warn("정리 중 오류: %s" % exc)
            self._post("tunnels", self._snapshot(session))
            self._post("state", "stopped")
            util.ok("정리 완료")

    def on_stop(self) -> None:
        if not self.running:
            return
        self.btn_stop.configure(state="disabled")
        self.status.configure(text="정리 중 …")
        self.stop_event.set()

    def _set_running(self, running: bool) -> None:
        self.running = running
        self.btn_start.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")
        for widget_var in (self.entry_ports,):
            widget_var.configure(state="disabled" if running else "normal")
        self.status.configure(text=self._idle_status())
        self._refresh_status()

    # ------------------------------------------------------------------
    # 도구 버튼
    # ------------------------------------------------------------------
    def _run_in_thread(self, func: Callable[[], None], label: str) -> None:
        """오래 걸릴 수 있는 작업을 UI를 막지 않고 실행."""

        def wrapper() -> None:
            try:
                func()
            except Exception as exc:  # noqa: BLE001
                util.debug(traceback.format_exc())
                self._post("fatal", label, str(exc), "")

        threading.Thread(target=wrapper, daemon=True).start()

    def on_doctor(self) -> None:
        def work() -> None:
            util.info("── 환경 진단 ──")
            util.info("OS %s (%s) · Python %s" % (util.os_tag(), util.arch_tag(), sys.version.split()[0]))
            util.info("권한: %s" % ("관리자/root" if util.is_admin() else "일반 사용자"))
            util.info("방화벽 백엔드: %s" % fw_mod.backend_label(fw_mod.detect_backend()))
            for name in ("cloudflared", "ngrok"):
                path = binaries.locate(name)
                if path:
                    util.ok("%s: %s (%s)" % (name, binaries.detect_version(path, name) or "?", path))
                else:
                    util.warn("%s: 설치되지 않음 (시작하면 자동으로 내려받습니다)" % name)
            for label, url in (
                ("GitHub", "https://api.github.com"),
                ("Cloudflare", "https://api.trycloudflare.com"),
                ("Discord", "https://discord.com/api/v10/gateway"),
            ):
                status, _ = util.http_request(url, timeout=6)
                (util.ok if status else util.warn)("%s 연결: %s" % (label, status or "실패"))
            channels = Notifier(self._current_config()).channels()
            util.info("알림 채널: %s" % (", ".join(channels) or "없음"))
            util.info("── 진단 끝 ──")

        self._run_in_thread(work, "진단 실패")

    def on_firewall_cleanup(self) -> None:
        def work() -> None:
            removed = fw_mod.cleanup()
            self._post("info_box", "방화벽 정리", "규칙 %d개를 제거했습니다." % removed)

        self._run_in_thread(work, "방화벽 정리 실패")

    def on_elevate(self) -> None:
        if self.running:
            messagebox.showinfo("실행 중", "터널을 먼저 중지한 뒤 다시 시도하세요.", parent=self)
            return
        self._save_config(silent=True)
        try:
            from .cli import relaunch_elevated

            args = ["gui"]
            if self.config_path:
                args = ["-c", self.config_path, "gui"]
            code = relaunch_elevated(args)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("권한 상승 실패", str(exc), parent=self)
            return
        if code == 0:
            release_lock()
            self.closing = True
            self.destroy()

    def on_save_config(self) -> None:
        self._save_config(silent=False)

    def _save_config(self, silent: bool = True) -> None:
        target = self.config_path or os.path.join(util.home_dir(), "config.json")
        data = dict(self.cfg.data)
        data.update(self._collect_overrides())
        data.pop("_help", None)
        try:
            util.write_json_file(target, data)
            self.cfg = config_mod.load(None, self.config_path)
            if not silent:
                messagebox.showinfo("설정 저장", "저장했습니다:\n%s" % target, parent=self)
            else:
                util.debug("설정 저장: %s" % target)
        except OSError as exc:
            messagebox.showerror(
                "설정 저장 실패",
                "설정 파일을 저장하지 못했습니다.\n%s\n\n%s" % (target, exc),
                parent=self,
            )

    # ------------------------------------------------------------------
    # 설정 대화상자
    # ------------------------------------------------------------------
    def on_notify_settings(self) -> None:
        fields = [
            {"key": "discord_webhook", "label": "디스코드 웹훅 URL",
             "help": "채널 편집 → 연동 → 웹후크 에서 만든 URL"},
            {"key": "discord_bot_token", "label": "디스코드 봇 토큰", "secret": True},
            {"key": "discord_channel_id", "label": "디스코드 채널 ID"},
            {"key": "mention", "label": "멘션", "help": "예: @here 또는 <@&역할ID>"},
            {"key": "notify_on_change", "label": "주소 변경 시에도 알림", "kind": "bool"},
        ]
        values = {key: self.cfg.get(key, "") for key in
                  ("discord_webhook", "discord_bot_token", "discord_channel_id", "mention")}
        values["notify_on_change"] = self.cfg.bool("notify_on_change")

        def send_test(dialog: FieldDialog) -> None:
            current = dialog.current()

            def work() -> None:
                cfg = config_mod.from_dict(
                    {**self.cfg.data, **self._collect_overrides(), **current},
                    ["알림 설정 테스트"],
                )
                notifier = Notifier(cfg, "test", self.var_proto.get())
                if not notifier.configured():
                    self._post("info_box", "알림 테스트",
                               "설정된 알림 채널이 없습니다.\n웹훅 URL 또는 봇 토큰/채널 ID를 입력하세요.")
                    return
                ok_sent = notifier.send_test()
                self._post(
                    "info_box", "알림 테스트",
                    "테스트 메시지를 보냈습니다. 디스코드를 확인하세요." if ok_sent
                    else "전송에 실패했습니다. 로그를 확인하세요.",
                )

            self._run_in_thread(work, "알림 테스트 실패")

        dialog = FieldDialog(
            self, "알림 설정", fields, values,
            extra_button={"text": "테스트 전송", "command": send_test},
            note="터널이 열리면 임시 도메인 주소를 이 채널로 보냅니다. 비워 두면 그 채널은 사용하지 않습니다.",
        )
        self.wait_window(dialog)
        if dialog.result is not None:
            data = dict(self.cfg.data)
            data.update(dialog.result)
            self.cfg = config_mod.Config(data, self.cfg.sources + ["GUI 알림 설정"])
            self._refresh_status()

    def on_advanced_settings(self) -> None:
        fields = [
            {"key": "host", "label": "로컬 호스트", "help": "터널이 연결할 대상 (기본 localhost)"},
            {"key": "bin_path", "label": "실행 파일 경로", "browse": True,
             "help": "비워 두면 자동으로 내려받아 사용합니다"},
            {"key": "ngrok_authtoken", "label": "ngrok 토큰", "secret": True},
            {"key": "ngrok_region", "label": "ngrok 리전", "help": "us, eu, ap, au, sa, jp, in"},
            {"key": "start_delay", "label": "기동 간격(초)", "help": "여러 포트를 순차로 띄우는 간격"},
            {"key": "ready_timeout", "label": "주소 대기(초)"},
            {"key": "max_restarts", "label": "최대 재시작 횟수"},
            {"key": "restart", "label": "죽으면 자동 재시작", "kind": "bool"},
            {"key": "extra_args", "label": "추가 인자", "help": "터널 실행 파일에 그대로 전달"},
        ]
        values = {key: self.cfg.get(key, "") for key in
                  ("host", "bin_path", "ngrok_authtoken", "ngrok_region",
                   "start_delay", "ready_timeout", "max_restarts", "extra_args")}
        values["restart"] = self.cfg.bool("restart")
        dialog = FieldDialog(self, "고급 설정", fields, values,
                             note="대부분 기본값 그대로 두면 됩니다.")
        self.wait_window(dialog)
        if dialog.result is None:
            return
        result = dict(dialog.result)
        # 숫자 필드 검증 - 잘못 입력해도 저장은 되지만 기본값으로 되돌린다
        for key, caster, default in (
            ("start_delay", float, 1.0),
            ("ready_timeout", int, 90),
            ("max_restarts", int, 5),
        ):
            try:
                result[key] = caster(str(result.get(key, "")).strip() or default)
            except ValueError:
                util.warn("%s 값이 숫자가 아니어서 기본값(%s)으로 되돌립니다." % (key, default))
                result[key] = default
        data = dict(self.cfg.data)
        data.update(result)
        self.cfg = config_mod.Config(data, self.cfg.sources + ["GUI 고급 설정"])

    # ------------------------------------------------------------------
    # 로그 버튼
    # ------------------------------------------------------------------
    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def copy_log(self) -> None:
        self._copy(self.log.get("1.0", "end").strip(), "로그")

    def open_log_dir(self) -> None:
        path = util.log_dir()
        try:
            if util.IS_WINDOWS:
                os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
            elif util.IS_MAC:
                util.run_cmd(["open", path], timeout=10)
            else:
                util.run_cmd(["xdg-open", path], timeout=10)
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo("폴더 열기 실패", "%s\n\n%s" % (path, exc), parent=self)

    # ------------------------------------------------------------------
    # 예외 처리 / 종료
    # ------------------------------------------------------------------
    def _on_tk_error(self, exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        self._append_log("error", "GUI 오류:\n%s" % detail)
        self._write_crash_log(detail)
        messagebox.showerror(
            "오류",
            "화면 처리 중 오류가 발생했습니다.\n%s\n\n자세한 내용은 로그를 확인하세요." % exc_value,
            parent=self,
        )

    def _write_crash_log(self, detail: str) -> None:
        try:
            path = os.path.join(util.log_dir(), "gui-error.log")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n===== %s =====\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), detail))
        except Exception:  # noqa: BLE001
            pass

    def on_close(self) -> None:
        if self.running:
            if not messagebox.askokcancel(
                "종료 확인",
                "터널이 실행 중입니다.\n종료하면 터널이 닫히고 방화벽 규칙도 원래대로 되돌립니다.\n계속할까요?",
                parent=self,
            ):
                return
            self.status.configure(text="정리 중 … 잠시만 기다려 주세요")
            self.btn_stop.configure(state="disabled")
            self.btn_start.configure(state="disabled")
            self.stop_event.set()
            self._wait_and_destroy(deadline=time.time() + 20)
            return
        self._finish()

    def _wait_and_destroy(self, deadline: float) -> None:
        """작업 스레드가 정리를 마칠 때까지 UI를 살려 둔 채 기다린다."""
        worker = self.worker
        if worker is not None and worker.is_alive() and time.time() < deadline:
            self.after(200, lambda: self._wait_and_destroy(deadline))
            return
        if worker is not None and worker.is_alive():
            util.warn("정리가 끝나지 않아 강제로 종료합니다.")
        self._finish()

    def _finish(self) -> None:
        self.closing = True
        util.remove_sink(self._log_sink)
        release_lock()
        try:
            self._save_config(silent=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.destroy()
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------------------------------------
def launch(config_path: Optional[str] = None) -> int:
    """GUI 실행 진입점. CLI 의 `fastunnel gui` 가 호출한다."""
    if util.IS_WINDOWS:
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001  - 구버전 윈도우에서는 없어도 그만
            pass

    other = acquire_lock()
    app: Optional[FastunnelApp] = None
    try:
        app = FastunnelApp(config_path)
    except Exception as exc:  # noqa: BLE001
        detail = traceback.format_exc()
        try:
            path = os.path.join(util.log_dir(), "gui-error.log")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\n===== 시작 실패 %s =====\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), detail))
        except Exception:  # noqa: BLE001
            pass
        util.error("GUI를 시작하지 못했습니다: %s" % exc)
        try:
            messagebox.showerror("fastunnel", "GUI를 시작하지 못했습니다.\n%s" % exc)
        except Exception:  # noqa: BLE001
            pass
        release_lock()
        return 1

    if other:
        messagebox.showwarning(
            "이미 실행 중",
            "다른 fastunnel 창이 이미 실행 중입니다 (PID %s).\n"
            "두 창에서 같은 포트를 열면 충돌할 수 있으니 주의하세요." % other,
            parent=app,
        )

    def thread_hook(args) -> None:  # noqa: ANN001
        detail = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        util.error("작업 스레드 오류: %s" % args.exc_value)
        util.debug(detail)

    if hasattr(threading, "excepthook"):
        threading.excepthook = thread_hook  # type: ignore[assignment]

    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    finally:
        release_lock()
    return 0
