"""Herdr core inventory. Persistent terminal state is intentionally read-only."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .model import Entry, read_json


def storage_root():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "herdr-core"


def terminals(home, errors=None):
    from .providers import SCAN_ERRORS, _error, _plain_stat, clean_path, stamp

    path = home / "terminals.json"
    if _plain_stat(path) is None:
        return []
    data = read_json(path)
    rows = data.get("terminals") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        raise ValueError("지원하지 않는 Herdr terminals 스키마")
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


def protected_homes():
    return {storage_root(), Path(os.environ.get("HERDR_APP_HOME", Path.home() / ".config/herdr-app")),
            Path.home() / ".config/herdr-core"}
