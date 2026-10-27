from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def cli_version(path):
    try:
        out = subprocess.run([path, "--version"], capture_output=True, encoding="utf-8", errors="replace",
                             timeout=15, creationflags=FLAGS).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(map(int, m.groups())) if m else ()


@functools.lru_cache(maxsize=None)
def codex_cli():
    # Several Codex CLIs can be on PATH (desktop-app bundle, npm). Use the newest one,
    # so an older binary never touches a database migrated by a newer Codex.
    found, seen = [], set()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        for name in ("codex.exe", "codex.cmd"):
            p = Path(folder.strip('"')) / name
            if p.is_file() and str(p).casefold() not in seen:
                seen.add(str(p).casefold())
                found.append((cli_version(str(p)), str(p)))
    if not found:
        raise RuntimeError("codex 실행 파일을 찾지 못함")
    version, path = max(found, key=lambda x: x[0])
    return path, ".".join(map(str, version)) or "버전 확인 불가"


def command(exe, args, env=None, timeout=30):
    if exe == "codex":
        path = codex_cli()[0]
    else:
        path = shutil.which(exe + ".exe") or shutil.which(exe + ".cmd") or shutil.which(exe)
    if not path:
        raise RuntimeError(f"{exe} 실행 파일을 찾지 못함")
    result = subprocess.run([path, *args], capture_output=True, encoding="utf-8", errors="replace",
                            env=env, timeout=timeout, creationflags=FLAGS, shell=False)
    if result.returncode:
        raise RuntimeError(f"{exe}: {(result.stderr or result.stdout).strip()[:400]}")
    return result.stdout


def running_processes():
    """Processes that hold AI app records open: [{app, name, pid, path}]."""
    if os.name != "nt":
        raise RuntimeError("이 버전의 실행 상태 확인·삭제는 Windows 전용임")
    script = r"""
    $ErrorActionPreference = 'Stop'
    $found = @()
    Get-CimInstance Win32_Process | ForEach-Object {
      $n = $_.Name.ToLowerInvariant()
      $c = [string]$_.CommandLine
      $app = $null
      if ($n -eq 'codex.exe') { $app = 'Codex' }
      elseif ($n -match '^claude.*\.exe$') { $app = 'Claude' }
      elseif ($n -eq 'orca.exe') { $app = 'Orca' }
      elseif ($n -eq 'node.exe' -and $c -match '@google[\\/]gemini-cli') { $app = 'Gemini' }
      elseif ($n -eq 'node.exe' -and $c -match '@anthropic-ai[\\/]claude-code') { $app = 'Claude' }
      elseif ($n -eq 'node.exe' -and $c -match '@openai[\\/]codex') { $app = 'Codex' }
      if ($app) { $found += [pscustomobject]@{app=$app; name=$_.Name; pid=$_.ProcessId; path=[string]$_.ExecutablePath} }
    }
    ConvertTo-Json -InputObject @($found) -Compress
    """
    p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, encoding="utf-8", errors="replace", timeout=20,
                       creationflags=FLAGS)
    if p.returncode:
        raise RuntimeError("앱 실행 상태를 확인하지 못해 삭제를 차단함")
    return json.loads(p.stdout.strip() or "[]")


def active_providers():
    return {r["app"] for r in running_processes()}


def orca_projects():
    # CLI resolves the active Orca profile. Never edit Orca's JSON/SQLite by guessing its schema.
    exe = os.environ.get("ORCA_CLI_COMMAND", "orca-dev" if os.environ.get("ORCA_DEV_REPO_ROOT") else "orca")
    data = json.loads(command(exe, ["repo", "list", "--json"], timeout=8))
    if not data.get("ok"):
        raise RuntimeError("Orca 프로젝트 조회 실패")
    return [{"name": r.get("displayName", "Orca 프로젝트"), "path": r.get("path", ""), "id": r.get("id", "")}
            for r in data.get("result", {}).get("repos", [])]
