"""Reproduce UI captures without invoking either deletion backend.

py -3.11 tests/capture_qt.py [--real-read-only]
Fake profiles are temporary. The optional real capture is strictly read-only.
"""
import argparse
import gc
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from PySide6.QtWidgets import QApplication
from manager.model import ClosingConnection
from manager.qt_dialogs import ReviewDialog
from manager.qt_ui import App


def wait(app, window, condition=lambda: True):
    until = time.monotonic() + 45
    while time.monotonic() < until:
        app.processEvents()
        if not window.busy and condition():
            return
        time.sleep(.015)
    raise RuntimeError("Capture scan timed out")


def settle(app):
    for _ in range(8):
        app.processEvents()
        time.sleep(.015)


def fixtures(root):
    project = root / "Orbit Notes"
    project.mkdir()
    (project / "sample.py").write_text("fixture only", encoding="utf8")
    subprocess.run(["git", "init", "-q", str(project)], capture_output=True, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    codex = root / "codex"
    (codex / "sessions").mkdir(parents=True)
    with sqlite3.connect(codex / "state_5.sqlite", factory=ClosingConnection) as db:
        db.execute("create table threads(id text,title text,cwd text,updated_at integer,rollout_path text)")
        for i, (title, kb) in enumerate([("Search layout review", 96), ("Component example cleanup", 68)]):
            sid = str(uuid.uuid4())
            path = codex / "sessions" / ("rollout-" + sid + ".jsonl")
            path.write_text(json.dumps({"fixture": title}) + "\n", encoding="utf8")
            with path.open("ab") as stream:
                stream.truncate(kb * 1024)
            db.execute("insert into threads values(?,?,?,?,?)", (sid, title, str(project), 1793148000 - i * 86400, str(path)))
    (codex / "cache").mkdir()
    (codex / "cache" / "diagnostic.log").write_bytes(b"x" * (320 * 1024))
    claude = root / "claude"
    folder = claude / "projects" / "atlas"
    folder.mkdir(parents=True)
    atlas = root / "Atlas Demo"
    atlas.mkdir()
    (folder / (str(uuid.uuid4()) + ".jsonl")).write_text(json.dumps({"cwd": str(atlas), "type": "user", "message": {"content": "Demo data format discussion"}}) + "\n", encoding="utf8")
    gemini = root / "gemini"
    folder = gemini / "tmp" / "sample"
    (folder / "chats").mkdir(parents=True)
    protected = gemini / "System Templates"
    protected.mkdir()
    (folder / ".project_root").write_text(str(protected), encoding="utf8")
    (folder / "chats" / "session-sample.json").write_text(json.dumps({"sessionId": str(uuid.uuid4()), "summary": "Example copy draft", "messages": []}), encoding="utf8")
    backups = root / "backups"
    backups.mkdir()
    for day in ["28", "24", "18"]:
        with zipfile.ZipFile(backups / ("ai-records-2026-10-" + day + "-demo.zip"), "w") as archive:
            archive.writestr("fixture.txt", "Example backup only.\n" * 1000)
    return [("Codex", codex), ("Claude", claude), ("Gemini", gemini)], backups


def capture_fake(app, output):
    with tempfile.TemporaryDirectory(prefix="ai-qt-capture-") as temp:
        root = Path(temp)
        roots, backups = fixtures(root)
        processes = [{"app": "Claude", "name": "claude.exe", "pid": 8120, "path": str(root / "claude.exe")}]
        with patch("manager.qt_logic.running_processes", return_value=processes), patch("manager.qt_logic.codex_cli", return_value=("fixture-codex.exe", "0.0.0")):
            window = App(roots=roots, auto_refresh=False)
            window.backup_dir = backups
            window.show()
            window.refresh()
            wait(app, window)
            window.orca = [{"name": "Orca sample registration", "path": str(root / "registered"), "id": "fixture"}]
            window.render()
            orbit = next(e.group for e in window.entries if e.kind == "conversation" and e.provider == "Codex")
            window.toggle_workspace(orbit)
            caches = [e for e in window.entries if e.kind == "cache"]
            window.set_records_checked(caches, True, caches[0].group)
            window.tree.expandAll()
            e = next(e for e in window.entries if e.provider == "Codex" and e.kind == "conversation")
            window.tree.setCurrentIndex(window.proxy.mapFromSource(window.model.record_items[e.key][0].index()))
            settle(app)
            window.grab().save(str(output / "qt-main.png"))
            # Open the review flow directly; never press a delete/review button.
            window.review()
            wait(app, window, lambda: any(isinstance(d, ReviewDialog) for d in window.dialogs))
            settle(app)
            dialog = next(d for d in window.dialogs if isinstance(d, ReviewDialog))
            assert not dialog.confirm_button.isEnabled()
            dialog.grab().save(str(output / "qt-confirm.png"))
            dialog.reject()
            settle(app)
            dialog = window.manage_backups()
            settle(app)
            dialog.grab().save(str(output / "qt-backups.png"))
            dialog.reject()
            window.close()
            window.deleteLater()
            app.processEvents()
            gc.collect()
    assert not root.exists(), "Temporary profiles must be removed"
    print("Fake main/confirm/backups captured; temporary profiles removed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-read-only", action="store_true")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    output = ROOT / "design"
    output.mkdir(exist_ok=True)
    with patch("manager.operations.delete") as no_delete, patch("manager.operations.remove_backups") as no_remove:
        capture_fake(app, output)
        if args.real_read_only:
            window = App(allow_delete=False, auto_refresh=False)
            window.show()
            window.refresh()
            wait(app, window)
            settle(app)
            assert not window.allow_delete and not window.review_button.isEnabled()
            window.grab().save(str(output / "qt-main-real.png"))
            print("Real read-only capture:", len(window.entries), "records; delete disabled")
            window.close()
            window.deleteLater()
            app.processEvents()
        no_delete.assert_not_called()
        no_remove.assert_not_called()
    print("Deletion backend calls during captures: 0")


if __name__ == "__main__":
    main()
