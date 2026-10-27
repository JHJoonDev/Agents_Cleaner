from __future__ import annotations

import hashlib
import json
import os
import sqlite3
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
        return path.is_symlink() or bool(path.lstat().st_file_attributes & 0x400)
    except AttributeError:
        return path.is_symlink()
    except OSError:
        return True


def inside(path: Path, root: Path):
    try:
        path.resolve().relative_to(root.resolve())
        return path.resolve() != root.resolve()
    except (ValueError, OSError):
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
    total = 0
    for p in tree_files(path):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def signature(entry: Entry):
    rows = []
    for path in entry.files:
        safe_path(path, entry.home)
        for p in tree_files(path):
            s = p.stat()
            rows.append((str(p), s.st_size, s.st_mtime_ns))
    return hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest()


def read_json(path: Path, limit=64 * 1024 * 1024):
    if path.stat().st_size > limit:
        raise ValueError(f"JSON 크기 한도 초과: {path.name}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def read_db(path: Path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def readable_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024
