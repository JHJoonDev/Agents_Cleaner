from __future__ import annotations

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
    import psutil
except ImportError:
    psutil = None

PROVIDERS = {"Codex", "Claude", "Gemini", "Orca", "Herdr"}


def _orca_may_use(home):
    # ~/.codex/accounts homes have no "orca" in their path but can be Orca-managed.
    parts = [part.casefold() for part in Path(home).parts]
    return "orca" in str(home).lower() or parts[-3:-1] == [".codex", "accounts"]


def entry_running(entry, live):
    return (entry.provider in live or ("Orca" in live and _orca_may_use(entry.home))
            or ("Herdr" in live and entry.provider in {"Codex", "Claude", "Gemini"}))


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
    def __init__(self):
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

    def assign_and_resume(self, process):
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

    def active(self):
        info = _JobAccounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _run_process(args, *, timeout, env=None, encoding="utf-8", errors="replace",
                 creationflags=FLAGS, capture_output=True, shell=False):
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


def cli_version(path, timeout=2):
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
def _codex_candidates(candidates):
    deadline = time.monotonic() + 8
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


def codex_cli(path_value=None):
    # Serialize cache misses too: lru_cache alone launches duplicate probes.
    with _discovery_lock:
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


codex_cli.cache_clear = _codex_candidates.cache_clear


def command(exe, args, env=None, timeout=30, before_run=None):
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


def _process_app(name, cmdline=""):
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


def _native_processes():
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


def running_processes():
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


def active_providers():
    return {row["app"] for row in running_processes()}


def orca_projects():
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
