from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from .model import Entry, inside, is_link, read_db, read_json


def valid_id(value):
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (ValueError, TypeError):
        return False


def stamp(value, fallback):
    try:
        if isinstance(value, (int, float)):
            return value / 1000 if value > 10**11 else value
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return fallback


def clean_path(value):
    # Codex stores Windows extended-length paths (\\?\C:\...). Show and compare them as normal paths.
    value = str(value or "")
    if value.startswith(r"\\?\UNC" + "\\"):
        return "\\\\" + value[8:]
    return value[4:] if value.startswith(r"\\?" + "\\") else value


def codex(home: Path):
    dbs = sorted(home.glob("state_*.sqlite"), key=lambda p: int(p.stem.split("_")[-1]), reverse=True)
    if not dbs:
        return []
    db = dbs[0]
    result = []
    with read_db(db) as conn:
        cols = {r[1] for r in conn.execute("pragma table_info(threads)")}
        needed = {"id", "cwd", "title", "updated_at"}
        if not needed <= cols:
            raise ValueError("지원하지 않는 Codex threads 스키마")
        for row in conn.execute("select * from threads"):
            r = dict(row)
            if not valid_id(r["id"]):
                continue
            files = []
            raw = r.get("rollout_path")
            blocked = ""
            if raw:
                p = Path(raw)
                if not p.is_absolute():
                    p = home / p
                if inside(p, home):
                    # Codex compresses older rollouts in place: <rollout>.jsonl -> <rollout>.jsonl.zst
                    files.extend(c for c in (p, p.with_name(p.name + ".zst")) if c.exists())
                else:
                    blocked = "대화 파일이 해당 프로필 밖에 있음"
            result.append(Entry("Codex", home, r["id"], r.get("title") or r.get("name") or r["id"],
                                clean_path(r.get("cwd")) or "(프로젝트 없음)", stamp(r["updated_at"], 0), files,
                                extra={"db": str(db), "project_id": r.get("project_id")}, blocked=blocked))
    return result


def claude(home: Path):
    result = []
    projects = home / "projects"
    if not projects.exists():
        return result
    for folder in projects.iterdir():
        if not folder.is_dir() or is_link(folder):
            continue
        for p in folder.glob("*.jsonl"):
            if not valid_id(p.stem) or is_link(p):
                continue
            cwd, title = "", ""
            # Bounded metadata read; never execute transcript contents.
            with p.open("r", encoding="utf-8", errors="replace") as stream:
                for _ in range(80):
                    line = stream.readline(262145)
                    if not line:
                        break
                    if len(line) > 262144:
                        break
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    cwd = cwd or row.get("cwd", "")
                    if row.get("summary"):
                        title = str(row["summary"])
                    if not title and row.get("type") == "user":
                        content = row.get("message", {}).get("content", "")
                        if isinstance(content, str):
                            title = content[:140]
                    if cwd and title:
                        break
            files = [p]
            for extra in [folder / p.stem, home / "file-history" / p.stem,
                          home / "session-env" / p.stem, home / "debug" / f"{p.stem}.txt"]:
                if extra.exists() and not is_link(extra):
                    files.append(extra)
            result.append(Entry("Claude", home, p.stem, title or p.stem, cwd or folder.name,
                                p.stat().st_mtime, files, extra={"project_dir": str(folder)}))
    return result


def gemini(home: Path):
    result = []
    tmp = home / "tmp"
    if not tmp.exists():
        return result
    for folder in tmp.iterdir():
        if not folder.is_dir() or is_link(folder):
            continue
        marker = folder / ".project_root"
        cwd = marker.read_text(encoding="utf-8").strip() if marker.is_file() else folder.name
        for p in (folder / "chats").glob("session-*.json"):
            if is_link(p):
                continue
            row = read_json(p)
            if not isinstance(row, dict):
                raise ValueError("지원하지 않는 Gemini 기록 형식")
            session = row.get("sessionId", row.get("id", ""))
            if not valid_id(session):
                continue
            title = row.get("summary", row.get("title", ""))
            if not title:
                for message in row.get("messages", []):
                    if message.get("type") == "user":
                        value = message.get("content", "")
                        title = value if isinstance(value, str) else ""
                        break
            result.append(Entry("Gemini", home, session, str(title or session)[:140], cwd,
                                stamp(row.get("lastUpdated"), p.stat().st_mtime), [p],
                                extra={"project_dir": str(folder)}))
    return result


def profile_roots(user_home=None, roaming=None):
    user_home = Path(user_home or Path.home())
    roaming = Path(roaming or os.environ.get("APPDATA", user_home / "AppData" / "Roaming"))
    roots = [("Codex", Path(os.environ.get("CODEX_HOME", user_home / ".codex"))),
             ("Claude", Path(os.environ.get("CLAUDE_CONFIG_DIR", user_home / ".claude"))),
             ("Gemini", user_home / ".gemini")]
    base = roaming / "orca"
    for pattern, provider in [("codex-accounts/*/home", "Codex"), ("codex-runtime-home/home", "Codex"),
                              ("claude-accounts/*/home", "Claude"), ("claude-profiles/*", "Claude")]:
        roots.extend((provider, p) for p in base.glob(pattern) if p.is_dir())
    seen, found = set(), []
    for provider, p in roots:
        if not p.is_dir() or is_link(p):
            continue
        key = (provider, str(p.resolve()).casefold())
        if key not in seen:
            seen.add(key)
            found.append((provider, p.resolve()))
    return found


# Only unambiguous runtime caches. Gemini tmp contains conversations: never bulk-delete it.
CACHES = {"Codex": [".tmp", "tmp", "log", "cache"],
          "Claude": ["cache", "shell-snapshots", "paste-cache"], "Gemini": [],
          "Orca": ["Cache", "Code Cache", "GPUCache", "logs"]}


def scan(roots=None):
    entries, errors = [], []
    selected_roots = roots if roots is not None else profile_roots()
    if roots is None:
        orca_root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "orca"
        if orca_root.is_dir() and not is_link(orca_root):
            selected_roots.append(("Orca", orca_root.resolve()))
    for provider, home in selected_roots:
        try:
            entries.extend({"Codex": codex, "Claude": claude, "Gemini": gemini, "Orca": lambda _: []}[provider](home))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            errors.append(f"{provider} ({home}): {exc}")
        for name in CACHES.get(provider, []):
            p = home / name
            if p.exists() and not is_link(p):
                entries.append(Entry(provider, home, f"cache:{name}", name, "(공용 캐시·로그)",
                                     p.stat().st_mtime, [p], "cache"))
    project_paths = [str(Path(e.project).resolve()) for e in entries
                     if e.kind == "conversation" and Path(e.project).is_absolute()]
    for e in entries:
        e.extra["protected_projects"] = project_paths
    return entries, errors
