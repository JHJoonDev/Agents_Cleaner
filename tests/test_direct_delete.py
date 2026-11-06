"""Default deletion must never depend on backup storage. Fixture files only."""
import json
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager.model import ClosingConnection, signature
from manager.operations import delete
from manager.providers import scan


class DirectDeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "agent"
        self.home.mkdir()
        self.sid = str(uuid.uuid4())
        self.original = self.root / "project"
        self.original.mkdir()
        (self.original / "source.py").write_text("preserve", encoding="utf8")

    def entry(self, provider):
        if provider == "Claude":
            path = self.home / "projects/fixture" / f"{self.sid}.jsonl"
            row = {"sessionId": self.sid, "cwd": str(self.original), "type": "user",
                   "message": {"content": "fixture"}}
        elif provider == "Gemini":
            path = self.home / "tmp/fixture/chats/session-fixture.json"
            row = {"sessionId": self.sid, "messages": []}
        else:
            path = self.home / "sessions" / f"rollout-{self.sid}.jsonl"
            row = {}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row), encoding="utf8")
        if provider == "Codex":
            with sqlite3.connect(self.home / "state_5.sqlite", factory=ClosingConnection) as db:
                db.execute("create table threads(id text, cwd text, title text, updated_at integer, rollout_path text)")
                db.execute("insert into threads values(?,?,?,?,?)",
                           (self.sid, str(self.original), "fixture", 100, str(path)))
        entries, errors = scan([(provider, self.home)])
        self.assertFalse(errors)
        return entries[0]

    def test_default_recycles_selected_claude_and_gemini_without_backup(self):
        for provider in ("Claude", "Gemini"):
            with self.subTest(provider=provider):
                entry = self.entry(provider)
                expected = {entry.key: signature(entry)}
                recycler = Mock(side_effect=lambda path: path.unlink())
                with patch("manager.operations.backup", side_effect=OSError("no backup disk")) as backup:
                    archive, good, failed, _ = delete([entry], expected, detector=lambda: set(), recycler=recycler)
                backup.assert_not_called()
                self.assertIsNone(archive)
                self.assertEqual(good, [entry])
                self.assertFalse(failed)
                recycler.assert_called_once_with(entry.files[0])
        self.assertTrue((self.original / "source.py").exists())

    def test_codex_calls_official_delete_without_snapshot(self):
        entry = self.entry("Codex")
        def native(exe, args, **options):
            self.assertEqual(exe, "codex")
            self.assertEqual(args, ["--no-daemon", "delete", self.sid, "--force"])
            self.assertEqual(options["env"]["CODEX_HOME"], str(self.home))
            with sqlite3.connect(self.home / "state_5.sqlite", factory=ClosingConnection) as db:
                db.execute("delete from threads where id=?", (self.sid,))
            entry.files[0].unlink()
        with patch("manager.operations.backup", side_effect=AssertionError("unexpected backup")) as backup, \
                patch("manager.operations._snapshot_database", side_effect=AssertionError("unexpected snapshot")) as snapshot:
            archive, good, failed, _ = delete([entry], {entry.key: signature(entry)},
                                              detector=lambda: set(), runner=native)
        self.assertIsNone(archive)
        self.assertEqual(good, [entry])
        self.assertFalse(failed)
        backup.assert_not_called()
        snapshot.assert_not_called()

    def test_unused_backup_destination_is_never_opened(self):
        entry = self.entry("Claude")
        destination = self.root / "not-a-directory"
        destination.write_text("preserve", encoding="utf8")
        archive, good, failed, _ = delete([entry], {entry.key: signature(entry)}, destination,
                                          detector=lambda: set(), recycler=lambda p: p.unlink())
        self.assertIsNone(archive)
        self.assertEqual(good, [entry])
        self.assertFalse(failed)
        self.assertEqual(destination.read_text(), "preserve")

    def test_restart_before_mutation_preserves_record_without_backup(self):
        entry = self.entry("Claude")
        checks = iter([set(), {"Claude"}])
        recycler = Mock()
        archive, good, failed, _ = delete([entry], {entry.key: signature(entry)},
                                          detector=lambda: next(checks), recycler=recycler)
        self.assertIsNone(archive)
        self.assertFalse(good)
        self.assertTrue(failed)
        recycler.assert_not_called()
        self.assertTrue(entry.files[0].exists())

    def test_changed_record_is_rejected_without_backup(self):
        entry = self.entry("Claude")
        expected = {entry.key: signature(entry)}
        entry.files[0].write_text("changed", encoding="utf8")
        recycler = Mock()
        with self.assertRaisesRegex(ValueError, "새로고침"):
            delete([entry], expected, detector=lambda: set(), recycler=recycler)
        recycler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
