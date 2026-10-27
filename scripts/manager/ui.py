from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .model import readable_size, signature
from .operations import DEFAULT_BACKUP_DIR, delete, list_backups, remove_backups
from .providers import scan
from .runtime import codex_cli, orca_projects, running_processes
from .workspaces import ai_homes, conversations_in, fixed_problem, key as path_key, plan_workspaces

CLOSE_HINT = {"Orca": "Orca 앱 전체 (안에서 실행 중인 Codex·Claude 터미널 포함)",
              "Codex": "Codex 앱과 터미널의 codex CLI",
              "Claude": "Claude 데스크톱 앱과 터미널의 Claude Code (데스크톱 앱도 같은 기록을 사용함)",
              "Gemini": "터미널의 gemini CLI"}


class App(tk.Tk):
    def __init__(self, roots=None, allow_delete=True):
        super().__init__()
        self.title("AI 기록 정리 · 프로젝트 / 대화 선택")
        self.geometry("1250x800")
        self.minsize(980, 620)
        self.roots, self.allow_delete = roots, allow_delete
        self.entries, self.checked, self.project_groups = [], set(), set()
        self.groups, self.rows, self.events = {}, {}, queue.Queue()
        self.busy = False
        self.running, self.processes, self.errors, self.orca = set(), [], [], []
        self.codex, self.backup_dir = "", DEFAULT_BACKUP_DIR
        self.workspaces, self.dirs, self.protected, self.pending = set(), {}, {}, []
        self.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(self)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure("Treeview", rowheight=29, font=("맑은 고딕", 10))
        style.configure("Treeview.Heading", font=("맑은 고딕", 10, "bold"))
        shell = ttk.Frame(self, padding=18)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="AI 프로젝트 · 대화기록 정리", font=("맑은 고딕", 19, "bold")).pack(anchor="w")
        ttk.Label(shell, text="체크한 AI 기록만 정리합니다. 작업 폴더(결과물)는 '작업 폴더' 열에서 따로 체크한 경우에만 휴지통으로 옮깁니다. 계정·설정·프로젝트 메모리는 보존합니다.").pack(anchor="w", pady=(6, 15))
        bar = ttk.Frame(shell)
        bar.pack(fill="x")
        self.query = tk.StringVar()
        search = ttk.Entry(bar, textvariable=self.query, width=45)
        search.pack(side="left", fill="x", expand=True)
        self.query.trace_add("write", lambda *_: self.render())
        ttk.Button(bar, text="새로고침", command=self.refresh).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="선택 해제", command=self.clear).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="실행 중 앱", command=self.show_processes).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="백업 관리", command=self.manage_backups).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="조회 오류", command=lambda: messagebox.showinfo("조회 오류", "\n".join(self.errors) or "없음", parent=self)).pack(side="left", padx=8)
        self.status = tk.StringVar(value="목록을 읽는 중…")
        ttk.Label(shell, textvariable=self.status).pack(anchor="w", pady=10)
        frame = ttk.Frame(shell)
        frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(frame, columns=("app", "count", "size", "updated", "state", "folder"), selectmode="browse")
        for key, label, width in [("#0", "선택 / 프로젝트 · 대화", 450), ("app", "앱", 75),
                                  ("count", "구분", 65), ("size", "기록 용량", 95),
                                  ("updated", "최근 변경", 150), ("state", "상태", 125), ("folder", "작업 폴더", 115)]:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=65, stretch=key == "#0")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("blocked", foreground="#777777")
        self.tree.bind("<ButtonRelease-1>", self.click)
        self.tree.bind("<space>", self.space)
        self.details = tk.StringVar(value="프로젝트 행을 체크하면 소속 대화를 함께 선택합니다. 펼침 화살표는 선택을 바꾸지 않습니다.")
        ttk.Label(shell, textvariable=self.details, wraplength=1180).pack(anchor="w", pady=10)
        bottom = ttk.Frame(shell)
        bottom.pack(fill="x")
        self.selection = tk.StringVar(value="선택 없음")
        ttk.Label(bottom, textvariable=self.selection, font=("맑은 고딕", 10, "bold")).pack(side="left")
        self.delete_button = ttk.Button(bottom, text="선택 항목 검토 → 삭제", command=self.review)
        self.delete_button.pack(side="right")
        ttk.Label(shell, text="Codex는 공식 삭제 명령과 DB 백업을 사용합니다. Claude·Gemini 파일은 Windows 휴지통으로 이동합니다.\n작업 폴더는 백업 없이 휴지통으로 이동합니다. 공간은 휴지통을 비우고 '백업 관리'에서 오래된 백업을 지운 뒤 확보됩니다.", foreground="#666666").pack(anchor="w", pady=(12, 0))
        self.after(100, self.poll)
        self.after(150, self.refresh)

    def close(self):
        if self.busy:
            messagebox.showinfo("처리 중", "백업·삭제 결과를 확인한 뒤 창을 닫으세요.", parent=self)
        else:
            self.destroy()

    def launch(self, function, event):
        if self.busy:
            return
        self.busy = True
        self.delete_button.state(["disabled"])
        def work():
            try:
                self.events.put((event, function(), None))
            except Exception as exc:
                self.events.put((event, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        if self.busy:
            return
        self.status.set("앱별 프로젝트·대화기록을 읽는 중…")
        def work():
            entries, errors = scan(self.roots)
            for e in entries:
                e.extra["display_size"] = e.size
            try:
                processes = running_processes()
                running = {p["app"] for p in processes}
            except Exception as exc:
                errors.append(str(exc))
                processes, running = [], {"Codex", "Claude", "Gemini", "Orca"}
            projects = []
            if self.roots is None:
                try:
                    projects = orca_projects()
                except Exception as exc:
                    errors.append(f"Orca 프로젝트: {exc}")
            try:
                path, version = codex_cli()
                codex = f"{version} ({path})"
            except Exception as exc:
                codex = f"찾지 못함: {exc}"
            dirs, protected, homes = {}, {}, ai_homes(entries)
            for e in entries:
                if e.kind == "conversation" and Path(e.project).is_absolute() and path_key(e.project) not in dirs:
                    if Path(e.project).is_dir():
                        dirs[path_key(e.project)] = e.project
                        reason = fixed_problem(e.project, homes)
                        if reason:
                            protected[path_key(e.project)] = reason
            return entries, errors, running, processes, projects, codex, dirs, protected
        self.launch(work, "scan")

    def poll(self):
        try:
            while True:
                event, result, error = self.events.get_nowait()
                self.busy = False
                if self.allow_delete:
                    self.delete_button.state(["!disabled"])
                if error:
                    self.status.set("실패: " + error)
                    messagebox.showerror("작업 중단", error, parent=self)
                elif event == "plan":
                    self.status.set("작업 폴더 확인 완료")
                    self.open_review(self.pending, result)
                elif event == "scan":
                    self.entries, self.errors, self.running, self.processes, self.orca, self.codex, self.dirs, self.protected = result
                    self.checked.clear()
                    self.project_groups.clear()
                    self.workspaces.clear()
                    self.render()
                    text = f"기록 {len(self.entries):,}개 · 읽기 오류 {len(self.errors)}개 · "
                    if self.running:
                        text += f"실행 중: {', '.join(sorted(self.running))} → 이 앱들의 기록은 삭제할 수 없습니다. '실행 중 앱'에서 닫을 프로그램을 확인하세요."
                    else:
                        text += "실행 중인 AI 앱 없음 · 모든 기록 선택 가능"
                    self.status.set(text)
                else:
                    archive, good, failed, notes = result
                    text = f"처리 성공 {len(good)}개 / 오류 {len(failed)}개\n백업: {archive}\n"
                    text += "".join(f"\n· {n}" for n in notes)
                    text += "".join(f"\n✕ {name}: {reason}" for name, reason in failed[:12])
                    messagebox.showinfo("정리 결과", text, parent=self)
                    self.refresh()
        except queue.Empty:
            pass
        self.after(150, self.poll)

    def workspace_of(self, group, members):
        if all(e.kind == "cache" for e in members):
            return None
        k = path_key(group[2]) if Path(group[2]).is_absolute() else None
        return k if k in self.dirs else None

    def is_running(self, e):
        return e.provider in self.running or ("orca" in str(e.home).lower() and "Orca" in self.running)

    def render(self):
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        self.groups, self.rows = defaultdict(list), {}
        query = self.query.get().strip().casefold()
        for e in self.entries:
            self.groups[e.group].append(e)
        # Most recently used projects first; shared caches after all conversations.
        order = sorted(self.groups.items(), key=lambda g: (all(e.kind == "cache" for e in g[1]),
                                                            -max(e.updated for e in g[1])))
        for i, (group, members) in enumerate(order):
            if query and not any(query in f"{e.provider} {e.title} {e.project} {e.home}".casefold() for e in members):
                continue
            n = sum(e.key in self.checked for e in members)
            mark = "☑" if n == len(members) else ("◩" if n else "☐")
            parent = f"p{i}"
            label = Path(group[2]).name or group[2]
            latest = max(e.updated for e in members)
            ws = self.workspace_of(group, members)
            if not ws:
                folder = ""
            elif ws in self.protected:
                folder = "보호됨"
            else:
                folder = "☑ 함께 삭제" if ws in self.workspaces else "☐ 함께 삭제"
            self.tree.insert("", "end", iid=parent, text=f"{mark}  {label}", open=bool(query),
                             values=(group[0], f"{len(members)}개", readable_size(sum(e.extra.get("display_size", 0) for e in members)),
                                     datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M") if latest else "", "프로젝트 묶음", folder))
            self.rows[parent] = ("project", group)
            for e in sorted(members, key=lambda x: x.updated, reverse=True):
                locked = e.blocked or ("앱 종료 후 삭제" if self.is_running(e) else "선택 가능")
                date = datetime.fromtimestamp(e.updated).strftime("%Y-%m-%d %H:%M") if e.updated else ""
                title = " ".join(e.title.split())[:160]
                self.tree.insert(parent, "end", iid=e.key, text=f"{'☑' if e.key in self.checked else '☐'}  {title}",
                                 values=(e.provider, "캐시" if e.kind == "cache" else "대화",
                                         readable_size(e.extra.get("display_size", 0)) if e.files else "DB 항목만", date, locked),
                                 tags=("blocked",) if locked != "선택 가능" else ())
                self.rows[e.key] = ("entry", e)
        if self.orca:
            parent = self.tree.insert("", "end", text="Orca 프로젝트 등록 (조회 전용)", values=("Orca", "등록", "", "", "앱에서 등록 해제"))
            for p in self.orca:
                self.tree.insert(parent, "end", text=p["name"], values=("Orca", "프로젝트", "", "", "조회 전용"), tags=("blocked",))
        total = sum(e.extra.get("display_size", 0) for e in self.entries if e.key in self.checked)
        text = f"선택 {len(self.checked)}개 · 기록 파일 {readable_size(total)} (DB 공간 별도)"
        if self.workspaces:
            text += f" · 작업 폴더 {len(self.workspaces)}개 함께 삭제 (용량은 검토 단계에서 계산)"
        self.selection.set(text)

    def clear(self):
        self.checked.clear()
        self.project_groups.clear()
        self.workspaces.clear()
        self.render()

    def render_keep_open(self):
        expanded = {p for p in self.tree.get_children() if self.tree.item(p, "open")}
        self.render()
        for p in expanded:
            if self.tree.exists(p):
                self.tree.item(p, open=True)

    def click(self, event):
        row = self.tree.identify_row(event.y)
        if not row or row not in self.rows or self.busy:
            return
        kind, value = self.rows[row]
        if kind == "entry":
            self.details.set(f"작업 폴더: {value.project}\nAI 저장소: {value.home} | ID: {value.session}")
        else:
            self.details.set(f"작업 폴더: {value[2]}\nAI 저장소: {value[1]}")
            if self.tree.identify_column(event.x) == "#6":
                self.toggle_workspace(value)
                return
        # Only the leading checkbox is actionable. Row text is for selection/details.
        if self.tree.identify_column(event.x) == "#0" and 18 <= event.x <= (85 if kind == "entry" else 55):
            self.toggle(row)

    def space(self, _):
        rows = self.tree.selection()
        if rows:
            self.toggle(rows[0])
        return "break"

    def toggle(self, row):
        if self.busy or row not in self.rows:
            return
        expanded = {p for p in self.tree.get_children() if self.tree.item(p, "open")}
        kind, value = self.rows[row]
        # Blocked records can never be checked, so they must not decide the check state either.
        members = [e for e in (self.groups[value] if kind == "project" else [value]) if not e.blocked]
        if not members:
            return
        remove = all(e.key in self.checked for e in members)
        for e in members:
            self.checked.discard(e.key) if remove else self.checked.add(e.key)
        if kind == "project":
            self.project_groups.discard(value) if remove else self.project_groups.add(value)
        else:
            self.project_groups.discard(value.group)
        # A work folder stays marked only while every chat that worked in it is checked.
        for k in list(self.workspaces):
            if any(e.key not in self.checked for e in conversations_in(self.dirs[k], self.entries) if not e.blocked):
                self.workspaces.discard(k)
        self.render()
        for p in expanded:
            if self.tree.exists(p):
                self.tree.item(p, open=True)

    def toggle_workspace(self, group):
        if self.busy:
            return
        k = self.workspace_of(group, self.groups[group])
        if not k:
            return
        path = self.dirs[k]
        if k in self.protected:
            self.details.set(f"작업 폴더 보호됨: {path}\n이유: {self.protected[k]}. 이 폴더는 프로그램에서 삭제할 수 없습니다.")
            return
        if k in self.workspaces:
            self.workspaces.discard(k)
            self.details.set(f"작업 폴더 보존: {path}")
        else:
            members = conversations_in(path, self.entries)
            for e in members:
                if not e.blocked:
                    self.checked.add(e.key)
            for g, ms in self.groups.items():
                if all(m.key in self.checked for m in ms):
                    self.project_groups.add(g)
            self.workspaces.add(k)
            locked = sum(bool(e.blocked) for e in members)
            self.details.set(f"작업 폴더를 함께 삭제합니다: {path}\n이 폴더(하위 폴더 포함)에서 작업한 대화 {len(members)}개를 앱 구분 없이 모두 선택했습니다."
                             + (f" 선택할 수 없는 대화 {locked}개가 있어 검토 단계에서 폴더가 제외됩니다." if locked else ""))
        self.render_keep_open()

    def show_processes(self):
        if not self.processes:
            text = "실행 중인 Codex·Claude·Gemini·Orca 프로세스가 없습니다." if not self.running else "실행 상태를 확인하지 못해 모든 앱을 실행 중으로 간주합니다."
        else:
            by_app = defaultdict(list)
            for p in self.processes:
                by_app[p["app"]].append(p)
            text = "아래 프로그램을 모두 닫은 뒤 '새로고침'을 누르세요.\nOrca 터미널 안에서 이 프로그램을 띄웠다면 Orca를 닫고 탐색기에서 실행.cmd를 다시 실행하세요.\n"
            for app in sorted(by_app):
                text += f"\n[{app}] {CLOSE_HINT.get(app, '')}\n"
                for p in by_app[app][:8]:
                    text += f"  · {p['name']} (PID {p['pid']}) {p.get('path') or ''}\n"
                if len(by_app[app]) > 8:
                    text += f"  · 외 {len(by_app[app]) - 8}개\n"
        messagebox.showinfo("실행 중 앱", text, parent=self)

    def manage_backups(self):
        window = tk.Toplevel(self)
        window.title("백업 관리")
        window.geometry("760x460")
        window.transient(self)
        folder = tk.StringVar(value=str(self.backup_dir))
        summary = tk.StringVar()
        ttk.Label(window, textvariable=folder, padding=(15, 12, 15, 4)).pack(anchor="w")
        ttk.Label(window, textvariable=summary, padding=(15, 0, 15, 8), foreground="#666666").pack(anchor="w")
        frame = ttk.Frame(window, padding=(15, 0))
        frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=("size", "date"), selectmode="extended")
        for key, label, width in [("#0", "백업 파일", 420), ("size", "크기", 110), ("date", "만든 시각", 160)]:
            tree.heading(key, text=label)
            tree.column(key, width=width, stretch=key == "#0")
        tree.pack(side="left", fill="both", expand=True)
        def load():
            tree.delete(*tree.get_children())
            rows = list_backups(Path(folder.get()))
            for p, size, mtime in rows:
                tree.insert("", "end", iid=str(p), text=p.name,
                            values=(readable_size(size), datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")))
            summary.set(f"백업 {len(rows)}개 · 합계 {readable_size(sum(r[1] for r in rows))}. 선택한 백업은 휴지통을 거치지 않고 바로 삭제됩니다.")
        def remove():
            if not self.allow_delete:
                return
            chosen = tree.selection()
            if not chosen:
                messagebox.showinfo("선택 필요", "삭제할 백업을 선택하세요. Ctrl·Shift로 여러 개를 고를 수 있습니다.", parent=window)
                return
            if not messagebox.askyesno("백업 영구 삭제", f"백업 {len(chosen)}개를 영구 삭제합니다.\n이 백업에 든 기록은 더 이상 복구할 수 없습니다. 계속할까요?", parent=window):
                return
            try:
                remove_backups(chosen, Path(folder.get()))
            except Exception as exc:
                messagebox.showerror("삭제 실패", str(exc), parent=window)
            load()
        def choose():
            picked = filedialog.askdirectory(parent=window, initialdir=folder.get())
            if picked:
                folder.set(picked)
                self.backup_dir = Path(picked)
                load()
        bar = ttk.Frame(window, padding=15)
        bar.pack(fill="x")
        ttk.Button(bar, text="다른 폴더", command=choose).pack(side="left")
        ttk.Button(bar, text="폴더 열기", command=lambda: os.startfile(folder.get()) if Path(folder.get()).is_dir() else None).pack(side="left", padx=8)
        ttk.Button(bar, text="닫기", command=window.destroy).pack(side="right")
        remove_button = ttk.Button(bar, text="선택 백업 영구 삭제", command=remove)
        remove_button.pack(side="right", padx=8)
        if not self.allow_delete:
            remove_button.state(["disabled"])
        load()
        window.grab_set()

    def review(self):
        if self.busy or not self.allow_delete:
            return
        selected = [e for e in self.entries if e.key in self.checked]
        if not selected:
            messagebox.showinfo("선택 필요", "정리할 프로젝트 또는 대화를 체크하세요.", parent=self)
            return
        if not self.workspaces:
            self.open_review(selected, [])
            return
        paths = [self.dirs[k] for k in self.workspaces]
        keys, entries, orca = set(self.checked), list(self.entries), [p.get("path", "") for p in self.orca]
        self.pending = selected
        self.status.set("작업 폴더 용량과 git 상태를 확인하는 중…")
        self.launch(lambda: plan_workspaces(paths, entries, keys, orca), "plan")

    def open_review(self, selected, plan):
        window = tk.Toplevel(self)
        window.title("삭제 대상 최종 확인")
        window.geometry("880x700" if plan else "850x620")
        window.transient(self)
        ttk.Label(window, text="선택한 AI 기록을 정리합니다." + (" 표시한 작업 폴더도 휴지통으로 옮깁니다." if plan else " 작업 폴더(결과물)는 보존됩니다."), padding=15).pack(anchor="w")
        text = tk.Text(window, wrap="word", height=16, padx=12, pady=8)
        text.pack(fill="both", expand=True, padx=15)
        text.tag_configure("head", font=("맑은 고딕", 10, "bold"))
        text.tag_configure("bad", foreground="#b3261e")
        text.tag_configure("warn", foreground="#8a5a00")
        eligible = [item for item in plan if not item["problem"]]
        if plan:
            text.insert("end", "■ 함께 삭제할 작업 폴더 (휴지통으로 이동, 프로그램이 따로 백업하지 않음)\n", "head")
            for item in plan:
                if item["problem"]:
                    text.insert("end", f"  ✕ {item['path']}\n", "bad")
                    text.insert("end", f"     삭제하지 않음: {item['problem']}\n", "bad")
                else:
                    text.insert("end", f"  • {item['path']}\n     {readable_size(item['size'])} · 파일 {item['files']:,}개\n")
                    for w in item["warnings"]:
                        text.insert("end", f"     ⚠ {w}\n", "warn")
            text.insert("end", "\n■ 정리할 AI 기록\n", "head")
        for e in selected:
            text.insert("end", f"[{e.provider}] {e.title[:120]}\n  ID: {e.session}\n  저장소: {e.home}\n")
        text.insert("end", "\nCodex: 공식 명령으로 영구 삭제 + 대화 원문·DB 스냅샷 백업. 프로젝트 등록은 보존합니다.")
        if any(e.provider == "Codex" for e in selected):
            text.insert("end", f"\n  사용할 Codex CLI: {self.codex}")
        text.insert("end", "\nClaude·Gemini: 백업 후 Windows 휴지통으로 이동. Claude 프로젝트 메모리(memory)는 보존합니다.\n앱 실행 중에는 삭제를 차단합니다.")
        text.configure(state="disabled")
        destination = tk.StringVar(value=str(self.backup_dir))
        bar = ttk.Frame(window, padding=(15, 15, 15, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="삭제 전 백업 위치:").pack(side="left")
        ttk.Entry(bar, textvariable=destination).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(bar, text="변경", command=lambda: destination.set(filedialog.askdirectory(parent=window) or destination.get())).pack(side="left")
        existing = list_backups(Path(destination.get()))
        ttk.Label(window, padding=(15, 0), foreground="#666666",
                  text=f"이 위치의 기존 백업 {len(existing)}개 · {readable_size(sum(r[1] for r in existing))} ('백업 관리'에서 정리)").pack(anchor="w")
        caches = [e for e in selected if e.kind == "cache"]
        with_caches = tk.BooleanVar(value=False)
        if caches:
            size = readable_size(sum(e.extra.get("display_size", 0) for e in caches))
            ttk.Checkbutton(window, variable=with_caches,
                            text=f"캐시·로그도 백업 ({size}). 앱이 다시 만드는 파일이라 보통은 필요 없습니다.").pack(anchor="w", padx=15, pady=(8, 0))
        agreed = tk.BooleanVar()
        ttk.Checkbutton(window, text="선택 작업이 종료되었고, 필요한 결과물은 보관했거나 필요 없음을 확인했습니다.", variable=agreed).pack(anchor="w", padx=15, pady=(8, 0))
        moved = tk.BooleanVar()
        if eligible:
            total = readable_size(sum(i["size"] for i in eligible))
            ttk.Checkbutton(window, variable=moved,
                            text=f"작업 폴더 {len(eligible)}개({total})의 결과물을 실제 사용할 폴더로 옮겼거나 필요 없음을 확인했습니다.").pack(anchor="w", padx=15, pady=(8, 0))
        def apply():
            if not agreed.get() or not destination.get().strip():
                messagebox.showinfo("확인 필요", "작업 종료·결과물 보관 여부와 백업 위치를 확인하세요.", parent=window)
                return
            if eligible and not moved.get():
                messagebox.showinfo("확인 필요", "작업 폴더의 결과물을 옮겼는지 확인하세요.", parent=window)
                return
            try:
                expected = {e.key: signature(e) for e in selected}
            except Exception as exc:
                messagebox.showerror("검사 실패", str(exc), parent=window)
                return
            groups = set(self.project_groups)
            dest = Path(destination.get()).expanduser()
            self.backup_dir = dest
            include = with_caches.get()
            window.destroy()
            self.status.set("백업 검증 후 선택한 기록을 정리하는 중…")
            folders = [i["path"] for i in eligible]
            everything = list(self.entries)
            self.launch(lambda: delete(selected, expected, dest, groups, include_caches=include,
                                       workspaces=folders, all_entries=everything), "delete")
        ttk.Button(window, text="백업 후 선택 기록 삭제", command=apply).pack(anchor="e", padx=15, pady=15)
        window.grab_set()
