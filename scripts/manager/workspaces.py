"""Work folders (conversation cwd) that the user explicitly marks for deletion together with their chats."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .model import is_link, tree_files
from .providers import profile_roots
from .runtime import command

PROGRAM_ROOT = Path(__file__).resolve().parents[2]
PERSONAL = {"desktop", "documents", "downloads", "pictures", "music", "videos", "favorites", "contacts",
            "links", "saved games", "searches", "3d objects", "appdata", "바탕 화면", "문서", "다운로드", "사진", "음악", "동영상"}


def key(path):
    return os.path.normcase(os.path.abspath(str(path)))


def under(child, parent):
    c, p = key(child), key(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def conversations_in(path, entries):
    """Every conversation that worked in this folder or one of its subfolders."""
    return [e for e in entries if e.kind == "conversation" and Path(e.project).is_absolute() and under(e.project, path)]


def ai_homes(entries=()):
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    homes = {e.home for e in entries} | {p for _, p in profile_roots()} | {roaming / "orca"}
    home = Path.home()
    return homes | {home / ".codex", home / ".claude", home / ".gemini"}


def fixed_problem(path, homes=()):
    """Why this folder can never be deleted as a work folder, or ''."""
    p = Path(path)
    if not p.is_absolute():
        return "절대 경로가 아님"
    if not p.is_dir():
        return "폴더가 없음"
    if is_link(p) or key(p.resolve()) != key(p):
        return "링크·junction 경로"
    if key(p) == key(p.anchor):
        return "드라이브 루트"
    user = Path.home()
    if under(user, p):
        return "사용자 폴더 또는 그 상위 폴더"
    name, parent = p.name.casefold(), p.parent
    if key(parent) == key(user) and (name in PERSONAL or name.startswith("onedrive")):
        return "Windows 기본 개인 폴더"
    if parent.name.casefold().startswith("onedrive") and key(parent.parent) == key(user) and name in PERSONAL:
        return "Windows 기본 개인 폴더"
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "APPDATA", "LOCALAPPDATA", "TEMP"):
        value = os.environ.get(var)
        if value and under(value, p):
            return "시스템·앱 데이터 폴더이거나 그 상위 폴더"
    for value in (os.environ.get("SystemRoot"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                  os.environ.get("ProgramData"), str(Path(os.environ.get("LOCALAPPDATA", user)) / "Programs")):
        if value and under(p, value):
            return "설치된 프로그램·시스템 폴더 안"
    for h in homes:
        if under(p, h) or under(h, p):
            return "AI 앱 저장소와 겹침"
    if under(PROGRAM_ROOT, p):
        return "이 정리 프로그램이 들어 있는 폴더"
    return ""


def workspace_problem(path, entries, selected_keys, homes):
    reason = fixed_problem(path, homes)
    if reason:
        return reason
    left = [e for e in conversations_in(path, entries) if e.key not in selected_keys]
    if left:
        return (f"이 폴더(하위 포함)에서 작업한 대화 {len(left)}개가 선택되지 않음 "
                f"(예: [{left[0].provider}] {' '.join(left[0].title.split())[:40]})")
    return ""


def git_warnings(path):
    p = Path(path)
    if not (p / ".git").exists():
        return []
    try:
        out = command("git", ["-C", str(p), "status", "--porcelain=v1", "-b"], timeout=20)
    except Exception as exc:
        return [f"git 상태 확인 실패: {str(exc)[:120]}"]
    lines = out.splitlines()
    head = lines[0] if lines and lines[0].startswith("## ") else ""
    warnings = []
    changed = [line for line in lines if not line.startswith("## ")]
    if changed:
        warnings.append(f"커밋하지 않은 변경 {len(changed)}개")
    ahead = re.search(r"ahead (\d+)", head)
    if ahead:
        warnings.append(f"push하지 않은 커밋 {ahead.group(1)}개")
    if "..." not in head:
        warnings.append("원격 저장소(upstream)가 없어 push된 사본이 없을 수 있음")
    if (p / ".git").is_file():
        # A linked worktree: deleting it leaves only stale metadata in the main repository.
        warnings.append("git worktree임: 삭제 후 원본 저장소에서 'git worktree prune' 필요")
        return warnings
    try:
        listed = command("git", ["-C", str(p), "worktree", "list", "--porcelain"], timeout=20)
        count = sum(line.startswith("worktree ") for line in listed.splitlines())
        if count > 1:
            warnings.append(f"이 저장소를 쓰는 다른 git worktree {count - 1}개가 함께 망가짐")
    except Exception:
        pass
    return warnings


def plan_workspaces(paths, entries, selected_keys, orca_paths=()):
    """Size, git state and blocking reason for each marked work folder. Read-only."""
    homes = ai_homes(entries)
    plan = []
    for path in sorted(paths, key=key):
        item = {"path": path, "size": 0, "files": 0, "warnings": [],
                "problem": workspace_problem(path, entries, selected_keys, homes)}
        if not item["problem"]:
            for f in tree_files(Path(path)):
                try:
                    item["size"] += f.stat().st_size
                    item["files"] += 1
                except OSError:
                    pass
            item["warnings"] = git_warnings(path)
            for repo in orca_paths:
                if repo and under(repo, path):
                    item["warnings"].append(f"Orca에 등록된 프로젝트 포함: {repo} (Orca에서 등록 해제 필요)")
            if under(path, Path.home() / "orca" / "workspaces"):
                item["warnings"].append("Orca가 만든 워크스페이스: Orca에서 워크스페이스를 삭제하는 편이 안전함")
        plan.append(item)
    return plan
