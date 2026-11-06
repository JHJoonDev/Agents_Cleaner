import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager.herdr import protected_homes
from manager.model import Entry, signature
from manager.operations import _stopped, delete, validate
from manager.providers import scan
from manager.runtime import _process_app, entry_running
from manager.workspaces import fixed_problem


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "herdr-core/default"
        self.home.mkdir(parents=True)
        self.state = self.home / "terminals.json"
        self.state.write_text(json.dumps({"terminals": {"1": {"address": "t1",
            "title": "fixture", "cwd": str(self.base / "project"), "state": "exited",
            "created_ms": 1794000000000, "screen": str(self.base / "outside"),
            "restart_env": {"TOKEN": "must not be exposed"}}}}), encoding="utf8")
        for name in ("core.log", "holder.log", "core.json", "core.lock", "docs.json"):
            (self.home / name).write_text("fixture", encoding="utf8")
        (self.home / "screens").mkdir()
        (self.home / "screens/t1.snap").write_text("preserve", encoding="utf8")

    def test_inventory_is_read_only_and_limits_cleanup_to_logs(self):
        before = {p: p.read_bytes() for p in self.home.rglob("*") if p.is_file()}
        entries, errors = scan([("Herdr", self.home)])
        self.assertFalse(errors)
        terminal = next(e for e in entries if e.kind == "conversation")
        self.assertTrue(terminal.blocked)
        self.assertEqual(terminal.files, [])
        self.assertNotIn("TOKEN", repr(terminal))
        self.assertEqual({e.session for e in entries if e.kind == "cache"},
                         {"cache:core.log", "cache:holder.log"})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_native_terminal_cannot_be_deleted_even_if_unblocked(self):
        entry = next(e for e in scan([("Herdr", self.home)])[0] if e.kind == "conversation")
        entry.blocked = ""
        with self.assertRaises(ValueError):
            validate(entry)

    def test_only_selected_log_is_recycled_without_backup(self):
        entry = next(e for e in scan([("Herdr", self.home)])[0] if e.session == "cache:core.log")
        recycled = []
        def recycle(path):
            recycled.append(path)
            path.unlink()  # Fixture only.
        archive, good, failed, _ = delete([entry], {entry.key: signature(entry)}, self.base / "backup",
                                          detector=lambda: set(), recycler=recycle)
        self.assertIsNone(archive)
        self.assertFalse((self.base / "backup").exists())
        self.assertEqual(good, [entry])
        self.assertFalse(failed)
        self.assertEqual(recycled, [self.home / "core.log"])
        for name in ("terminals.json", "holder.log", "core.json", "core.lock", "docs.json", "screens/t1.snap"):
            self.assertTrue((self.home / name).is_file())

    def test_herdr_blocks_shared_ai_records_and_workspaces(self):
        for provider in ("Herdr", "Codex", "Claude", "Gemini"):
            entry = Entry(provider, self.home, "fixture", "fixture", "", 0)
            self.assertTrue(entry_running(entry, {"Herdr"}))
            with self.assertRaises(RuntimeError):
                _stopped(lambda: {"Herdr"}, [entry])
        with self.assertRaises(RuntimeError):
            _stopped(lambda: {"Herdr"}, [], workspace=True)

    def test_all_windows_herdr_process_names_are_detected(self):
        for name in ("Herdr.exe", "herdr-app.exe", "herdr-core.exe", "hc.exe"):
            self.assertEqual(_process_app(name), "Herdr")
        self.assertIsNone(_process_app("herdr-custom-build.exe"))

    def test_default_scan_discovers_profiles_and_protects_storage(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.base)}), \
                patch("manager.providers.profile_roots", return_value=[]):
            entries, errors = scan()
            self.assertTrue(any(e.provider == "Herdr" for e in entries))
            self.assertTrue(fixed_problem(self.home, protected_homes()))
        self.assertFalse([e for e in errors if e.startswith("Herdr")])

    def test_bad_metadata_does_not_hide_logs(self):
        self.state.write_text('{"terminals": []}', encoding="utf8")
        entries, errors = scan([("Herdr", self.home)])
        self.assertTrue(errors)
        self.assertEqual(len(entries), 2)

    def test_state_file_is_never_an_allowed_cache(self):
        entry = Entry("Herdr", self.home, "cache:terminals.json", "fixture", "", 0,
                      [self.state], kind="cache")
        with self.assertRaises(ValueError):
            validate(entry)


if __name__ == "__main__":
    unittest.main()
