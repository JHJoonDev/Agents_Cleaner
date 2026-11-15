from __future__ import annotations
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence, TypeVar, ParamSpec, cast, TYPE_CHECKING

import functools
import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

try:
    import psutil as psutil
except ImportError:
    psutil = None  # type: ignore[assignment]  # optional dependency  # type: ignore[assignment]  # optional dependency

if TYPE_CHECKING:
    from .model import Entry

PROVIDERS = {"Codex", "Claude", "Gemini", "Orca", "Herdr", "Artifacts"}

ARTIFACT_TIERS: dict[int | None,str] = {1: "1 바로 정리", 2: "2 확인 후 정리", 3: "3 개수 제한"}


def entry_group(entry: Entry) -> Any:
    if entry.kind == "artifact":
        return (entry.provider, str(entry.home),
                "작업 부산물 · " + ARTIFACT_TIERS.get(entry.extra.get("tier"), "단계 미확인"))
    return entry.group


def entry_kind(entry: Entry) -> Any:
    return {"artifact": "부산물", "cache": "캐시"}.get(
        entry.kind, "터미널" if entry.provider == "Herdr" else "대화")


def artifact_details(entry: Entry) -> Any:
    from .model import Entry, readable_size
    method = "영구 삭제" if entry.extra.get("method") == "delete" else "휴지통"
    return (f"경로: {', '.join(str(p) for p in entry.files)}\n"
            f"용량: {readable_size(entry.extra.get('display_size', 0))} · "
            f"단계: {ARTIFACT_TIERS.get(entry.extra.get('tier'), '미확인')} · 방법: {method}\n"
            f"미사용: {entry.extra.get('idle_days', 0)}일 · "
            f"재설치: {entry.extra.get('reinstall') or '해당 없음'}\n"
            f"차단 사유: {entry.blocked or '없음'}")


def cleanup_summary(selected: Any, plan: Any=()) -> Any:
    """The same deletion/recycle breakdown in Qt and the Tk fallback."""
    from .model import Entry, readable_size
    permanent = [e for e in selected if
                 (e.kind == "artifact" and e.extra.get("method") == "delete")
                 or (e.provider == "Codex" and e.kind == "conversation")]
    recycled = [e for e in selected if e not in permanent]
    folders = [p for p in plan if not p["problem"]]
    size: Callable[[list[Entry]],int] = lambda rows: sum(e.extra.get("display_size", 0) for e in rows)
    return (f"영구 삭제: {len(permanent)}개 · {readable_size(size(permanent))}\n"
            f"휴지통 이동: {len(recycled) + len(folders)}개 · "
            f"{readable_size(size(recycled) + sum(p['size'] for p in folders))}")


def claude_homes(user_home: Path | None=None) -> list[Path]:
    """Plain Claude homes, including launcher accounts; never follow links/junctions."""
    from .model import is_link
    user = Path(user_home or Path.home())
    candidates = [user / ".claude"]
    candidates.extend(p for p in user.glob(".claude-*")
                      if not is_link(p) and p.is_dir()
                      and not is_link(p / "projects") and (p / "projects").is_dir())
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.append(Path(os.environ["CLAUDE_CONFIG_DIR"]))
    found: list[Any]
    seen: set[Any]
    found, seen = [], set()
    for p in candidates:
        if not p.is_dir() or is_link(p) or any(is_link(a) for a in p.absolute().parents):
            continue
        resolved = p.resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            found.append(resolved)
    return found


def _orca_may_use(home: Path) -> Any:
    # ~/.codex/accounts homes have no "orca" in their path but can be Orca-managed.
    parts = [part.casefold() for part in Path(home).parts]
    return "orca" in str(home).lower() or parts[-3:-1] == [".codex", "accounts"]


class LiveApps(frozenset[str]):
    claude_all: bool
    claude_sessions: frozenset[str]
    """Running apps plus which Claude conversations they hold. Compares like a set of app names."""
    def __new__(cls, apps: Iterable[str]=(), claude_all: bool=False, claude_sessions: Iterable[str]=()) -> LiveApps:
        self = super().__new__(cls, apps)
        self.claude_all, self.claude_sessions = claude_all, frozenset(claude_sessions)
        return self

    def holds(self, entry: Entry) -> bool:
        if getattr(entry, "kind", "conversation") == "artifact":
            return False
        if "Orca" in self and _orca_may_use(entry.home):
            return True
        if entry.provider not in self:
            return False
        if entry.provider != "Claude" or entry.kind != "conversation":
            return True
        return self.claude_all or entry.session in self.claude_sessions


def entry_running(entry: Entry, live: Iterable[str]) -> bool:
    if getattr(entry, "kind", "conversation") == "artifact":
        return False
    if isinstance(live, LiveApps):
        return live.holds(entry)
    # A plain set has no process detail (detection failed, or a caller that only knows names).
    return (entry.provider in live or ("Orca" in live and _orca_may_use(entry.home))
            or ("Herdr" in live and entry.provider in {"Codex", "Claude", "Gemini"}))


def lock_key(live: Iterable[str]) -> Any:
    """What decides the locks, for spotting a change: LiveApps equality only compares app names."""
    return (frozenset(live), getattr(live, "claude_all", None), getattr(live, "claude_sessions", None))


def workspace_blocker(live: set[str] | frozenset[str]) -> str:
    # Source folders stay stricter than records: an idle terminal can still be sitting in one.
    if live & {"Orca", "Herdr"}:
        return "Orca 또는 Herdr가 실행 중이어서 작업 폴더를 보존함"
    if isinstance(live, LiveApps) and (live.claude_all or live.claude_sessions):
        return "Claude Code 세션이 실행 중이어서 작업 폴더를 보존함"
    return ""


def is_claude_desktop(row: dict[str,Any]) -> bool:
    """The desktop shell, not the Claude Code sessions it hosts (those are separate binaries)."""
    path = str(row.get("path") or "").replace("/", "\\").casefold()
    return ("\\windowsapps\\claude_" in path or "\\anthropicclaude\\" in path) and "claude-code" not in path


def claude_session(pid: int) -> dict[str,Any] | None:
    """Find this PID across Claude homes; unknown or conflicting ownership fails closed."""
    try:
        from .model import is_link
        if psutil is None:
            return None
        started = psutil.Process(pid).create_time()
        found = None
        for home in claude_homes():
            path = home / "sessions" / f"{pid}.json"
            if not path.exists():
                continue
            if is_link(home / "sessions") or is_link(path):
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("pid") != pid:
                return None
            sid = data.get("sessionId")
            if not isinstance(sid, str) or not sid:
                return None
            # An older file belongs to a previous process that had this PID.
            if path.stat().st_mtime < started - 5:
                continue
            if found is not None and found["sessionId"] != sid:
                return None
            found = data
        return found
    except Exception:
        return None


def lock_state(processes: Any) -> LiveApps:
    """Fail closed: one Claude CLI we cannot tie to a conversation locks all Claude conversations.

    Search every Claude home: CLAUDE_CONFIG_DIR may point at an Orca profile while
    its conversation is in ~/.claude/projects or a launcher account home.
    """
    sessions: set[Any]
    sessions, unknown = set(), False
    for row in processes:
        if row["app"] != "Claude" or is_claude_desktop(row):
            continue
        data = claude_session(row["pid"])
        if data is None:
            unknown = True
        else:
            sessions.add(data["sessionId"])
    return LiveApps({row["app"] for row in processes}, unknown, sessions)


_discovery_lock = threading.Lock()
_orca_lock = threading.Lock()
_orca_cache = None

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class _JobLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", ctypes.c_uint32), ("min_working_set", ctypes.c_size_t),
                ("max_working_set", ctypes.c_size_t), ("active_limit", ctypes.c_uint32),
                ("affinity", ctypes.c_size_t), ("priority", ctypes.c_uint32),
                ("scheduling", ctypes.c_uint32)]


class _JobExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _JobLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class _JobAccounting(ctypes.Structure):
    _fields_ = [("times", ctypes.c_int64 * 4), ("faults", ctypes.c_uint32),
                ("total", ctypes.c_uint32), ("active", ctypes.c_uint32), ("terminated", ctypes.c_uint32)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("usage", ctypes.c_uint32),
                ("tid", ctypes.c_uint32), ("pid", ctypes.c_uint32),
                ("priority", ctypes.c_long), ("delta", ctypes.c_long), ("flags", ctypes.c_uint32)]


class _WindowsJob:
    """Own only this invocation's suspended child and its descendants."""
    handle: Any
    kernel: Any
    def __init__(self) -> None:
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer, uint = ctypes.c_void_p, ctypes.c_uint32
        signatures = {
            "CreateJobObjectW": ([pointer, ctypes.c_wchar_p], pointer),
            "SetInformationJobObject": ([pointer, ctypes.c_int, pointer, uint], ctypes.c_int),
            "AssignProcessToJobObject": ([pointer, pointer], ctypes.c_int),
            "QueryInformationJobObject": ([pointer, ctypes.c_int, pointer, uint, pointer], ctypes.c_int),
            "TerminateJobObject": ([pointer, uint], ctypes.c_int),
            "CloseHandle": ([pointer], ctypes.c_int),
            "CreateToolhelp32Snapshot": ([uint, uint], pointer),
            "Thread32First": ([pointer, ctypes.POINTER(_ThreadEntry)], ctypes.c_int),
            "Thread32Next": ([pointer, ctypes.POINTER(_ThreadEntry)], ctypes.c_int),
            "OpenThread": ([uint, ctypes.c_int, uint], pointer),
            "ResumeThread": ([pointer], uint),
        }
        for name, (argtypes, restype) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = argtypes, restype
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _JobExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, process: Any) -> None:
        # CPython 3.11 retains the native process handle; assignment precedes any CLI code.
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        thread = None
        try:
            entry = _ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            found = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.pid == process.pid:
                    thread = self.kernel.OpenThread(2, False, entry.tid)  # THREAD_SUSPEND_RESUME
                    break
                found = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if not thread:
                raise OSError("CLI suspended thread could not be opened")
            if self.kernel.ResumeThread(thread) != 1:
                raise OSError("CLI suspended thread could not be resumed safely")
        finally:
            if thread:
                self.kernel.CloseHandle(thread)
            self.kernel.CloseHandle(snapshot)

    def active(self) -> Any:
        info = _JobAccounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def terminate(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _run_process(args: Sequence[str | Path], *, timeout: float, env: Any=None, encoding: str="utf-8", errors: str="replace",
                 creationflags: Any=FLAGS, capture_output: bool=True, shell: bool=False) -> subprocess.CompletedProcess[str]:
    """Bound the entire Windows process tree, including inherited output handles."""
    if os.name != "nt":
        return subprocess.run(args, timeout=timeout, env=env, encoding=encoding, errors=errors,
                              capture_output=capture_output, creationflags=creationflags, shell=shell)
    if not capture_output or shell:
        raise ValueError("Only captured shell-free CLI execution is supported")
    deadline = time.monotonic() + timeout
    job, process = _WindowsJob(), None
    try:
        # Regular files avoid communicate() waiting forever for a grandchild's pipe EOF.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(args, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                           env=env, creationflags=creationflags | 4, shell=False)
                job.assign_and_resume(process)
                process.wait(timeout=max(0, deadline - time.monotonic()))
                while job.active():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(args, timeout)
                    time.sleep(min(0.01, remaining))
                stdout.seek(0)
                stderr.seek(0)
                return subprocess.CompletedProcess(args, process.returncode,
                    stdout.read().decode(encoding, errors), stderr.read().decode(encoding, errors))
            except BaseException as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    exc.timeout = timeout
                if process is not None:
                    cleanup_deadline = time.monotonic() + 1
                    # A job assignment/termination failure must never skip the still-suspended root.
                    steps = (("job termination", job.terminate),
                             ("root termination", process.kill),
                             ("root wait", lambda: process.wait(
                                 timeout=max(0, cleanup_deadline - time.monotonic()))))
                    for label, cleanup_step in steps:
                        try:
                            cleanup_step()
                        except Exception as cleanup:
                            exc.add_note(f"CLI {label} failed: {cleanup}")
                    try:
                        while job.active():
                            if time.monotonic() >= cleanup_deadline:
                                raise RuntimeError("CLI child process termination could not be confirmed")
                            time.sleep(0.01)
                    except Exception as cleanup:
                        exc.add_note(f"CLI child process cleanup failed: {cleanup}")
                if isinstance(exc, subprocess.TimeoutExpired):
                    stdout.seek(0)
                    stderr.seek(0)
                    exc.output, exc.stderr = stdout.read(65536), stderr.read(65536)
                raise
    finally:
        # The non-inherited job handle is a final kill-on-close backstop.
        job.close()


def cli_version(path: str | Path, timeout: float=2) -> tuple[int,...]:
    try:
        result = _run_process([path, "--version"], capture_output=True, encoding="utf-8", errors="replace",
                                timeout=timeout, creationflags=FLAGS, shell=False)
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode:
        return ()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout or "")
    return tuple(map(int, match.groups())) if match else ()


@functools.lru_cache(maxsize=32)
def _codex_candidates(candidates: Any) -> Any:
    deadline = time.monotonic() + 8
    found: list[Any]
    found = []
    for path, _mtime, _size in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Codex CLI 버전 탐색 시간 초과")
        version = cli_version(path, timeout=min(2, remaining))
        # An unverified candidate might be newer; an older CLI must not modify its DB.
        if not version:
            raise RuntimeError(f"Codex CLI 버전 확인 실패: {path}")
        found.append((version, path))
    if not found:
        raise RuntimeError("codex 실행 파일을 찾지 못함")
    version, path = max(found, key=lambda row: (row[0], row[1].casefold().endswith(".exe"), row[1]))
    return path, ".".join(map(str, version))


def codex_cli(path_value: Any=None) -> Any:
    # Serialize cache misses too: lru_cache alone launches duplicate probes.
    with _discovery_lock:
        candidates: list[Any]
        seen: set[Any]
        candidates, seen = [], set()
        names = ("codex.exe", "codex.cmd") if os.name == "nt" else ("codex",)
        for folder in (os.environ.get("PATH", "") if path_value is None else path_value).split(os.pathsep):
            if not folder.strip('"'):
                continue
            for name in names:
                path = Path(folder.strip('"')) / name
                if not path.is_file():
                    continue
                path = path.resolve()
                identity = os.path.normcase(str(path))
                if identity not in seen:
                    seen.add(identity)
                    info = path.stat()
                    candidates.append((str(path), info.st_mtime_ns, info.st_size))
        return _codex_candidates(tuple(candidates))


codex_cli.cache_clear = _codex_candidates.cache_clear  # type: ignore[attr-defined]  # intentional callable cache API


def command(exe: Any, args: Any, env: Any=None, timeout: float=30, before_run: Any=None) -> str:
    if exe == "codex":
        path = codex_cli(None if env is None else env.get("PATH", ""))[0]
    else:
        search = env.get("PATH", "") if env is not None else None
        path = shutil.which(exe, path=search)
    if not path:
        raise RuntimeError(f"{exe} 실행 파일을 찾지 못함")
    if before_run:
        before_run()
    result = _run_process([path, *args], capture_output=True, encoding="utf-8", errors="replace",
                            env=env, timeout=timeout, creationflags=FLAGS, shell=False)
    if result.returncode:
        raise RuntimeError(f"{exe}: {(result.stderr or result.stdout).strip()[:400]}")
    return result.stdout


def _process_app(name: str, cmdline: str="") -> str | None:
    name = name.casefold()
    if name == "codex.exe":
        return "Codex"
    if name.startswith("claude") and name.endswith(".exe"):
        return "Claude"
    if name == "orca.exe":
        return "Orca"
    if name in {"herdr.exe", "herdr-app.exe", "herdr-core.exe", "hc.exe"}:
        return "Herdr"
    if name == "node.exe":
        for package, app in (("@google/gemini-cli", "Gemini"), ("@anthropic-ai/claude-code", "Claude"),
                             ("@openai/codex", "Codex")):
            if package in cmdline.replace("\\", "/").casefold():
                return app
    return None


def _native_processes() -> list[dict[str,Any]]:
    found: list[Any]
    found = []
    for pid in psutil.pids():
        try:
            # Fresh objects avoid psutil's cached name surviving Windows PID reuse.
            process = psutil.Process(pid)
            name = process.name()
            cmdline = " ".join(process.cmdline()) if name.casefold() == "node.exe" else ""
            if name.casefold() == "node.exe" and not cmdline:
                raise RuntimeError("node 실행 명령을 확인하지 못해 삭제를 차단함")
            app = _process_app(name, cmdline)
            if app:
                try:
                    path = process.exe()
                except psutil.AccessDenied:
                    path = ""
                found.append({"app": app, "name": name, "pid": process.pid, "path": path})
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError("프로세스 접근 거부로 실행 상태를 확인하지 못해 삭제를 차단함") from exc
    return found


def running_processes() -> list[dict[str,Any]]:
    """Always fresh, including each destructive-operation safety check."""
    if os.name != "nt":
        raise RuntimeError("이 버전의 실행 상태 확인·삭제는 Windows 전용임")
    if psutil is not None:
        return _native_processes()
    script = r"""
    $ErrorActionPreference = 'Stop'
    $filter = "Name = 'codex.exe' OR Name LIKE 'claude%.exe' OR Name = 'orca.exe' OR Name = 'node.exe' OR Name = 'herdr.exe' OR Name = 'herdr-app.exe' OR Name = 'herdr-core.exe' OR Name = 'hc.exe'"
    $found = @(Get-CimInstance Win32_Process -Filter $filter | ForEach-Object {
      $n = $_.Name.ToLowerInvariant()
      $c = [string]$_.CommandLine
      $app = $null
      if ($n -eq 'codex.exe') { $app = 'Codex' }
      elseif ($n -match '^claude.*\.exe$') { $app = 'Claude' }
      elseif ($n -eq 'orca.exe') { $app = 'Orca' }
      elseif ($n -in @('herdr.exe', 'herdr-app.exe', 'herdr-core.exe', 'hc.exe')) { $app = 'Herdr' }
      elseif ($n -eq 'node.exe') {
        if (-not $c) { throw 'Cannot inspect node command line' }
        if ($c -match '@google[\\/]gemini-cli') { $app = 'Gemini' }
        elseif ($c -match '@anthropic-ai[\\/]claude-code') { $app = 'Claude' }
        elseif ($c -match '@openai[\\/]codex') { $app = 'Codex' }
      }
      if ($app) { [pscustomobject]@{app=$app; name=$_.Name; pid=$_.ProcessId; path=[string]$_.ExecutablePath} }
    })
    ConvertTo-Json -InputObject @($found) -Compress
    """
    result = _run_process(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, encoding="utf-8", errors="replace", timeout=8,
                            creationflags=FLAGS, shell=False)
    if result.returncode:
        raise RuntimeError("앱 실행 상태를 확인하지 못해 삭제를 차단함")
    rows = json.loads(result.stdout.strip())
    if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("app") not in PROVIDERS
                                         or not isinstance(row.get("pid"), int) for row in rows):
        raise RuntimeError("앱 실행 상태 응답이 잘못되어 삭제를 차단함")
    return rows


def active_providers() -> LiveApps:
    return lock_state(running_processes())


def orca_projects() -> Any:
    global _orca_cache
    exe = os.environ.get("ORCA_CLI_COMMAND") or ("orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else "orca")
    cache_key = (exe, os.environ.get("PATH", ""))
    with _orca_lock:
        if _orca_cache and _orca_cache[0] == cache_key and time.monotonic() - _orca_cache[1] < 1:
            return [dict(row) for row in _orca_cache[2]]
        data = json.loads(command(exe, ["repo", "list", "--json"], timeout=8))
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise RuntimeError("Orca 프로젝트 조회 실패")
        result = data.get("result")
        repos = result.get("repos") if isinstance(result, dict) else None
        if not isinstance(repos, list) or any(not isinstance(row, dict) or not isinstance(row.get("path"), str)
                                              or not row["path"] for row in repos):
            raise RuntimeError("Orca 프로젝트 조회 응답 오류")
        rows = [{"name": row.get("displayName", "Orca 프로젝트"), "path": row["path"], "id": row.get("id", "")}
                for row in repos]
        _orca_cache = cache_key, time.monotonic(), rows
        return [dict(row) for row in rows]
