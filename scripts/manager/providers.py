from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from datetime import datetime
from pathlib import Path

from .model import Entry, _link_stat, read_db, read_json


SCAN_ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, RuntimeError)


def _plain_stat(path):
    try:
        info = path.lstat()
        return None if _link_stat(info) else info
    except FileNotFoundError:
        return None


def _children(folder, errors=None, provider=""):
    info = _plain_stat(folder)
    if info is None or not stat.S_ISDIR(info.st_mode):
        return
    with os.scandir(folder) as children:
        for child in children:
            try:
                info = child.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except SCAN_ERRORS as exc:
                _error(errors, provider, Path(child.path), exc)
                continue
            if not _link_stat(info):
                yield Path(child.path), info


def _error(errors, provider, home, exc):
    if errors is None:
        raise exc
    errors.append(f"{provider} ({home}): {exc}")


def valid_id(value):
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, TypeError):
        return False


def stamp(value, fallback):
    try:
        if isinstance(value, (int, float)):
            result = value / 1000 if value > 10**11 else value
        else:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        datetime.fromtimestamp(result)  # Reject nonfinite and platform-unrepresentable dates.
        return result
    except (ValueError, TypeError, OverflowError, OSError):
        try:
            datetime.fromtimestamp(fallback)
            return fallback
        except (ValueError, TypeError, OverflowError, OSError):
            return 0


def clean_path(value):
    # Codex stores Windows extended-length paths (\\?\C:\...). Show and compare them as normal paths.
    value = str(value or "")
    if value.startswith(r"\\?\UNC" + "\\"):
        return "\\\\" + value[8:]
    return value[4:] if value.startswith(r"\\?" + "\\") else value


def codex(home: Path, errors=None):
    dbs = sorted((p for p, info in _children(home, errors, "Codex") if stat.S_ISREG(info.st_mode)
                  and p.name.startswith("state_") and p.suffix == ".sqlite"
                  and p.stem[6:].isascii() and p.stem[6:].isdigit()),
                 key=lambda p: int(p.stem[6:]), reverse=True)
    if not dbs:
        return []
    db = dbs[0]
    result = []
    resolved_home, parents = home.resolve(), {}
    with read_db(db) as conn:
        cols = {r[1] for r in conn.execute("pragma table_info(threads)")}
        needed = {"id", "cwd", "title", "updated_at"}
        if not needed <= cols:
            raise ValueError("지원하지 않는 Codex threads 스키마")
        # Avoid loading large unrelated columns (prompts, tool payloads, etc.).
        selected = sorted(needed | (cols & {"name", "rollout_path", "project_id"}))
        for row in conn.execute("select " + ",".join('"' + c + '"' for c in selected) + " from threads"):
            r = dict(row)
            if not valid_id(r["id"]):
                continue
            try:
                files, blocked, candidates = [], "", []
                raw = r.get("rollout_path")
                if raw:
                    p = Path(raw)
                    if not p.is_absolute():
                        p = home / p
                    parent = parents.get(p.parent)
                    if parent is None:
                        parent = p.parent.resolve()
                        parents[p.parent] = parent
                    if parent == resolved_home or resolved_home in parent.parents:
                        # Older rollouts may be compressed in place.
                        candidates = [p, p.with_name(p.name + ".zst")]
                        for candidate in candidates:
                            try:
                                info = candidate.lstat()
                            except FileNotFoundError:
                                continue
                            if _link_stat(info):
                                # Native Codex deletion can remove DB rows even with no files.
                                # Keep this target visible and explicitly block deletion.
                                files.append(candidate)
                                blocked = "대화 파일이 링크·junction이라 정리하지 않음"
                                _error(errors, "Codex", candidate, ValueError(blocked))
                            elif stat.S_ISREG(info.st_mode):
                                files.append(candidate)
                            else:
                                blocked = "대화 파일 형식을 확인할 수 없어 정리하지 않음"
                                _error(errors, "Codex", candidate, ValueError(blocked))
                    else:
                        blocked = "대화 파일이 해당 프로필 밖에 있음"
                result.append(Entry("Codex", home, r["id"], str(r.get("title") or r.get("name") or r["id"]),
                                    clean_path(r.get("cwd")) or "(프로젝트 없음)", stamp(r["updated_at"], 0), files,
                                    extra={"db": str(db), "project_id": r.get("project_id"),
                                           "rollout_candidates": [str(p) for p in candidates]}, blocked=blocked))
            except SCAN_ERRORS as exc:
                _error(errors, "Codex", home, exc)
    return result


def claude(home: Path, errors=None):
    result = []
    projects = home / "projects"
    extra_bases = []
    for name in ("file-history", "session-env", "debug"):
        base = home / name
        info = _plain_stat(base)
        extra_bases.append(base if info is not None and stat.S_ISDIR(info.st_mode) else None)
    for folder, folder_info in _children(projects, errors, "Claude"):
        if not stat.S_ISDIR(folder_info.st_mode):
            continue
        try:
            records = _children(folder, errors, "Claude")
            for p, info in records:
                if p.suffix.lower() != ".jsonl" or not stat.S_ISREG(info.st_mode) or not valid_id(p.stem):
                    continue
                try:
                    result.append(_claude_entry(home, folder, p, info, extra_bases))
                except SCAN_ERRORS as exc:
                    _error(errors, "Claude", p, exc)
        except SCAN_ERRORS as exc:
            _error(errors, "Claude", folder, exc)
    return result


def _claude_entry(home, folder, p, info, extra_bases):
    cwd, title = "", ""
    # Bounded metadata read; never execute transcript contents.
    with p.open("r", encoding="utf-8", errors="replace") as stream:
        for _ in range(80):
            line = stream.readline(262145)
            if not line or len(line) > 262144:
                break
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            value = row.get("cwd")
            if not cwd and isinstance(value, str):
                cwd = value
            if row.get("summary"):
                title = str(row["summary"])[:140]
            message = row.get("message")
            if not title and row.get("type") == "user" and isinstance(message, dict):
                content = message.get("content", "")
                if isinstance(content, str):
                    title = content[:140]
            if cwd and title:
                break
    files = [p]
    extras = [folder / p.stem] + [base / (f"{p.stem}.txt" if base.name == "debug" else p.stem)
                                  for base in extra_bases if base is not None]
    for extra in extras:
        if _plain_stat(extra) is not None:
            files.append(extra)
    return Entry("Claude", home, p.stem, title or p.stem, cwd or folder.name,
                 info.st_mtime, files, extra={"project_dir": str(folder)})


def gemini(home: Path, errors=None):
    result = []
    tmp = home / "tmp"
    for folder, folder_info in _children(tmp, errors, "Gemini"):
        if not stat.S_ISDIR(folder_info.st_mode):
            continue
        marker = folder / ".project_root"
        cwd, blocked = folder.name, ""
        try:
            info = _plain_stat(marker)
            if info is not None and stat.S_ISREG(info.st_mode):
                with marker.open(encoding="utf-8") as stream:
                    cwd = stream.read(65537).strip()
                if len(cwd) > 65536:
                    raise ValueError("프로젝트 경로 크기 한도 초과")
        except SCAN_ERRORS as exc:
            _error(errors, "Gemini", marker, exc)
            cwd, blocked = folder.name, "프로젝트 경로를 읽지 못함"
        try:
            for p, info in _children(folder / "chats", errors, "Gemini"):
                if not (p.name.startswith("session-") and p.suffix.lower() == ".json"
                        and stat.S_ISREG(info.st_mode)):
                    continue
                try:
                    row = read_json(p)
                    if not isinstance(row, dict):
                        raise ValueError("지원하지 않는 Gemini 기록 형식")
                    session = row.get("sessionId", row.get("id", ""))
                    if not valid_id(session):
                        continue
                    title = row.get("summary", row.get("title", ""))
                    if not title and isinstance(row.get("messages"), list):
                        for message in row["messages"]:
                            if isinstance(message, dict) and message.get("type") == "user":
                                value = message.get("content", "")
                                title = value if isinstance(value, str) else ""
                                break
                    result.append(Entry("Gemini", home, session, str(title or session)[:140], cwd,
                                        stamp(row.get("lastUpdated"), info.st_mtime), [p],
                                        extra={"project_dir": str(folder)}, blocked=blocked))
                except SCAN_ERRORS as exc:
                    _error(errors, "Gemini", p, exc)
        except SCAN_ERRORS as exc:
            _error(errors, "Gemini", folder, exc)
    return result


def profile_roots(user_home=None, roaming=None, errors=None):
    user_home = Path(user_home or Path.home())
    roaming = Path(roaming or os.environ.get("APPDATA", user_home / "AppData" / "Roaming"))
    roots = [("Codex", Path(os.environ.get("CODEX_HOME", user_home / ".codex"))),
             ("Claude", Path(os.environ.get("CLAUDE_CONFIG_DIR", user_home / ".claude"))),
             ("Gemini", user_home / ".gemini")]
    base = roaming / "orca"
    try:
        info = _plain_stat(base)
        if info is not None and stat.S_ISDIR(info.st_mode):
            for name, provider in [("codex-accounts", "Codex"), ("claude-accounts", "Claude"),
                                   ("claude-profiles", "Claude")]:
                try:
                    roots.extend((provider, p if name == "claude-profiles" else p / "home")
                                 for p, info in _children(base / name, errors, provider) if stat.S_ISDIR(info.st_mode))
                except SCAN_ERRORS as exc:
                    _error(errors, provider, base / name, exc)
            runtime = base / "codex-runtime-home"
            info = _plain_stat(runtime)
            if info is not None and stat.S_ISDIR(info.st_mode):
                roots.append(("Codex", runtime / "home"))
    except SCAN_ERRORS as exc:
        _error(errors, "Orca", base, exc)
    seen, found = set(), []
    for provider, p in roots:
        try:
            info = _plain_stat(p)
            if info is None or not stat.S_ISDIR(info.st_mode):
                continue
            resolved = p.resolve()
            key = (provider, os.path.normcase(str(resolved)))
            if key not in seen:
                seen.add(key)
                found.append((provider, resolved))
        except SCAN_ERRORS as exc:
            _error(errors, provider, p, exc)
    return found


# Only unambiguous runtime caches. Gemini tmp contains conversations: never bulk-delete it.
CACHES = {"Codex": [".tmp", "tmp", "log", "cache"],
          "Claude": ["cache", "shell-snapshots", "paste-cache"], "Gemini": [],
          "Orca": ["Cache", "Code Cache", "GPUCache", "logs"]}


def scan(roots=None):
    entries, errors = [], []
    selected_roots = list(roots) if roots is not None else profile_roots(errors=errors)
    if roots is None:
        orca_root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "orca"
        try:
            info = _plain_stat(orca_root)
            if info is not None and stat.S_ISDIR(info.st_mode):
                selected_roots.append(("Orca", orca_root.resolve()))
        except SCAN_ERRORS as exc:
            _error(errors, "Orca", orca_root, exc)
    seen = set()
    for provider, home in selected_roots:
        try:
            home = Path(home)
            info = _plain_stat(home)
            if info is None or not stat.S_ISDIR(info.st_mode):
                continue
            key = (provider, os.path.normcase(str(home.resolve())))
            if key in seen:
                continue
            seen.add(key)
            if provider != "Orca":
                entries.extend({"Codex": codex, "Claude": claude, "Gemini": gemini}[provider](home, errors=errors))
        except SCAN_ERRORS as exc:
            _error(errors, provider, home, exc)
        for name in CACHES.get(provider, []):
            p = home / name
            try:
                info = _plain_stat(p)
                if info is not None:
                    entries.append(Entry(provider, home, f"cache:{name}", name, "(공용 캐시·로그)",
                                         info.st_mtime, [p], "cache"))
            except SCAN_ERRORS as exc:
                _error(errors, provider, p, exc)
    project_paths = []
    for project in dict.fromkeys(e.project for e in entries if e.kind == "conversation"):
        try:
            if Path(project).is_absolute():
                project_paths.append(str(Path(project).resolve()))
        except SCAN_ERRORS as exc:
            _error(errors, "프로젝트 경로", "", exc)
    unique = {}
    for e in entries:
        previous = unique.get(e.key)
        if previous is not None:
            previous.files = list(dict.fromkeys(previous.files + e.files))
            previous.blocked = "대화 ID가 여러 기록에 중복되어 정리하지 않음"
            _error(errors, e.provider, e.home, ValueError(previous.blocked))
            continue
        unique[e.key] = e
        e.extra["protected_projects"] = project_paths
    return list(unique.values()), errors
