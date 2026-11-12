"""Project disk usage. Scanning/reporting never deletes files."""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat

from .model import is_link, readable_size

# Default: the folder that contains this app; override with AGENTS_CLEANER_SIZE_ROOT.
DEFAULT_ROOT = Path(os.environ.get("AGENTS_CLEANER_SIZE_ROOT") or Path(__file__).resolve().parents[3])
LARGE = 500 * 1024 * 1024
CACHES = {"node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt", "build", "dist", "target", ".cache"}
DATA_DIRS = {"dataset", "datasets", "data", ".git"}
BULK = {"uploads", "upload", "output", "outputs", "temp", "tmp"}
SOURCE = {".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".c", ".cpp", ".h", ".java", ".cs", ".go", ".swift", ".kt", ".sql"}


def settings_path():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Agents_Cleaner/size-roots.json"


def configured_roots():
    try:
        data = json.loads(settings_path().read_text(encoding="utf8"))
        return [Path(x) for x in data["roots"] if isinstance(x, str)] or [DEFAULT_ROOT]
    except (OSError, ValueError, KeyError, TypeError):
        return [DEFAULT_ROOT]


def save_roots(roots):
    target = settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"roots": [str(x) for x in roots]}, ensure_ascii=False, indent=2), encoding="utf8")


def protected(path):
    name = path.name.lower()
    return name in DATA_DIRS or name.startswith(("toxauto-", "dataset")) or name == ".env" or name.startswith(".env.") or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key"} or name.endswith((".db-wal", ".db-shm"))


@dataclass
class Candidate:
    path: Path
    size: int
    kind: str
    fingerprint: str
    blocked: str = ""


@dataclass
class Project:
    path: Path
    size: int = 0
    candidates: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def reclaimable(self):
        return sum(x.size for x in self.candidates if not x.blocked)


def measure(path, errors):
    """No junction/symlink traversal, including under candidate directories."""
    digest = hashlib.sha256()
    size, blocked = 0, ""
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            if is_link(current):
                errors.append(str(current) + ": 링크/접근 불가 제외")
                blocked = "링크/junction 포함"
                continue
            info = current.lstat()
            digest.update(f"{current}|{info.st_ino}|{info.st_mtime_ns}|{info.st_size}".encode("utf8"))
            if protected(current):
                blocked = "보호 항목 포함 (.git/설정/DB/데이터)"
                if current.name.lower().startswith("toxauto-"):
                    continue
            if stat.S_ISDIR(info.st_mode):
                pending.extend(sorted(current.iterdir(), key=lambda x: x.name))
            elif stat.S_ISREG(info.st_mode):
                size += info.st_size
        except OSError:
            errors.append(str(current))
            blocked = "접근/측정 실패"
    return size, digest.hexdigest(), blocked


def cache_name(name):
    name = name.lower()
    return name in CACHES or name.startswith((".venv", "venv"))


def scan_project(path, large=LARGE):
    project = Project(path)
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            if is_link(current):
                project.errors.append(str(current) + ": 링크/접근 불가 제외")
                continue
            if current != path and current.name.lower().startswith("toxauto-"):
                continue
            info = current.lstat()
            if stat.S_ISDIR(info.st_mode):
                name = current.name.lower()
                if current != path and (cache_name(name) or name in BULK):
                    size, fingerprint, blocked = measure(current, project.errors)
                    project.size += size
                    if cache_name(name) or size > large:
                        project.candidates.append(Candidate(current, size, "캐시" if cache_name(name) else "대용량 폴더", fingerprint, blocked))
                    continue  # never double count nested candidates
                if name == ".git" or protected(current):
                    size, _, _ = measure(current, project.errors)
                    project.size += size
                    continue
                pending.extend(sorted(current.iterdir(), key=lambda x: x.name))
            elif stat.S_ISREG(info.st_mode):
                project.size += info.st_size
                if info.st_size > large:
                    _, fingerprint, blocked = measure(current, project.errors)
                    if current.suffix.lower() in SOURCE:
                        blocked = "소스 파일 보존"
                    project.candidates.append(Candidate(current, info.st_size, "대용량 파일", fingerprint, blocked))
        except OSError:
            project.errors.append(str(current))
    project.candidates.sort(key=lambda x: x.size, reverse=True)
    return project


def scan_roots(roots=None, large=LARGE):
    projects, errors, seen = [], [], set()
    for root in roots or configured_roots():
        root = Path(root).absolute()
        try:
            if root.name.lower().startswith("toxauto-"):
                errors.append(str(root) + ": 제외 대상")
                continue
            if any(is_link(p) for p in (root, *root.parents)):
                errors.append(str(root) + ": 링크/junction 제외")
                continue
            for project in root.iterdir():
                if project.name.lower().startswith("toxauto-") or not project.is_dir() or is_link(project):
                    continue
                resolved = project.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                projects.append(scan_project(project, large))
        except OSError:
            errors.append(str(root) + ": 루트 접근 실패")
    projects.sort(key=lambda x: x.size, reverse=True)
    return projects, errors


def recycle_candidate(candidate, project):
    """Only called by the GUI after checkbox selection and explicit confirmation."""
    from .recycle import recycle
    path = candidate.path.absolute()
    base = project.path.absolute()
    if candidate.blocked or path == base or not path.is_relative_to(base):
        raise ValueError("허용된 프로젝트 정리 대상 아님")
    if any(is_link(p) or protected(p) for p in (path, *path.parents) if p == base or p.is_relative_to(base)):
        raise ValueError("보호/링크 경로")
    fresh = scan_project(base)
    match = next((c for c in fresh.candidates if c.path == path), None)
    if not match or match.blocked or (match.size, match.fingerprint) != (candidate.size, candidate.fingerprint):
        raise ValueError("조회 후 대상 변경됨. 다시 스캔 필요")
    recycle(path)


def write_report(projects, errors, target):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")
    lines = [f"# 프로젝트 용량 {datetime.now().isoformat(timespec='seconds')}",
             "조회 전용. 자동 삭제 없음. 보호/접근 실패 항목은 회수 가능량에서 제외.",
             f"\n총 용량: {readable_size(sum(p.size for p in projects))}",
             f"회수 가능 추정: {readable_size(sum(p.reclaimable for p in projects))}", "",
             "| 프로젝트 | 전체 | 회수 가능 |", "|---|---:|---:|"]
    for p in projects:
        lines.append(f"| {cell(p.path)} | {readable_size(p.size)} | {readable_size(p.reclaimable)} |")
    for p in projects:
        if p.candidates:
            lines.extend(["", "## " + cell(p.path.name)])
            lines.extend(f"- {cell(c.path)}: {readable_size(c.size)} ({c.blocked or c.kind})" for c in p.candidates)
    all_errors = errors + [e for p in projects for e in p.errors]
    if all_errors:
        lines.extend(["", "## 측정 누락 (실제 용량은 더 클 수 있음)", *["- " + cell(e) for e in all_errors]])
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf8")
    return target


def notify_report(projects, target):
    import sys
    # Optional external notifier: a folder whose worker_b_common.py exposes notify(text).
    module_dir = os.environ.get("AGENTS_CLEANER_NOTIFIER_DIR")
    if not module_dir:
        print("[알림 시뮬레이션] AGENTS_CLEANER_NOTIFIER_DIR 미설정. 보고서는 파일에 저장됨.")
        return False
    sys.path.insert(0, module_dir)
    from worker_b_common import notify
    top = sorted(projects, key=lambda p: p.reclaimable, reverse=True)[:10]
    notify("프로젝트 주간 용량 (조회 전용)\n회수 가능 " + readable_size(sum(p.reclaimable for p in projects)) + "\n" +
           "\n".join(f"{p.path.name}: {readable_size(p.size)} / 회수 {readable_size(p.reclaimable)}" for p in top) + "\n" + str(target))
