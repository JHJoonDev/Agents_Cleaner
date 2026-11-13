"""Read-only discovery and conservative validation of disposable work artifacts."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import herdr
from .model import Entry, _link_stat, inside, is_link, safe_path

DAY = 86400
TIER1_DAYS = 7
TIER2_DAYS = 30
KEEP_BACKUPS = 3
MAX_DEPTH = 4
CATEGORIES = {
    "temp-claude": (1, "delete", ""), "temp-agent": (1, "delete", ""),
    "pycache": (1, "delete", ""),
    "deps-node": (2, "recycle", "npm install로 다시 설치"),
    "deps-venv": (2, "recycle", "py -3.11 -m venv .venv 후 pip install -r requirements.txt"),
    "build-target": (2, "recycle", "cargo build로 다시 빌드"),
    "build-dist": (2, "recycle", "프로젝트의 빌드 명령으로 다시 생성"),
    "herdr-runtime-backup": (3, "recycle", ""),
    "herdr-profile-backup": (3, "recycle", ""),
}
LIVE_UNKNOWN = "Herdr 실행 상태를 확인하지 못해 보존"
_ERRORS = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)


def temp_root():
    return Path(os.environ.get("TEMP") or tempfile.gettempdir()).absolute()


def backup_root():
    return Path(os.environ.get("USERPROFILE") or Path.home()) / ".config/herdr-app/data"


def live_cwds():
    # Kept as a wrapper so callers/tests can inject live state without importing UI code.
    detector = getattr(herdr, "live_cwds", None)
    return detector() if detector is not None else None


def _children(path):
    if not path.exists():
        return []
    if is_link(path):
        raise ValueError("링크·junction 경로는 탐색하지 않음")
    with os.scandir(path) as stream:
        return [(Path(c.path), c.stat(follow_symlinks=False)) for c in stream]


def _nodes(path, recursive=False):
    """Fresh metadata, refusing links (including links below a target)."""
    pending = [path]
    while pending:
        item = pending.pop()
        info = item.lstat()
        if _link_stat(info):
            raise ValueError("대상 안의 링크·junction은 정리하지 않음")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ValueError("지원하지 않는 파일 형식")
        yield item, info
        if stat.S_ISDIR(info.st_mode) and (recursive or item == path):
            pending.extend(p for p, _ in _children(item))


def _snapshot(path, recursive=False):
    rows, newest = [], 0
    for p, s in _nodes(path, recursive):
        newest = max(newest, s.st_mtime)
        rows.append((str(p), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode))
    digest = hashlib.sha256(repr(sorted(rows)).encode()).hexdigest()
    return newest, digest


def _within(path, root):
    return path.resolve() == root.resolve() or inside(path, root)


def _rootlike(path):
    return herdr.is_root_like(path)


def live_problem(entry, live):
    if live is None or not isinstance(live, (list, tuple)):
        return LIVE_UNKNOWN
    target = entry.files[0]
    project = Path(entry.project) if Path(entry.project).is_absolute() else None
    for value in live:
        cwd = Path(value)
        if not cwd.is_absolute():
            return LIVE_UNKNOWN
        if _rootlike(cwd):
            if _within(target, cwd) and len(target.resolve().relative_to(cwd.resolve()).parts) <= 1:
                return "실행 중인 Herdr 작업 경로라 보존"
            continue
        if _within(cwd, target) or (project is not None and _within(cwd, project)):
            return "실행 중인 Herdr 프로젝트·작업 경로라 보존"
    return ""


def process_owners(apps):
    """Resolve cwd ownership for standalone CLIs, which need not belong to Herdr."""
    if not set(apps).intersection({"Claude", "Codex"}):
        return []
    from . import runtime
    if runtime.psutil is None:
        return None
    try:
        owners = []
        for row in runtime.running_processes():
            if row["app"] not in {"Claude", "Codex"} or (row["app"] == "Claude" and runtime.is_claude_desktop(row)):
                continue
            try:
                owners.append(Path(runtime.psutil.Process(row["pid"]).cwd()))
            except runtime.psutil.NoSuchProcess:
                continue
        return owners
    except Exception:
        return None


def process_problem(entry, apps, owners=()):
    """Use session detail when available; unknown Claude/Codex ownership fails closed."""
    category = entry.extra["category"]
    if category == "temp-claude" and "Claude" in apps:
        if not hasattr(apps, "claude_sessions") or getattr(apps, "claude_all", False) \
                or entry.files[0].name in apps.claude_sessions:
            return "실행 중인 Claude 세션의 임시 폴더라 보존"
    if category == "temp-agent" and entry.files[0].name.startswith(("codex_", "codex-")) and "Codex" in apps:
        return "Codex가 실행 중이어서 임시 항목을 보존"
    if owners is None:
        return "Claude·Codex 실행 경로를 확인하지 못해 보존"
    if live_problem(entry, list(owners)):
        return "실행 중인 Claude·Codex 작업 경로라 보존"
    return ""


def _category(path):
    name = path.name
    if name == "node_modules":
        return "deps-node"
    if (name == "venv" or name.startswith(".venv")) and (path / "pyvenv.cfg").is_file():
        return "deps-venv"
    if name == "target" and ((path / "CACHEDIR.TAG").is_file() or (path.parent / "Cargo.toml").is_file()):
        return "build-target"
    if name in {"dist", "build"} and any((path.parent / n).is_file() for n in ("package.json", "pyproject.toml", "setup.py")):
        return "build-dist"
    if name in {"__pycache__", ".pytest_cache"}:
        return "pycache"
    return ""


def _git_problem(path, project):
    # Detect parent repositories too, including linked worktrees (.git is a file).
    if not any((p / ".git").exists() for p in (project, *project.parents)):
        return ""
    result = subprocess.run(["git", "--literal-pathspecs", "-C", str(project), "ls-files", "-z", "--",
                             path.relative_to(project).as_posix()], capture_output=True,
                            timeout=4, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        return "Git 추적 여부를 확인하지 못해 보존"
    return "tracked" if result.stdout else ""


def _backup_children(folder):
    return [p for p, s in _children(folder) if stat.S_ISDIR(s.st_mode) and not _link_stat(s)]


def _kept_backups(folder):
    return sorted(_backup_children(folder), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)[:KEEP_BACKUPS]


def validate_shape(entry):
    """Independent whitelist, never trust Entry.extra to choose a deletion root or method."""
    if entry.kind != "artifact" or entry.provider != "Artifacts" or len(entry.files) != 1:
        raise ValueError("지원하지 않는 작업 산출물")
    category = entry.extra.get("category")
    if category not in CATEGORIES:
        raise ValueError("허용되지 않은 산출물 종류")
    tier, method, _ = CATEGORIES[category]
    if entry.extra.get("tier") != tier or entry.extra.get("method") != method:
        raise ValueError("산출물 단계·삭제 방식 불일치")
    path, home = entry.files[0], entry.home
    safe_path(path, home)
    for p in (home, *home.parents):
        if is_link(p):
            raise ValueError("링크·junction 경로는 사용하지 않음")
    if category.startswith("temp-"):
        if home.resolve() != temp_root().resolve():
            raise ValueError("임시 폴더 루트 불일치")
        rel = path.relative_to(home)
        if category == "temp-agent":
            matched = len(rel.parts) == 1 and path.name.startswith(("codex_", "codex-", "herdr-jhj-", "ai-storage-db-"))
        else:
            try:
                matched = len(rel.parts) == 3 and rel.parts[0] == "claude" and str(uuid.UUID(path.name)) == path.name.lower() and path.is_dir()
            except ValueError:
                matched = False
    elif category.startswith("herdr-"):
        folder = "runtime-backups" if category == "herdr-runtime-backup" else "profile-backups"
        matched = home.resolve() == backup_root().resolve() and path.parent == home / folder and path.is_dir()
    else:
        from .workspaces import ai_homes, fixed_problem
        # Artifact homes are projects, never add these homes to ai_homes().
        matched = (Path(entry.project).is_absolute() and home.resolve() == Path(entry.project).resolve()
                   and not fixed_problem(home, ai_homes()) and path.is_dir() and _category(path) == category)
    if not matched:
        raise ValueError("산출물 이름·경로가 허용된 종류와 불일치")
    roots = list(entry.extra.get("protected_roots", ())) + list(entry.extra.get("protected_projects", ()))
    for root in roots:
        if _within(Path(root), path):
            raise ValueError("원본 프로젝트를 포함하는 산출물은 보존")


def revalidate(entry, live=None, now=None, apps=()):
    """Expensive full traversal belongs at mutation time, not at initial inventory."""
    validate_shape(entry)
    if entry.blocked:
        raise ValueError(entry.blocked)
    live = live_cwds() if live is None else live
    reason = live_problem(entry, live) or process_problem(entry, apps, process_owners(apps))
    if reason:
        raise ValueError(reason)
    now = time.time() if now is None else now
    path = entry.files[0]
    newest, _ = _snapshot(path, recursive=True)
    if newest >= now - DAY:
        raise ValueError("최근 24시간 안에 수정되어 보존")
    shallow = entry.extra["tier"] == 2
    _, state = _snapshot(path, recursive=not shallow)
    if state != entry.extra.get("scan_state"):
        raise ValueError("스캔 이후 산출물이 변경됨. 새로고침 필요")
    if entry.extra["tier"] == 1 and newest >= now - TIER1_DAYS * DAY:
        raise ValueError("7일 이상 지나지 않아 보존")
    if entry.extra["tier"] == 2:
        project = Path(entry.project)
        updated, _, problems = _project_targets(project)
        if problems:
            raise ValueError("프로젝트 일부를 읽지 못해 보존")
        if max(updated, entry.extra.get("project_updated", 0)) >= now - TIER2_DAYS * DAY:
            raise ValueError("프로젝트가 30일 이상 유휴 상태가 아니어서 보존")
    if entry.extra["category"] in {"pycache", "deps-node", "deps-venv", "build-target", "build-dist"}:
        reason = _git_problem(path, Path(entry.project))
        if reason:
            raise ValueError("Git 추적 파일을 포함해 보존" if reason == "tracked" else reason)
    if entry.extra["tier"] == 3 and path in _kept_backups(path.parent):
        raise ValueError("최근 백업 3개 보존")


def _project_targets(project):
    pending, found, errors = [(project, 0)], [], []
    newest = project.stat().st_mtime
    while pending:
        folder, depth = pending.pop()
        try:
            for path, info in _children(folder):
                if _link_stat(info):
                    continue
                category = _category(path) if stat.S_ISDIR(info.st_mode) else ""
                if category:
                    found.append((path, category))
                    continue
                newest = max(newest, info.st_mtime)
                if stat.S_ISDIR(info.st_mode) and depth + 1 < MAX_DEPTH and path.name not in {".git", ".hg", ".svn"}:
                    pending.append((path, depth + 1))
        except _ERRORS as exc:
            errors.append(str(exc))
    return newest, found, errors


def scan_artifacts(entries, errors, live=None, now=None):
    from .runtime import active_providers
    from .workspaces import ai_homes, fixed_problem
    now = time.time() if now is None else now
    live = live_cwds() if live is None else live
    result = []
    source = [e for e in entries if e.kind == "conversation" or e.provider == "Herdr"]
    homes = ai_homes(e for e in entries if e.kind != "artifact")
    projects, project_times = [], {}
    for value in [e.project for e in source] + list(live or ()):
        path = Path(value)
        try:
            if path.is_absolute() and not fixed_problem(path, homes):
                path = path.resolve()
                if path not in projects:
                    projects.append(path)
        except _ERRORS as exc:
            errors.append(f"Artifacts ({path}): {exc}")
    for e in source:
        if Path(e.project).is_absolute():
            project_times[Path(e.project).resolve()] = max(project_times.get(Path(e.project).resolve(), 0), e.updated)
    roots = []
    for project in sorted(projects, key=lambda p: len(p.parts)):
        if not any(_within(project, root) for root in roots):
            roots.append(project)
    try:
        apps = active_providers()
        owners = process_owners(apps)
    except Exception:
        apps, owners = None, None

    def add(path, home, category, project="(공용 작업 산출물)", project_updated=0, blocked=""):
        tier, method, hint = CATEGORIES[category]
        try:
            safe_path(path, home)
            updated, state = _snapshot(path, recursive=tier != 2)
            entry = Entry("Artifacts", home, f"artifact:{category}:{hashlib.sha256(str(path).encode()).hexdigest()[:20]}",
                          path.name, str(project), updated, [path], "artifact",
                          {"tier": tier, "category": category, "method": method, "reinstall": hint,
                           "idle_days": max(0, int((now - updated) // DAY)), "scan_state": state,
                           "project_updated": project_updated, "protected_roots": [str(p) for p in projects]})
            reason = blocked or live_problem(entry, live)
            if not reason and any(_within(p, path) for p in projects):
                reason = "원본 프로젝트를 포함하는 산출물은 보존"
            if not reason:
                reason = "실행 프로세스를 확인하지 못해 보존" if apps is None else process_problem(entry, apps, owners)
            if not reason and updated >= now - DAY:
                reason = "최근 24시간 안에 수정되어 보존"
            if not reason and tier == 1 and updated >= now - TIER1_DAYS * DAY:
                reason = "7일 이상 지나지 않아 보존"
            if not reason and tier == 2 and project_updated >= now - TIER2_DAYS * DAY:
                reason = "프로젝트가 30일 이상 유휴 상태가 아니어서 보존"
            entry.blocked = reason
            result.append(entry)
        except _ERRORS as exc:
            errors.append(f"Artifacts ({path}): {exc}")

    tmp = temp_root()
    try:
        for path, info in _children(tmp):
            if not _link_stat(info) and path.name.startswith(("codex_", "codex-", "herdr-jhj-", "ai-storage-db-")):
                add(path, tmp, "temp-agent")
        for hashed, info in _children(tmp / "claude"):
            if _link_stat(info) or not stat.S_ISDIR(info.st_mode):
                continue
            for path, info in _children(hashed):
                try:
                    matched = str(uuid.UUID(path.name)) == path.name.lower()
                except ValueError:
                    matched = False
                if matched and stat.S_ISDIR(info.st_mode) and not _link_stat(info):
                    add(path, tmp, "temp-claude")
    except _ERRORS as exc:
        errors.append(f"Artifacts ({tmp}): {exc}")
    for project in roots:
        try:
            updated, targets, problems = _project_targets(project)
            updated = max([updated] + [t for p, t in project_times.items() if _within(p, project)])
            for path, category in targets:
                try:
                    reason = _git_problem(path, project)
                except _ERRORS:
                    reason = "Git 추적 여부를 확인하지 못해 보존"
                if reason == "tracked":
                    continue
                add(path, project, category, project, updated,
                    reason or ("프로젝트 일부를 읽지 못해 보존" if problems else ""))
        except _ERRORS as exc:
            errors.append(f"Artifacts ({project}): {exc}")
    home = backup_root()
    for folder, category in ((home / "runtime-backups", "herdr-runtime-backup"),
                             (home / "profile-backups", "herdr-profile-backup")):
        try:
            kept = _kept_backups(folder)
            for path in _backup_children(folder):
                add(path, home, category, blocked="최근 백업 3개 보존" if path in kept else "")
        except _ERRORS as exc:
            errors.append(f"Artifacts ({folder}): {exc}")
    return result
