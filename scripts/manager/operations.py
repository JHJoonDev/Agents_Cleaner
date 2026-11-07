from __future__ import annotations

import json
import functools
import hashlib
import os
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from .model import ClosingConnection, Entry, inside, is_link, read_db, safe_path, signature
from .providers import CACHES, valid_id
from .runtime import PROVIDERS, active_providers, command, entry_running, workspace_blocker
from .recycle import recycle
from .workspaces import ai_homes, conversations_in, fixed_problem, workspace_problem

DEFAULT_BACKUP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AI Storage Manager" / "backups"
BACKUP_PATTERN = "ai-records-*.zip"
CODEX_DELETE_TIMEOUT = 30
_mutation_lock = threading.Lock()


def _exclusive(function):
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        if not _mutation_lock.acquire(blocking=False):
            raise RuntimeError("다른 정리 작업이 진행 중임")
        try:
            return function(*args, **kwargs)
        finally:
            _mutation_lock.release()
    return guarded


def _no_links(path):
    for part in (Path(path).absolute(), *Path(path).absolute().parents):
        if part.exists() or part.is_symlink():
            if is_link(part):
                raise ValueError(f"링크·junction 경로는 사용하지 않음: {part}")


def _checked_nodes(path, home):
    safe_path(path, home)
    yield path
    if path.is_file():
        return
    def fail(exc):
        raise exc
    for parent, dirs, files in os.walk(path, followlinks=False, onerror=fail):
        dirs.sort()
        files.sort()
        for name in (*dirs, *files):
            item = Path(parent) / name
            safe_path(item, home)
            if is_link(item):
                raise ValueError(f"대상 안의 링크·junction은 정리하지 않음: {item}")
            yield item


def _checked_files(path, home):
    return (item for item in _checked_nodes(path, home) if item.is_file())


def _identity(path):
    info = Path(path).lstat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode


def _target_state(path, home):
    digest = hashlib.sha256(repr(_identity(path)).encode())
    for item in _checked_nodes(path, home):
        digest.update(repr((str(item), _identity(item))).encode())
    return digest.digest()


def _project_state(folder, home, entries, removed=None):
    if removed is None:
        removed = {p.resolve() for e in entries for p in e.files}
    rows = []
    for path in _checked_files(folder, home):
        resolved = path.resolve()
        if not any(parent in removed for parent in (resolved, *resolved.parents)):
            rows.append((str(path), _identity(path)))
    return hashlib.sha256(repr(rows).encode()).digest()


def _stopped(detector, entries, workspace=False):
    live = detector()
    if not isinstance(live, (set, frozenset)) or not live.issubset(PROVIDERS):
        raise RuntimeError("실행 상태를 확인하지 못해 삭제를 차단함")
    if workspace and workspace_blocker(live):
        raise RuntimeError(workspace_blocker(live))
    for entry in entries:
        if entry_running(entry, live):
            raise RuntimeError(f"{entry.provider}, Orca 또는 Herdr가 실행 중임. 앱과 CLI를 닫은 뒤 삭제")


def _snapshot_database(source, target, timeout=8):
    deadline = time.monotonic() + timeout
    def progress(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise TimeoutError("SQLite 백업 시간 초과. 원본은 삭제하지 않음")
    with read_db(source) as src, sqlite3.connect(target, factory=ClosingConnection) as dst:
        src.execute("PRAGMA busy_timeout=0")
        src.backup(dst, pages=256, progress=progress, sleep=0.05)


def validate(entry: Entry):
    if entry.provider not in PROVIDERS or entry.kind not in {"cache", "conversation"} or (
            entry.provider in {"Orca", "Herdr"} and entry.kind != "cache"):
        raise ValueError("지원하지 않는 정리 대상")
    _no_links(entry.home)
    if entry.blocked:
        raise ValueError(entry.blocked)
    if entry.kind == "cache":
        if entry.session.removeprefix("cache:") not in CACHES.get(entry.provider, []):
            raise ValueError("허용되지 않은 캐시")
        expected = [entry.home / entry.session.removeprefix("cache:")]
        if entry.files != expected:
            raise ValueError("캐시 경로 불일치")
    elif not valid_id(entry.session):
        raise ValueError("잘못된 대화 ID")
    for p in entry.files:
        safe_path(p, entry.home)
        for original in entry.extra.get("protected_projects", []):
            source = Path(original)
            if source.resolve() == p.resolve() or inside(source, p):
                raise ValueError("원본 프로젝트를 포함하는 대상은 정리하지 않음")
        rel = p.relative_to(entry.home)
        if entry.kind == "conversation":
            if entry.provider == "Codex" and rel.parts[0] not in {"sessions", "archived_sessions"}:
                raise ValueError("Codex 대화 파일 경로 확인 필요")
            if entry.provider == "Claude":
                if rel.parts[0] not in {"projects", "file-history", "session-env", "debug"}:
                    raise ValueError("Claude 대화 파일 경로 확인 필요")
                if p.stem != entry.session and p.name != entry.session:
                    raise ValueError("Claude 대화 ID와 파일명 불일치")
                if len(rel.parts) != (3 if rel.parts[0] == "projects" else 2):
                    raise ValueError("Claude 대화 파일 깊이 불일치")
            if entry.provider == "Gemini" and (len(rel.parts) != 4 or rel.parts[0] != "tmp" or rel.parts[2] != "chats"):
                raise ValueError("Gemini 대화 파일 경로 확인 필요")
    if entry.provider == "Codex" and entry.kind == "conversation":
        database = Path(entry.extra["db"])
        safe_path(database, entry.home)
        if database.parent != entry.home or not database.match("state_*.sqlite"):
            raise ValueError("Codex 데이터베이스 경로 불일치")
        with read_db(database) as db:
            row = db.execute("select updated_at from threads where id=?", (entry.session,)).fetchone()
        if not row or abs(float(row[0]) - entry.updated) > 1:
            raise ValueError("대화가 변경되었음. 새로고침 후 다시 선택")


def backup(entries, destination: Path, project_groups, include_caches=True, workspaces=()):
    destination = Path(destination)
    _no_links(destination)
    for workspace in workspaces:
        if destination.resolve() == Path(workspace).resolve() or inside(destination, Path(workspace)):
            raise ValueError("백업 위치가 삭제할 작업 폴더 안에 있음")
    for e in entries:
        validate(e)
        for p in e.files:
            if destination.resolve() == p.resolve() or inside(destination, p):
                raise ValueError("백업 위치가 삭제 대상 안에 있음")
        if e.group in project_groups and e.extra.get("project_dir"):
            p = Path(e.extra["project_dir"])
            if destination.resolve() == p.resolve() or inside(destination, p):
                raise ValueError("백업 위치가 삭제할 앱 프로젝트 안에 있음")
    destination.mkdir(parents=True, exist_ok=True)
    _no_links(destination)
    filename = destination / f"ai-records-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.zip"
    final_name = filename
    filename = filename.with_suffix(".zip.partial")
    destination_id = _identity(destination)[:2]
    try:
        paths, dbs, states = {}, set(), {}
        metadata_homes, project_folders = set(), set()
        manifest = []
        for e in entries:
            validate(e)
            prefix = e.provider + "-" + e.key[:12]
            # Caches are regenerated by the apps; backing them up only doubles their disk usage.
            skip = e.kind == "cache" and not include_caches
            manifest.append({"provider": e.provider, "home": str(e.home), "session": e.session,
                             "title": e.title, "project": e.project, "prefix": prefix, "backed_up": not skip})
            if skip:
                continue
            for p in e.files:
                states[p] = (e.home, _target_state(p, e.home))
                for item in _checked_files(p, e.home):
                    paths[str(item)] = (item, prefix + "/" + str(item.relative_to(e.home)).replace("\\", "/"))
            if e.kind == "conversation" and (e.provider, e.home) not in metadata_homes:
                metadata_homes.add((e.provider, e.home))
                for name in ("history.jsonl", "session_index.jsonl"):
                    item = e.home / name
                    if item.is_file():
                        safe_path(item, e.home)
                        paths.setdefault(str(item), (item, prefix + "/" + name))
                if e.provider == "Codex":
                    for pattern in ("state_*.sqlite", "thread_history_*.sqlite", "goals_*.sqlite", "memories_*.sqlite"):
                        dbs.update(e.home.glob(pattern))
            if e.kind == "conversation" and e.group in project_groups and e.provider in {"Claude", "Gemini"}:
                folder = Path(e.extra["project_dir"])
                validate_project_folder(e, folder)
                if folder in project_folders:
                    continue
                project_folders.add(folder)
                states[folder] = (e.home, _target_state(folder, e.home))
                for item in _checked_files(folder, e.home):
                    paths.setdefault(str(item), (item, prefix + "/" + str(item.relative_to(e.home)).replace("\\", "/")))
        with zipfile.ZipFile(filename, "x", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            if workspaces:
                # Work folders are not archived: the user moved the results elsewhere. Keep a record of what went.
                archive.writestr("workspaces.json", json.dumps(list(workspaces), ensure_ascii=False, indent=2))
            for p, name in paths.values():
                # Same-drive backups are preserved but do not reclaim their occupied space.
                if inside(destination, p) or destination.resolve() == p.resolve():
                    raise ValueError("백업 위치가 삭제 대상 안에 있음")
                _no_links(p)
                before = _identity(p)
                archive.write(p, name)
                _no_links(p)
                if _identity(p) != before:
                    raise ValueError("백업 중 파일이 변경됨")
            for db in sorted(dbs):
                _no_links(db)
                safe_path(db, db.parent)
                with tempfile.TemporaryDirectory(prefix="ai-storage-db-") as temp:
                    target = Path(temp) / db.name
                    _snapshot_database(db, target)
                    prefix = next(e.key[:12] for e in entries if e.home == db.parent)
                    archive.write(target, f"Codex-{prefix}/database-snapshot/{db.name}")
        with zipfile.ZipFile(filename) as check:
            if check.testzip():
                raise RuntimeError("백업 무결성 검사 실패. 원본은 삭제하지 않음")
        for p, (home, state) in states.items():
            if _target_state(p, home) != state:
                raise ValueError("백업 중 대상이 변경됨")
        _no_links(destination)
        if _identity(destination)[:2] != destination_id:
            raise ValueError("백업 폴더가 변경됨")
        # Windows rename is atomic and refuses overwriting an existing backup.
        if os.name == "nt":
            os.rename(filename, final_name)
        else:
            os.link(filename, final_name)
            filename.unlink()
        return final_name
    finally:
        # Only our incomplete archive in the original directory may be removed.
        try:
            _no_links(destination)
            if _identity(destination)[:2] == destination_id:
                filename.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def trim_claude_history(home, sessions, before_replace=None):
    path = home / "history.jsonl"
    if not path.exists():
        return
    safe_path(path, home)
    _no_links(path)
    before = _identity(path)
    fd, name = tempfile.mkstemp(prefix=".history-clean-", dir=home)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as dst, path.open("r", encoding="utf-8", newline="") as src:
            for line in src:
                try:
                    row = json.loads(line)
                except ValueError:
                    dst.write(line)
                    continue
                if not isinstance(row, dict) or row.get("sessionId") not in sessions:
                    dst.write(line)
        safe_path(path, home)
        _no_links(path)
        if _identity(path) != before:
            raise ValueError("Claude 인덱스가 변경되어 덮어쓰지 않음")
        if before_replace:
            before_replace()
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def validate_project_folder(entry, folder):
    safe_path(folder, entry.home)
    parts = folder.relative_to(entry.home).parts
    expected = "projects" if entry.provider == "Claude" else "tmp"
    if len(parts) != 2 or parts[0] != expected:
        raise ValueError("앱 프로젝트 저장 경로가 아님")
    for original in entry.extra.get("protected_projects", []):
        p = Path(original)
        if p.resolve() == folder.resolve() or inside(p, folder):
            raise ValueError("원본 프로젝트를 포함해 보존함")


@_exclusive
def delete(entries, expected, destination=None, project_groups=(), detector=active_providers, runner=command,
           recycler=recycle, include_caches=True, workspaces=(), all_entries=None, create_backup=False):
    if not entries:
        raise ValueError("선택한 항목 없음")
    entries, workspaces, project_groups = list(entries), tuple(workspaces), set(project_groups)
    everything = list(all_entries) if all_entries is not None else list(entries)
    homes = ai_homes(everything)
    selected = {e.key for e in entries}
    for path in workspaces:
        reason = workspace_problem(path, everything, selected, homes)
        if reason:
            raise ValueError(f"작업 폴더 {path}: {reason}")
    _stopped(detector, entries, workspace=bool(workspaces))
    states, workspace_states, project_states = {}, {}, {}
    removed_paths = {p.resolve() for e in entries for p in e.files}
    for path in workspaces:
        workspace_states[path] = _target_state(Path(path), Path(path).parent)
    for e in entries:
        validate(e)
        if signature(e) != expected.get(e.key):
            raise ValueError("선택 이후 파일이 변경됨. 새로고침 필요")
        for path in e.files:
            states[path] = _target_state(path, e.home)
        if e.group not in project_states and e.group in project_groups and e.provider in {"Claude", "Gemini"} and e.kind == "conversation":
            folder = Path(e.extra["project_dir"])
            validate_project_folder(e, folder)
            project_states[e.group] = _project_state(folder, e.home, entries, removed_paths)
    archive = backup(entries, Path(destination), project_groups, include_caches, workspaces) if create_backup else None
    archive_state = _identity(archive) if archive is not None else None
    def check_archive():
        if archive is None:
            return
        _no_links(archive)
        if _identity(archive) != archive_state:
            raise ValueError("검증된 백업이 변경되어 삭제를 중단함")
    succeeded, failed, notes, claude_ids = [], [], [], {}
    aborted = ""
    def stopped(targets, workspace=False):
        nonlocal aborted
        if aborted:
            raise RuntimeError(aborted)
        try:
            _stopped(detector, targets, workspace)
        except Exception as exc:
            aborted = f"실행 상태 변경·확인 실패로 남은 정리 중단: {exc}"
            raise
    for e in entries:
        if aborted:
            failed.append((e.title[:60], f"미시도·보존: {aborted}"))
            continue
        try:
            # Read-only validation may be slow; check fresh processes at each
            # actual mutation boundary (CLI before_run or each recycle call).
            validate(e)
            if signature(e) != expected[e.key]:
                raise ValueError("삭제 준비 중 파일이 변경됨")
            check_archive()
            if e.provider == "Codex" and e.kind == "conversation":
                env = os.environ.copy()
                env["CODEX_HOME"] = str(e.home)
                env.pop("CODEX_SQLITE_HOME", None)
                options = {"env": env, "timeout": CODEX_DELETE_TIMEOUT}
                if runner is command:
                    def before_run():
                        validate(e)
                        if signature(e) != expected[e.key]:
                            raise ValueError("CLI 탐색 중 대상이 변경됨")
                        stopped([e])
                        check_archive()
                    options["before_run"] = before_run
                try:
                    if runner is not command:
                        stopped([e])
                    runner("codex", ["--no-daemon", "delete", e.session, "--force"], **options)
                    with read_db(Path(e.extra["db"])) as db:
                        if db.execute("select 1 from threads where id=?", (e.session,)).fetchone():
                            raise RuntimeError("Codex 삭제 명령 이후에도 대화가 남아 있음")
                except Exception as exc:
                    aborted = f"Codex 명령 실패로 남은 정리 중단: {exc}"
                    raise
            else:
                for path in e.files:
                    stopped([e])
                    _no_links(path)
                    if _target_state(path, e.home) != states[path]:
                        raise ValueError("삭제 직전 대상이 변경됨")
                    check_archive()
                    recycler(path)
            if e.provider == "Claude" and e.kind == "conversation":
                claude_ids.setdefault(e.home, set()).add(e.session)
            succeeded.append(e)
        except Exception as exc:
            failed.append((e.title[:60], str(exc)))
    if aborted:
        notes.append("남은 항목과 인덱스·프로젝트·작업 폴더 후처리를 모두 보존함")
        return archive, succeeded, failed, notes
    for home, ids in claude_ids.items():
        try:
            stopped([e for e in entries if e.home == home])
            def before_replace():
                stopped([e for e in entries if e.home == home])
                check_archive()
            trim_claude_history(home, ids, before_replace=before_replace)
        except Exception as exc:
            failed.append(("Claude 대화 인덱스", str(exc)))
    if aborted:
        notes.append("실행 상태 확인 실패 이후 프로젝트·작업 폴더를 보존함")
        return archive, succeeded, failed, notes
    # Project selection means conversation grouping; original cwd is never mutated.
    # Container deletion only covers Claude/Gemini's app-owned folders, after all sessions succeeded.
    good = {e.key for e in succeeded}
    for group in project_groups:
        members = [e for e in entries if e.group == group and e.kind == "conversation"]
        if members and all(e.key in good for e in members) and members[0].provider in {"Claude", "Gemini"}:
            e = members[0]
            folder = Path(e.extra["project_dir"])
            try:
                stopped(members)
                _no_links(folder)
                validate_project_folder(e, folder)
                remaining = list(folder.glob("*.jsonl")) if e.provider == "Claude" else list((folder / "chats").glob("*.json"))
                if remaining:
                    raise RuntimeError("선택에 없던 대화가 남아 프로젝트 폴더는 보존함")
                if e.provider == "Claude" and folder.exists():
                    # Claude keeps per-project memory (memory/) beside the transcripts: never remove it with chats.
                    leftovers = [p.name for p in folder.iterdir()]
                    if leftovers:
                        notes.append(f"{Path(e.project).name or e.project}: 프로젝트 메모리 등 보존 ({', '.join(leftovers[:3])})")
                        continue
                if e.provider == "Gemini" and folder.exists():
                    if any(p.name not in {"chats", ".project_root"} for p in folder.iterdir()) or (
                            (folder / "chats").exists() and any((folder / "chats").iterdir())):
                        notes.append(f"{e.project}: 프로젝트의 나머지 파일 보존")
                        continue
                if folder.exists():
                    if _project_state(folder, e.home, entries, removed_paths) != project_states[group]:
                        raise ValueError("앱 프로젝트의 파일이 변경되어 폴더를 보존함")
                    list(_checked_files(folder, e.home))
                    check_archive()
                    recycler(folder)
            except Exception as exc:
                failed.append(("앱 프로젝트 폴더", str(exc)))
    if aborted:
        notes.append("실행 상태 확인 실패 이후 작업 폴더를 보존함")
        return archive, succeeded, failed, notes
    # A work folder goes only after every chat that worked in it is gone, and never with a backup of its own.
    good = {e.key for e in succeeded}
    for path in workspaces:
        try:
            if any(e.key not in good for e in conversations_in(path, everything)):
                raise RuntimeError("이 폴더의 대화 삭제가 모두 성공하지 않아 작업 폴더는 보존함")
            stopped(conversations_in(path, everything), workspace=True)
            reason = fixed_problem(path, homes)
            if reason:
                raise ValueError(reason)
            if _target_state(Path(path), Path(path).parent) != workspace_states[path]:
                raise ValueError("작업 폴더가 변경되어 보존함")
            check_archive()
            recycler(Path(path))
            notes.append(f"작업 폴더 휴지통 이동: {path}")
        except Exception as exc:
            failed.append((f"작업 폴더 {path}", str(exc)))
    # Codex has no official command to unregister a project; its database is never edited directly.
    if any(e.provider == "Codex" and e.extra.get("project_id") and e.group in project_groups for e in succeeded):
        notes.append("Codex 프로젝트 등록은 보존함. 필요하면 Codex 앱에서 프로젝트를 제거")
    return archive, succeeded, failed, notes


def list_backups(folder: Path = DEFAULT_BACKUP_DIR):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    rows = [(p, p.stat().st_size, p.stat().st_mtime) for p in folder.glob(BACKUP_PATTERN) if p.is_file() and not is_link(p)]
    return sorted(rows, key=lambda r: r[2], reverse=True)


@_exclusive
def remove_backups(paths, folder: Path = DEFAULT_BACKUP_DIR):
    """Permanently delete backup ZIPs this program created. Nothing else is accepted."""
    paths = list(map(Path, paths))
    _no_links(folder)
    root = Path(folder).resolve()
    removed = []
    states = {}
    for p in paths:
        _no_links(p)
        if is_link(p) or not p.is_file() or p.resolve().parent != root or not p.match(BACKUP_PATTERN):
            raise ValueError(f"이 프로그램의 백업 파일이 아님: {p}")
        states[p] = _identity(p)
    for p in paths:
        _no_links(p)
        if p.resolve().parent != root or _identity(p) != states[p]:
            raise ValueError("백업 파일이 변경되어 삭제하지 않음")
        p.unlink()
        removed.append(p)
    return removed
