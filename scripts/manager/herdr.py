"""Herdr core inventory. Persistent terminal state is intentionally read-only."""
from __future__ import annotations
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence, TypeVar, ParamSpec, cast

import os
import re
import json
import shutil
import subprocess
from pathlib import Path

from .model import Entry, read_json


def is_root_like(path: Path) -> bool:
    """Drive roots and user-home ancestors must not match entire project trees."""
    from .providers import clean_path
    target = Path(clean_path(path)).resolve()
    home = Path.home().resolve()
    return target == target.parent or target == home or target in home.parents


def live_cwds() -> list[Path] | None:
    """Read live local terminal folders, or None when protection cannot be established."""
    from .providers import clean_path
    try:
        installed = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/herdr-core/hc.exe"
        executable = str(installed) if installed.is_file() else shutil.which("hc")
        if not executable:
            return None
        result = subprocess.run([executable, "ls", "--json"], capture_output=True, text=True,
                                encoding="utf-8", timeout=3, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(result.stdout)
        rows = data.get("terminals") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return None
        found: list[Any]
        found = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            if row.get("state") not in {"live", "finished"}:
                return None
            if row.get("state") != "live":
                continue
            foreground = row.get("foreground")
            cwd = (foreground.get("cwd") if isinstance(foreground, dict) else None) or row.get("cwd")
            if not isinstance(cwd, str) or not cwd:
                return None
            path = Path(clean_path(cwd))
            if not path.is_absolute():
                return None
            found.append(path)
        return found
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None


def storage_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "herdr-core"


def terminals(home: Path, errors: list[str] | None=None) -> list[Entry]:
    from .providers import SCAN_ERRORS, _error, _plain_stat, clean_path, stamp

    path = home / "terminals.json"
    if _plain_stat(path) is None:
        return []
    data = read_json(path)
    rows = data.get("terminals") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        raise ValueError("지원하지 않는 Herdr terminals 스키마")
    result: list[Any]
    result = []
    for key, row in rows.items():
        try:
            if not isinstance(row, dict):
                raise ValueError("Herdr 터미널 항목 형식 오류")
            sid = row.get("address")
            if not isinstance(sid, str) or not re.fullmatch(r"t[0-9a-z]+", sid):
                raise ValueError("Herdr 터미널 항목 형식 오류")
            # Never use screen paths, argv, restart_env, or remote addresses as targets.
            cwd = clean_path(row.get("last_cwd") or row.get("cwd"))
            title = str(row.get("name") or row.get("title") or sid)
            state = str(row.get("state") or "unknown")
            updated = row.get("finished_ms") or row.get("program_started_ms") or row.get("created_ms")
            result.append(Entry("Herdr", home, sid, f"{title} [{state}]",
                                cwd or "(프로젝트 없음)", stamp(updated, path.stat().st_mtime),
                                blocked="Herdr 터미널은 조회 전용입니다. 종료된 터미널은 hc rm <ID>로 제거하세요."))
        except SCAN_ERRORS as exc:
            _error(errors, "Herdr", home, exc)
    return result


def protected_homes() -> set[Path]:
    return {storage_root(), Path(os.environ.get("HERDR_APP_HOME", Path.home() / ".config/herdr-app")),
            Path.home() / ".config/herdr-core"}
