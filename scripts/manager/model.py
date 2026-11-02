from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Entry:
    provider: str
    home: Path
    session: str
    title: str
    project: str
    updated: float
    files: list[Path] = field(default_factory=list)
    kind: str = "conversation"
    extra: dict = field(default_factory=dict)
    blocked: str = ""

    @property
    def key(self):
        return hashlib.sha256(f"{self.provider}|{self.home}|{self.kind}|{self.session}".encode()).hexdigest()

    @property
    def group(self):
        # Project paths are labels, NEVER filesystem deletion targets.
        return (self.provider, str(self.home), self.project)

    @property
    def size(self):
        return sum(tree_size(p) for p in self.files)


def is_link(path: Path):
    try:
        # Reject Windows junctions as well as symbolic links.
        return _link_stat(path.lstat())
    except OSError:
        return True


def _link_stat(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def inside(path: Path, root: Path):
    try:
        target, base = path.resolve(), root.resolve()
        target.relative_to(base)
        return target != base
    except (ValueError, OSError, RuntimeError):
        return False


def safe_path(path: Path, root: Path):
    if not inside(path, root):
        raise ValueError(f"앱 저장소 밖의 경로 또는 루트 자체: {path}")
    cur = path.absolute()
    while True:
        if is_link(cur):
            raise ValueError(f"링크·junction은 정리하지 않음: {cur}")
        if cur == root.absolute():
            break
        if cur == cur.parent:
            raise ValueError("루트를 확인할 수 없음")
        cur = cur.parent
    return path


def tree_files(path: Path):
    if not path.exists() or is_link(path):
        return
    if path.is_file():
        yield path
        return
    for parent, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not is_link(Path(parent) / d)]
        for name in files:
            p = Path(parent) / name
            if not is_link(p):
                yield p


def tree_size(path: Path):
    return sum(info.st_size for _, info in _tree_stats(path))


def _tree_stats(path: Path, strict=False):
    """Use scandir metadata for display, but fresh lstat for deletion signatures."""
    try:
        info = path.lstat()
        if _link_stat(info):
            if strict:
                raise ValueError(f"링크·junction은 정리하지 않음: {path}")
            return
        if stat.S_ISREG(info.st_mode):
            yield path, info
            return
        if not stat.S_ISDIR(info.st_mode):
            if strict:
                raise ValueError(f"지원하지 않는 파일 형식: {path}")
            return
    except OSError:
        if strict:
            raise
        return
    pending = [path]
    while pending:
        folder = pending.pop()
        try:
            # Check again when visiting a queued directory; it may have been replaced.
            if is_link(folder):
                if strict:
                    raise ValueError(f"읽는 중 경로가 변경됨: {folder}")
                continue
            with os.scandir(folder) as children:
                for child in children:
                    try:
                        p = Path(child.path)
                        info = p.lstat() if strict else child.stat(follow_symlinks=False)
                        if _link_stat(info):
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(p)
                        elif stat.S_ISREG(info.st_mode):
                            yield p, info
                        elif strict:
                            raise ValueError(f"지원하지 않는 파일 형식: {p}")
                    except OSError:
                        if strict:
                            raise
        except OSError:
            if strict:
                raise


def signature(entry: Entry):
    # A native Codex delete may also remove a rollout that was missing at scan time.
    # Require refresh when either plain/compressed candidate newly appears.
    for candidate in entry.extra.get("rollout_candidates", ()):
        p = Path(candidate)
        try:
            info = p.lstat()
        except FileNotFoundError:
            continue
        if p not in entry.files or _link_stat(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError("스캔 이후 대화 파일이 변경됨. 새로고침 필요")
    rows = []
    for path in entry.files:
        safe_path(path, entry.home)
        for p, s in _tree_stats(path, strict=True):
            rows.append((str(p), s.st_size, s.st_mtime_ns))
    return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()


def read_json(path: Path, limit=64 * 1024 * 1024):
    # Bound the actual read too: a writer may grow the file after the size check.
    with path.open("rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        if size > limit:
            raise ValueError(f"JSON 크기 한도 초과: {path.name}")
        data = stream.read(size + 1)
    if len(data) > size:
        raise ValueError(f"읽는 중 JSON 크기가 변경됨: {path.name}")
    if len(data) > limit:
        raise ValueError(f"JSON 크기 한도 초과: {path.name}")
    return json.loads(data.decode("utf-8-sig"))


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def read_db(path: Path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2, factory=ClosingConnection)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
    except Exception:
        db.close()
        raise
    return db


def readable_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024
