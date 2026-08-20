#!/usr/bin/env python3
"""CGV 아이맥스 오픈 알리미 - 설정 프로그램.

표준 라이브러리(Tkinter)만 사용하므로 설치할 것이 없다.
    설정.bat 을 더블클릭하거나  python setup_gui.py

하는 일
-------
* 텔레그램 봇 토큰 / 챗 ID 입력, 챗 ID 자동 탐색, 테스트 전송
* 감시 대상(지점 + 영화) 추가/삭제 - 지점 목록은 CGV API에서 실시간으로 받아온다
* config.json 저장, .env 저장
* "지금 1회 점검"으로 watcher.py --dry-run 을 돌려 결과를 바로 확인
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import cgv_api
import notifier
import watcher

ROOT = Path(__file__).parent
PAD = {"padx": 8, "pady": 4}

# 화면에 보이는 이름 -> config.json 에 저장할 값
SCREEN_CHOICES = {
    "아이맥스": "아이맥스",
    "4DX": "4DX",
    "SCREENX": "SCREENX",
    "전 상영관 (일반관 포함)": "ALL",
}
UNIT_CHOICES = {
    "새 회차마다": "schedule",
    "새 영화만 (한 번씩)": "movie",
}
SCREEN_LABELS = {v: k for k, v in SCREEN_CHOICES.items()}
UNIT_LABELS = {v: k for k, v in UNIT_CHOICES.items()}


def run_bg(work, on_done, on_error=None):
    """work()를 백그라운드에서 돌리고 결과를 Tk 스레드로 넘긴다."""
    def runner():
        try:
            result = work()
        except Exception as exc:                      # noqa: BLE001
            if on_error:
                _tk_root.after(0, lambda: on_error(exc))
            return
        _tk_root.after(0, lambda: on_done(result))

    threading.Thread(target=runner, daemon=True).start()


_tk_root = None


# ------------------------------------------------------------------ 추가 창

class AddTargetDialog(tk.Toplevel):
    """지역 -> 지점 -> 영화 순으로 감시 대상을 고른다."""

    def __init__(self, master):
        super().__init__(master)
        self.title("감시 대상 추가")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.result = None
        self._regions = []

        body = ttk.Frame(self, padding=12)
        body.grid(sticky="nsew")

        ttk.Label(body, text="지역").grid(row=0, column=0, sticky="w", **PAD)
        self.region_cb = ttk.Combobox(body, state="disabled", width=34)
        self.region_cb.grid(row=0, column=1, **PAD)
        self.region_cb.bind("<<ComboboxSelected>>", self._on_region)

        ttk.Label(body, text="지점").grid(row=1, column=0, sticky="w", **PAD)
        self.site_cb = ttk.Combobox(body, state="disabled", width=34)
        self.site_cb.grid(row=1, column=1, **PAD)
        self.site_cb.bind("<<ComboboxSelected>>", self._on_site)

        self.imax_lbl = ttk.Label(body, text="", foreground="#666")
        self.imax_lbl.grid(row=2, column=1, sticky="w", padx=8)

        ttk.Separator(body, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=8)

        ttk.Label(body, text="상영관").grid(row=4, column=0, sticky="w", **PAD)
        self.screen_cb = ttk.Combobox(body, width=34, state="readonly",
                                      values=list(SCREEN_CHOICES))
        self.screen_cb.grid(row=4, column=1, **PAD)
        self.screen_cb.set("아이맥스")

        ttk.Label(body, text="영화").grid(row=5, column=0, sticky="w", **PAD)
        self.movie_cb = ttk.Combobox(body, state="disabled", width=34)
        self.movie_cb.grid(row=5, column=1, **PAD)

        self.mode_var = tk.StringVar(value="목록에서 고르기")
        modes = ttk.Frame(body)
        modes.grid(row=6, column=1, sticky="w", padx=8)
        for i, text in enumerate(("목록에서 고르기", "직접 입력", "전체 영화")):
            ttk.Radiobutton(modes, text=text, value=text,
                            variable=self.mode_var,
                            command=self._toggle_mode).grid(row=0, column=i, padx=(0, 10))

        self.manual_entry = ttk.Entry(body, width=36, state="disabled")
        self.manual_entry.grid(row=7, column=1, **PAD)

        ttk.Label(
            body, wraplength=330, foreground="#666",
            text="'직접 입력'은 미개봉작용입니다. 제목 일부만 적어도 되고 "
                 "공백은 무시하고 부분일치로 찾습니다.",
        ).grid(row=8, column=1, sticky="w", padx=8)

        ttk.Separator(body, orient="horizontal").grid(
            row=9, column=0, columnspan=2, sticky="ew", pady=8)

        ttk.Label(body, text="알림 단위").grid(row=10, column=0, sticky="w", **PAD)
        self.unit_cb = ttk.Combobox(body, width=34, state="readonly",
                                    values=list(UNIT_CHOICES))
        self.unit_cb.grid(row=10, column=1, **PAD)
        self.unit_cb.set("새 회차마다")
        ttk.Label(
            body, wraplength=330, foreground="#666",
            text="'새 영화만'은 그 영화관에 새 영화가 걸릴 때 한 번만 알립니다. "
                 "전 상영관을 볼 때 알림이 쏟아지지 않게 해줍니다.",
        ).grid(row=11, column=1, sticky="w", padx=8)

        btns = ttk.Frame(body)
        btns.grid(row=12, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="취소", command=self.destroy).grid(row=0, column=0, padx=4)
        self.ok_btn = ttk.Button(btns, text="추가", command=self._accept, state="disabled")
        self.ok_btn.grid(row=0, column=1, padx=4)

        self._load_lists()

    # -- 로딩 ---------------------------------------------------------

    def _load_lists(self):
        self.region_cb.set("불러오는 중...")
        self.movie_cb.set("불러오는 중...")

        def work():
            return cgv_api.get_regions(), cgv_api.get_now_playing()

        def done(payload):
            regions, movies = payload
            self._regions = regions
            self.region_cb["values"] = [r["regnGrpNm"] for r in regions]
            self.region_cb.state(["!disabled"])
            self.region_cb.set("")

            self._movies = movies
            self.movie_cb["values"] = [m["movNm"] for m in movies]
            self.movie_cb.state(["!disabled"])
            self.movie_cb.set("")

        def fail(exc):
            messagebox.showerror("CGV 조회 실패", str(exc), parent=self)
            self.destroy()

        run_bg(work, done, fail)

    def _on_region(self, _evt=None):
        region = next(
            (r for r in self._regions if r["regnGrpNm"] == self.region_cb.get()), None)
        sites = (region or {}).get("siteList", [])
        self._sites = sites
        self.site_cb["values"] = [s["siteNm"] for s in sites]
        self.site_cb.state(["!disabled"])
        self.site_cb.set("")
        self.imax_lbl.config(text="")
        self.ok_btn.state(["disabled"])

    def _on_site(self, _evt=None):
        site = self._current_site()
        if not site:
            return
        self.imax_lbl.config(text="아이맥스 확인 중...", foreground="#666")
        self.ok_btn.state(["disabled"])

        def work():
            return cgv_api.has_imax(site["siteNo"])

        def done(payload):
            ok, cnt = payload
            if ok:
                self.imax_lbl.config(
                    text="아이맥스 있음 (현재 {}편 상영 중)".format(cnt),
                    foreground="#0a7d28")
                self.ok_btn.state(["!disabled"])
            else:
                self.imax_lbl.config(
                    text="이 지점에는 아이맥스가 없습니다", foreground="#c0392b")

        def fail(exc):
            self.imax_lbl.config(text="확인 실패: {}".format(exc), foreground="#c0392b")

        run_bg(work, done, fail)

    def _current_site(self):
        return next(
            (s for s in getattr(self, "_sites", []) if s["siteNm"] == self.site_cb.get()),
            None)

    def _toggle_mode(self):
        mode = self.mode_var.get()
        self.movie_cb.state(["!disabled"] if mode == "목록에서 고르기" else ["disabled"])
        self.manual_entry.state(["!disabled"] if mode == "직접 입력" else ["disabled"])

    def _accept(self):
        site = self._current_site()
        if not site:
            messagebox.showwarning("확인", "지점을 선택하세요.", parent=self)
            return

        mode = self.mode_var.get()
        if mode == "전체 영화":
            keyword = ""
        elif mode == "직접 입력":
            keyword = self.manual_entry.get().strip()
        else:
            keyword = self.movie_cb.get().strip()

        if mode != "전체 영화" and not keyword:
            messagebox.showwarning("확인", "영화를 고르거나 제목을 입력하세요.", parent=self)
            return

        unit = UNIT_CHOICES[self.unit_cb.get()]
        if not keyword and unit == "schedule":
            go = messagebox.askyesno(
                "알림이 많아질 수 있습니다",
                "영화를 지정하지 않고 '새 회차마다'로 두면 날짜가 열릴 때마다 "
                "수십 건이 한꺼번에 올 수 있습니다.\n\n"
                "'새 영화만'으로 바꿀까요?", parent=self)
            if go:
                unit = "movie"
                self.unit_cb.set(UNIT_LABELS["movie"])

        self.result = {
            "site_no": site["siteNo"],
            "site_nm": site["siteNm"],
            "screen": SCREEN_CHOICES[self.screen_cb.get()],
            "movie_keyword": keyword,
            "notify": unit,
        }
        self.destroy()


# ------------------------------------------------------------------ 메인 창

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        global _tk_root
        _tk_root = self

        self.title("CGV 아이맥스 오픈 알리미 - 설정")
        self.resizable(False, False)

        self.cfg = watcher.load_config()
        self.targets = list(self.cfg.get("targets", []))

        outer = ttk.Frame(self, padding=10)
        outer.grid(sticky="nsew")

        self._build_telegram(outer)
        self._build_targets(outer)
        self._build_options(outer)
        self._build_actions(outer)
        self._build_log(outer)

        self._load_credentials()
        self._refresh_tree()

    # -- 텔레그램 -----------------------------------------------------

    def _build_telegram(self, parent):
        box = ttk.LabelFrame(parent, text="텔레그램", padding=8)
        box.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ttk.Label(box, text="Bot Token").grid(row=0, column=0, sticky="w", **PAD)
        self.token_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.token_var, width=52).grid(
            row=0, column=1, columnspan=2, sticky="w", **PAD)

        ttk.Label(box, text="Chat ID").grid(row=1, column=0, sticky="w", **PAD)
        self.chat_var = tk.StringVar()
        ttk.Entry(box, textvariable=self.chat_var, width=24).grid(
            row=1, column=1, sticky="w", **PAD)

        btns = ttk.Frame(box)
        btns.grid(row=1, column=2, sticky="w")
        ttk.Button(btns, text="챗 ID 자동 찾기", command=self._find_chat_id).grid(
            row=0, column=0, padx=3)
        ttk.Button(btns, text="테스트 전송", command=self._test_send).grid(
            row=0, column=1, padx=3)

        ttk.Label(
            box, foreground="#666", wraplength=560,
            text="@BotFather 에게 /newbot 으로 봇을 만들고 토큰을 받으세요. "
                 "그 다음 만든 봇과의 대화창에서 아무 메시지나 한 번 보낸 뒤 "
                 "'챗 ID 자동 찾기'를 누르면 됩니다.",
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 0))

    def _load_credentials(self):
        token, chat = notifier.credentials()
        self.token_var.set(token)
        self.chat_var.set(chat)

    def _find_chat_id(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning("확인", "Bot Token 을 먼저 입력하세요.")
            return
        self._log("챗 ID 를 찾는 중...")

        def done(found):
            if not found:
                messagebox.showinfo(
                    "챗 ID 없음",
                    "최근 대화가 없습니다.\n텔레그램에서 봇에게 아무 메시지나 "
                    "한 번 보낸 뒤 다시 눌러주세요.")
                return
            cid, name = found[0]
            self.chat_var.set(cid)
            self._log("챗 ID 찾음: {} ({})".format(cid, name))
            if len(found) > 1:
                self._log("  다른 후보: " + ", ".join(
                    "{}({})".format(c, n) for c, n in found[1:]))

        run_bg(lambda: notifier.discover_chat_id(token), done,
               lambda e: messagebox.showerror("실패", str(e)))

    def _test_send(self):
        token, chat = self.token_var.get().strip(), self.chat_var.get().strip()
        if not token or not chat:
            messagebox.showwarning("확인", "토큰과 챗 ID 를 모두 입력하세요.")
            return
        self._log("테스트 메시지 전송 중...")

        def work():
            return notifier.send(
                "\U0001F514 <b>CGV 아이맥스 알리미</b>\n\n"
                "테스트 메시지입니다. 이 메시지가 보이면 설정이 정상입니다.",
                token=token, chat_id=chat)

        run_bg(work,
               lambda _r: self._log("테스트 전송 성공. 텔레그램을 확인하세요."),
               lambda e: messagebox.showerror("전송 실패", str(e)))

    # -- 감시 대상 ----------------------------------------------------

    def _build_targets(self, parent):
        box = ttk.LabelFrame(parent, text="감시 대상", padding=8)
        box.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        cols = ("site", "screen", "movie", "unit")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", height=6)
        for key, title, width in (
            ("site", "지점", 140), ("screen", "상영관", 150),
            ("movie", "영화", 210), ("unit", "알림 단위", 130),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="w")
        self.tree.grid(row=0, column=0, rowspan=2, **PAD)

        ttk.Button(box, text="추가", command=self._add_target, width=10).grid(
            row=0, column=1, sticky="n", padx=4, pady=4)
        ttk.Button(box, text="삭제", command=self._remove_target, width=10).grid(
            row=1, column=1, sticky="n", padx=4)

    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for t in self.targets:
            screen = watcher.screen_of(t)
            self.tree.insert("", "end", values=(
                t.get("site_nm", t["site_no"]),
                SCREEN_LABELS.get(screen, screen),
                t.get("movie_keyword") or "전체 영화",
                UNIT_LABELS.get(watcher.granularity_of(t), "새 회차마다"),
            ))

    def _add_target(self):
        dlg = AddTargetDialog(self)
        self.wait_window(dlg)
        if not dlg.result:
            return
        new = dlg.result
        dup = any(
            t["site_no"] == new["site_no"]
            and watcher.screen_of(t) == watcher.screen_of(new)
            and watcher.normalize(t.get("movie_keyword", ""))
            == watcher.normalize(new["movie_keyword"])
            for t in self.targets
        )
        if dup:
            messagebox.showinfo("중복", "이미 등록된 조합입니다.")
            return
        self.targets.append(new)
        self._refresh_tree()
        self._log("추가: " + watcher.describe(new))

    def _remove_target(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("확인", "삭제할 항목을 선택하세요.")
            return
        for item in sel:
            idx = self.tree.index(item)
            removed = self.targets.pop(idx)
            self._log("삭제: {} / {}".format(
                removed.get("site_nm"), removed["movie_keyword"]))
        self._refresh_tree()

    # -- 옵션 / 동작 --------------------------------------------------

    def _build_options(self, parent):
        box = ttk.Frame(parent)
        box.grid(row=2, column=0, sticky="ew", pady=(0, 8))

        self.first_run_var = tk.BooleanVar(
            value=not self.cfg.get("notify_on_first_run", False))
        ttk.Checkbutton(
            box, text="첫 실행 때는 알리지 않기 (기존 회차를 기준선으로만 저장)",
            variable=self.first_run_var,
        ).grid(row=0, column=0, sticky="w", **PAD)

        ttk.Label(box, text="전체 스캔 주기").grid(row=1, column=0, sticky="w", **PAD)
        self.scan_var = tk.IntVar(value=int(self.cfg.get("full_scan_every_runs", 30)))
        ttk.Spinbox(box, from_=1, to=200, textvariable=self.scan_var, width=6).grid(
            row=1, column=1, sticky="w")
        ttk.Label(box, foreground="#666",
                  text="회마다 (1분 주기면 30 = 30분마다 전체 확인)").grid(
            row=1, column=2, sticky="w")

    def _build_actions(self, parent):
        box = ttk.Frame(parent)
        box.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(box, text="설정 저장", command=self._save, width=14).grid(
            row=0, column=0, padx=4)
        ttk.Button(box, text="지금 1회 점검", command=self._check_now, width=14).grid(
            row=0, column=1, padx=4)
        ttk.Button(box, text="설치 명령어 복사", command=self._copy_setup, width=16).grid(
            row=0, column=2, padx=4)

    def _save(self):
        cfg = {
            "targets": self.targets,
            "full_scan_every_runs": int(self.scan_var.get()),
            "notify_on_first_run": not self.first_run_var.get(),
        }
        (ROOT / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        token, chat = self.token_var.get().strip(), self.chat_var.get().strip()
        if token and chat:
            notifier.save_credentials(token, chat)

        self.cfg = cfg
        self._log("설정을 저장했습니다. (config.json / .env)")
        messagebox.showinfo("저장 완료", "설정을 저장했습니다.")

    def _check_now(self):
        self._save()
        self._log("점검을 시작합니다 (알림은 보내지 않습니다)...")

        def work():
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "watcher.py"), "--dry-run"],
                cwd=str(ROOT), env=env, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
            )
            return (proc.stdout or "") + (proc.stderr or "")

        run_bg(work, self._log, lambda e: self._log("점검 실패: {}".format(e)))

    def _copy_setup(self):
        text = (
            "# 1) 리포지토리 생성 + Secrets 등록\n"
            "gh auth login\n"
            "git init && git add -A && git commit -m \"CGV IMAX 알리미\"\n"
            "gh repo create imax-alert --public --source=. --remote=origin --push\n"
            "gh secret set TELEGRAM_BOT_TOKEN\n"
            "gh secret set TELEGRAM_CHAT_ID\n"
            "\n"
            "# 2) Actions에서 403이 나는지 먼저 확인\n"
            "gh workflow run smoke-test.yml && gh run watch\n"
            "\n"
            "# 3) cron-job.org 에 등록할 값 (1분 주기)\n"
            "URL     : https://api.github.com/repos/<계정>/imax-alert/dispatches\n"
            "Method  : POST\n"
            "Header  : Authorization: Bearer <public_repo 스코프 PAT>\n"
            "Header  : Accept: application/vnd.github+json\n"
            "Header  : User-Agent: imax-alert\n"
            "Body    : {\"event_type\":\"poll\"}\n"
            "성공응답 : 204\n"
        )
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log("설치 명령어를 클립보드에 복사했습니다.\n\n" + text)

    # -- 로그 ---------------------------------------------------------

    def _build_log(self, parent):
        box = ttk.LabelFrame(parent, text="로그", padding=6)
        box.grid(row=4, column=0, sticky="nsew")
        self.log_text = tk.Text(box, height=14, width=84, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(box, orient="vertical", command=self.log_text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log_text.config(yscrollcommand=bar.set, state="disabled")

    def _log(self, msg):
        self.log_text.config(state="normal")
        self.log_text.insert("end", str(msg).rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")


if __name__ == "__main__":
    App().mainloop()
