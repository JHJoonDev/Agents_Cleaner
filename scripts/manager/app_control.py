"""Close AI apps when the user asks. Nothing here runs automatically or decides what may be deleted."""
from __future__ import annotations

import ctypes
import os
import time
from collections import Counter
from ctypes import wintypes
from dataclasses import dataclass, field

from . import runtime
from .model import readable_size

GRACE_SECONDS = 10
WM_CLOSE = 0x0010
GW_OWNER = 4


@dataclass(frozen=True)
class Target:
    pid: int
    created: float
    name: str


@dataclass
class AppGroup:
    name: str
    rows: list = field(default_factory=list)
    targets: list = field(default_factory=list)   # The app's own processes.
    children: list = field(default_factory=list)  # Whatever they started (shells, dev servers...).
    memory: int = 0
    windows: int = 0
    agents: int = 0
    busy: int = 0
    holds_self: bool = False

    @property
    def stoppable(self):
        return bool(self.targets) and not self.holds_self

    def describe(self):
        parts = [f"창 {self.windows}개" if self.windows else "창 없음(터미널·백그라운드)"]
        if self.agents:
            parts.append(f"에이전트 {self.agents}개" + (f"(작업 중 {self.busy}개)" if self.busy else ""))
        if self.children:
            parts.append(f"하위 프로세스 {len(self.children)}개")
        if self.holds_self:
            parts.append("이 프로그램 포함")
        return " · ".join(parts)


def group_name(row):
    if row["app"] == "Claude":
        return "Claude 데스크톱" if runtime.is_claude_desktop(row) else "Claude Code"
    return row["app"]


def visible_windows():
    """(hwnd, pid) of the windows a user could close: visible, titled, not owned by another window."""
    if os.name != "nt":
        return []
    user32 = ctypes.WinDLL("user32")
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindow.argtypes, user32.GetWindow.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    found = []
    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER) and user32.GetWindowTextLengthW(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found.append((hwnd, pid.value))
        return True
    user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
    user32.EnumWindows(visit, 0)
    return found


def close_window(hwnd):
    user32 = ctypes.WinDLL("user32")
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))


def protected_pids():
    """This program and what started it. Stopping one of these would stop this program mid-work."""
    if runtime.psutil is None:
        return {os.getpid()}
    me = runtime.psutil.Process()
    return {me.pid, *(p.pid for p in me.parents())}


def _target(process):
    return Target(process.pid, process.create_time(), process.name())


def _note_agent(group, seen, pid, app, path):
    """Count what stopping this group would interrupt, including sessions an app hosts."""
    if pid in seen or app not in {"Codex", "Claude", "Gemini"} or runtime.is_claude_desktop({"path": path}):
        return
    seen.add(pid)
    group.agents += 1
    if app == "Claude":
        group.busy += (runtime.claude_session(pid) or {}).get("status") == "busy"


def snapshot(processes):
    """Group detected processes by app. A process that cannot be identified is shown but never targeted."""
    psutil, groups, agents = runtime.psutil, {}, {}
    protected, windows = protected_pids(), Counter(pid for _, pid in visible_windows())
    for row in processes:
        name = group_name(row)
        group = groups.setdefault(name, AppGroup(name))
        seen = agents.setdefault(name, set())
        group.rows.append(row)
        group.windows += windows[row["pid"]]
        group.holds_self = group.holds_self or row["pid"] in protected
        _note_agent(group, seen, row["pid"], row["app"], row.get("path", ""))
        if psutil is None:
            continue
        try:
            process = psutil.Process(row["pid"])
            group.targets.append(_target(process))
            group.memory += process.memory_info().rss
            children = process.children(recursive=True)
        except (psutil.Error, OSError):
            continue
        for child in children:
            group.holds_self = group.holds_self or child.pid in protected
            try:
                target = _target(child)
            except (psutil.Error, OSError):
                continue
            group.children.append(target)
            try:
                path = child.exe()
            except (psutil.Error, OSError):
                path = ""
            _note_agent(group, seen, child.pid, runtime._process_app(target.name), path)
    for group in groups.values():
        # A child can also be one of the app's own processes, or appear under several parents.
        group.children = [t for t in dict.fromkeys(group.children) if t not in group.targets]
    return sorted(groups.values(), key=lambda g: g.name.casefold())


def _alive(target):
    """The same process, not a new one that reused its PID."""
    psutil = runtime.psutil
    try:
        process = psutil.Process(target.pid)
        return process if process.create_time() == target.created and process.is_running() else None
    except (psutil.Error, OSError):
        return None


def remaining(targets):
    protected = protected_pids()
    return [t for t in targets if t.pid not in protected and _alive(t)]


def request_close(groups, grace=GRACE_SECONDS):
    """Ask nicely: close windows, stop the Herdr server, wait. Returns (still running, notes)."""
    if any(g.holds_self for g in groups):
        raise RuntimeError("이 프로그램이 해당 앱 안에서 실행 중이라 종료할 수 없습니다.")
    targets = list(dict.fromkeys(t for g in groups for t in (*g.targets, *g.children)))
    own = {t.pid: t for g in groups for t in g.targets}
    asked, notes = False, []
    for hwnd, pid in visible_windows():
        if pid in own and _alive(own[pid]) and close_window(hwnd):
            asked = True
    if any(g.name == "Herdr" for g in groups):
        try:
            runtime.command("hc", ["server", "stop"], timeout=15)
            asked = True
        except Exception as exc:
            notes.append(f"hc server stop 실패: {str(exc) or type(exc).__name__}")
    # A terminal program has no window of its own to ask, so waiting would only delay the next step.
    deadline = time.monotonic() + (grace if asked else 0)
    while True:
        left = remaining(targets)
        if not left or time.monotonic() >= deadline:
            return left, notes
        time.sleep(.2)


def force_stop(targets):
    """Terminate these processes and anything they started since. Returns (still running, notes)."""
    psutil = runtime.psutil
    protected, victims, notes = protected_pids(), {}, []
    for target in targets:
        process = None if target.pid in protected else _alive(target)
        if process is None:
            continue
        try:
            for child in process.children(recursive=True):
                if child.pid not in protected:
                    victims.setdefault(child.pid, child)
        except (psutil.Error, OSError):
            pass
        victims.setdefault(process.pid, process)
    for process in victims.values():
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except (psutil.Error, OSError) as exc:
            notes.append(f"PID {process.pid} 종료 실패: {type(exc).__name__}")
    if victims:
        psutil.wait_procs(list(victims.values()), timeout=3)
    return remaining(targets), notes


def stop_summary(groups):
    lines = [f"· {g.name}: 프로세스 {len(g.targets)}개, {g.describe()}, 메모리 {readable_size(g.memory)}" for g in groups]
    text = "다음 앱을 종료합니다.\n\n" + "\n".join(lines) + "\n\n창을 닫아 정상 종료를 요청합니다. 그래도 남는 프로세스는 다시 확인한 뒤 강제 종료합니다."
    if any(g.agents for g in groups):
        text += "\n\n실행 중인 에이전트의 작업이 끊길 수 있습니다."
    return text


def leftovers_summary(targets, notes=()):
    names = Counter(t.name for t in targets)
    listed = ", ".join(f"{name} {count}개" for name, count in names.most_common())
    extra = "".join(f"\n{note}" for note in notes)
    return (f"정상 종료 후에도 프로세스 {len(targets)}개가 남아 있습니다.\n\n{listed}\n{extra}\n"
            "강제 종료하면 저장하지 않은 내용과 진행 중인 작업이 사라질 수 있습니다.")
