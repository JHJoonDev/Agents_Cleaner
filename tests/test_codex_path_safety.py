"""Native deletion must use the rollout path captured by the inventory."""
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager.model import ClosingConnection, signature
from manager.operations import delete
from manager.providers import scan


class CodexPathSafetyTests(unittest.TestCase):
    def test_changed_database_rollout_is_blocked_before_native_delete(self):
        for replacement in ("outside", "inside", "missing", "cleared"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                home = root / "profile"
                sessions = home / "sessions"
                sessions.mkdir(parents=True)
                original = sessions / "original.jsonl"
                original.write_text("original", encoding="utf8")
                other = (sessions if replacement == "inside" else root) / "other.jsonl"
                other.write_text("preserve", encoding="utf8")
                database = home / "state_5.sqlite"
                sid = str(uuid.uuid4())
                with sqlite3.connect(database, factory=ClosingConnection) as db:
                    db.execute("create table threads(id text, cwd text, title text, updated_at integer, rollout_path text)")
                    db.execute("insert into threads values(?,?,?,?,?)", (sid, str(root), "fixture", 100, str(original)))
                entries, errors = scan([("Codex", home)], include_artifacts=False)
                self.assertFalse(errors)
                entry = entries[0]
                expected = {entry.key: signature(entry)}
                changed = None if replacement == "cleared" else str(other)
                if replacement == "missing":
                    changed = str(sessions / "not-created.jsonl")
                with sqlite3.connect(database, factory=ClosingConnection) as db:
                    db.execute("update threads set rollout_path=? where id=?", (changed, sid))
                runner = Mock(side_effect=RuntimeError("native deletion must not run"))
                with self.assertRaises(ValueError):
                    delete(entries, expected, detector=lambda: set(), runner=runner)
                runner.assert_not_called()
                self.assertEqual(other.read_text(encoding="utf8"), "preserve")
                self.assertTrue(original.exists())


if __name__ == "__main__":
    unittest.main()
