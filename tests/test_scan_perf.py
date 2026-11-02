"""Temporary fixtures only; run this file with --benchmark for scan timings."""
import json
import hashlib
import os
import sqlite3
import statistics
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from types import ModuleType, SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import model, providers


def make_codex(home, count=100, payload=0):
    home.mkdir(parents=True, exist_ok=True)
    sessions = home / "sessions"
    sessions.mkdir()
    project = str(home.parent / "project")
    with sqlite3.connect(home / "state_5.sqlite", factory=model.ClosingConnection) as conn:
        conn.execute("create table threads(id text, cwd text, title text, updated_at integer, "
                     "rollout_path text, project_id text, irrelevant text)")
        for index in range(count):
            sid = str(uuid.UUID(int=index + 1))
            rollout = sessions / f"rollout-{sid}.jsonl"
            rollout.write_bytes(b"fixture\n")
            conn.execute("insert into threads values(?,?,?,?,?,?,?)",
                         (sid, project, "fixture", 100, str(rollout), None, "x" * payload))


def make_claude(home, count=100):
    folder = home / "projects" / "fixture-project"
    folder.mkdir(parents=True)
    for index in range(count):
        sid = str(uuid.UUID(int=index + 1))
        (folder / f"{sid}.jsonl").write_text(json.dumps({"cwd": str(home.parent / "project"),
            "type": "user", "message": {"content": "fixture"}}) + "\n", encoding="utf8")
    return folder


def make_gemini(home, count=50, payload=0):
    folder = home / "tmp" / "fixture-project"
    chats = folder / "chats"
    chats.mkdir(parents=True)
    (folder / ".project_root").write_text(str(home.parent / "project"), encoding="utf8")
    for index in range(count):
        (chats / f"session-{index}.json").write_text(json.dumps({
            "sessionId": str(uuid.UUID(int=index + 1)), "summary": "fixture",
            "lastUpdated": 100, "messages": [{"type": "user", "content": "x" * payload}]}), encoding="utf8")
    return folder


def baseline_modules():
    package = "_scan_baseline"
    sys.modules[package] = ModuleType(package)
    sys.modules[package].__path__ = []
    modules = []
    for name in ("model", "providers"):
        source = subprocess.check_output(["git", "show", f"HEAD:scripts/manager/{name}.py"],
                                         cwd=Path(__file__).resolve().parents[1])
        module = ModuleType(f"{package}.{name}")
        module.__package__ = package
        sys.modules[module.__name__] = module
        exec(compile(source, f"HEAD:{name}.py", "exec"), module.__dict__)
        modules.append(module)
    return modules


def benchmark(compare=False):
    variants = [("optimized", model, providers)]
    if compare:
        variants.insert(0, ("baseline", *baseline_modules()))
    with tempfile.TemporaryDirectory(prefix="scan-perf-") as temp:
        base = Path(temp)
        make_codex(base / "codex", 1500, 32768)
        make_claude(base / "claude", 500)
        make_gemini(base / "gemini", 100, 65536)
        cache = base / "cache"
        for index in range(30):
            folder = cache / str(index)
            folder.mkdir(parents=True)
            for item in range(100):
                (folder / str(item)).write_bytes(b"x" * 128)
        cases = {"codex_1500": lambda m, p: p.scan([("Codex", base / "codex")]),
                 "claude_500": lambda m, p: p.scan([("Claude", base / "claude")]),
                 "gemini_100_6mb": lambda m, p: p.scan([("Gemini", base / "gemini")]),
                 "cache_3000": lambda m, p: m.tree_size(cache)}
        for name, function in cases.items():
            timings = {label: [] for label, _, _ in variants}
            for _, m, p in variants:
                function(m, p)  # Warm both versions on the same fixture.
            for index in range(3):
                for label, m, p in (variants if index % 2 else variants[::-1]):
                    start = time.perf_counter()
                    result = function(m, p)
                    timings[label].append((time.perf_counter() - start) * 1000)
                    if isinstance(result, tuple):
                        assert not result[1], result[1]
                        amount = len(result[0])
                    else:
                        amount = result
                    assert amount == {"codex_1500": 1500, "claude_500": 500,
                                      "gemini_100_6mb": 100, "cache_3000": 384000}[name]
            print(json.dumps({"case": name, "median_ms": {
                label: round(statistics.median(values), 2) for label, values in timings.items()}, "result": amount}))


class ScanSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scan-safety-")
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_codex_minimal_schema_and_non_numeric_database_name(self):
        home = self.base / "codex"
        home.mkdir()
        (home / "state_bad.sqlite").write_bytes(b"invalid")
        with sqlite3.connect(home / "state_5.sqlite", factory=model.ClosingConnection) as conn:
            conn.execute("create table threads(id text, cwd text, title text, updated_at integer)")
            conn.execute("insert into threads values(?,?,?,?)", (str(uuid.uuid4()), "", "fixture", 100))
        entries, errors = providers.scan([("Codex", home)])
        self.assertFalse(errors)
        self.assertEqual(len(entries), 1)

    def test_corrupt_json_does_not_hide_healthy_conversations(self):
        home = self.base / "gemini"
        folder = make_gemini(home, 2)
        (folder / "chats" / "session-bad.json").write_text("{", encoding="utf8")
        entries, errors = providers.scan([("Gemini", home)])
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(errors), 1)

    def test_claude_unexpected_message_and_unreadable_file_are_isolated(self):
        home = self.base / "claude"
        folder = make_claude(home, 2)
        bad = folder / f"{uuid.uuid4()}.jsonl"
        bad.write_text('{"type":"user","message":null}\n', encoding="utf8")
        original = Path.open
        locked = folder / f"{uuid.uuid4()}.jsonl"
        locked.write_text("{}\n", encoding="utf8")
        def open_file(path, *args, **kwargs):
            if path == locked:
                raise PermissionError("fixture locked")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", open_file):
            entries, errors = providers.scan([("Claude", home)])
        self.assertGreaterEqual(len(entries), 2)
        self.assertTrue(errors)

    def test_missing_and_corrupt_database_do_not_break_other_profiles(self):
        home = self.base / "codex"
        home.mkdir()
        (home / "state_5.sqlite").write_bytes(b"not sqlite")
        other = self.base / "claude"
        make_claude(other, 1)
        entries, errors = providers.scan([("Codex", home), ("Gemini", self.base / "missing"), ("Claude", other)])
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(errors), 1)

    def test_tree_size_and_signature_are_fresh_after_changes(self):
        folder = self.base / "cache"
        folder.mkdir()
        target = folder / "first"
        target.write_bytes(b"first")
        entry = model.Entry("Claude", self.base, "cache:cache", "fixture", "", 0, [folder], "cache")
        before = model.signature(entry)
        self.assertEqual(entry.size, 5)
        target.write_bytes(b"larger fixture")
        self.assertEqual(entry.size, 14)
        self.assertNotEqual(before, model.signature(entry))
        before = model.signature(entry)
        (folder / "second").write_bytes(b"x")
        self.assertNotEqual(before, model.signature(entry))

    def test_signature_refuses_unreadable_directory(self):
        folder = self.base / "cache"
        folder.mkdir()
        entry = model.Entry("Claude", self.base, "cache:cache", "fixture", "", 0, [folder], "cache")
        original = os.scandir
        def scandir(path):
            if Path(path) == folder:
                raise PermissionError("fixture locked")
            return original(path)
        with patch.object(os, "scandir", scandir):
            self.assertEqual(entry.size, 0)
            with self.assertRaises(OSError):
                model.signature(entry)

    def test_read_json_limit_including_growth(self):
        target = self.base / "large.json"
        target.write_text('"' + "x" * 32 + '"', encoding="utf8")
        with self.assertRaises(ValueError):
            model.read_json(target, 16)
        with patch.object(os, "fstat", return_value=type("Stat", (), {"st_size": 1})()):
            with self.assertRaisesRegex(ValueError, "변경"):
                model.read_json(target, 64)

    def test_large_fixture_retains_every_session_and_deduplicates_projects(self):
        home = self.base / "codex"
        make_codex(home, 200, 8192)
        entries, errors = providers.scan([("Codex", home)])
        self.assertFalse(errors)
        self.assertEqual(len(entries), 200)
        self.assertEqual(len(entries[0].extra["protected_projects"]), 1)

    def test_database_lock_is_reported_and_other_profile_still_scans(self):
        home = self.base / "codex"
        make_codex(home, 1)
        other = self.base / "claude"
        make_claude(other, 1)
        with sqlite3.connect(home / "state_5.sqlite", factory=model.ClosingConnection) as writer:
            writer.execute("begin exclusive")
            entries, errors = providers.scan([("Codex", home), ("Claude", other)])
        self.assertEqual([e.provider for e in entries], ["Claude"])
        self.assertEqual(len(errors), 1)
        self.assertIn("locked", errors[0])

    def test_corrupt_newest_database_does_not_fall_back_to_stale_rows(self):
        home = self.base / "codex"
        make_codex(home, 1)
        (home / "state_6.sqlite").write_bytes(b"fixture corruption")
        entries, errors = providers.scan([("Codex", home)])
        self.assertFalse(entries)
        self.assertEqual(len(errors), 1)

    def test_database_projection_omits_large_payloads(self):
        home = self.base / "codex"
        make_codex(home, 2, 65536)
        queries, original = [], providers.read_db
        def read_db(path):
            conn = original(path)
            conn.set_trace_callback(queries.append)
            return conn
        with patch.object(providers, "read_db", read_db):
            entries, errors = providers.scan([("Codex", home), ("Codex", home)])
        self.assertEqual(len(entries), 2)
        self.assertFalse(errors)
        selected = [sql.lower() for sql in queries if sql.lower().startswith("select")]
        self.assertEqual(len(selected), 1)
        self.assertNotIn("irrelevant", selected[0])
        self.assertNotIn("*", selected[0])

    def link(self, source, target):
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(source))
        else:
            source.symlink_to(target, target_is_directory=True)

    def test_links_are_excluded_from_scans_sizes_and_signatures(self):
        outside = self.base / "outside"
        make_claude(outside, 1)
        home = self.base / "claude"
        home.mkdir()
        for source, target in [(home / "projects", outside / "projects"), (home / "cache", outside)]:
            self.link(source, target)
        entries, errors = providers.scan([("Claude", home)])
        self.assertFalse(entries)
        self.assertFalse(errors)
        self.assertEqual(model.tree_size(home), 0)
        entry = model.Entry("Claude", self.base, "fixture", "fixture", "", 0, [home])
        before = model.signature(entry)
        (outside / "new").write_bytes(b"fixture")
        self.assertEqual(before, model.signature(entry))
        entry.files = [home / "cache"]
        with self.assertRaises(ValueError):
            model.signature(entry)

    def test_profile_discovery_deduplicates_and_skips_account_junction(self):
        roaming, user = self.base / "roaming", self.base / "user"
        account = roaming / "orca" / "codex-accounts" / "valid"
        home = account / "home"
        home.mkdir(parents=True)
        self.link(account.parent / "linked", account)
        with patch.dict(os.environ, {"CODEX_HOME": str(home), "CLAUDE_CONFIG_DIR": str(user / "missing")}):
            self.assertEqual(providers.profile_roots(user, roaming), [("Codex", home.resolve())])

    def test_signature_matches_previous_format_and_detects_same_size_changes(self):
        folder = self.base / "cache"
        folder.mkdir()
        target = folder / "fixture"
        target.write_bytes(b"before")
        entry = model.Entry("Claude", self.base, "fixture", "fixture", "", 0, [folder])
        rows = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in model.tree_files(folder)]
        before = model.signature(entry)
        self.assertEqual(before, hashlib.sha256(json.dumps(sorted(rows)).encode()).hexdigest())
        stamp = target.stat().st_mtime_ns
        target.write_bytes(b"after!")
        os.utime(target, ns=(stamp + 1000000, stamp + 1000000))
        self.assertNotEqual(before, model.signature(entry))

    def test_disappearance_during_signature_fails_closed(self):
        folder = self.base / "cache"
        folder.mkdir()
        target = folder / "fixture"
        target.write_bytes(b"fixture")
        entry = model.Entry("Claude", self.base, "fixture", "fixture", "", 0, [folder])
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == target:
                raise FileNotFoundError("fixture removed concurrently")
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", lstat):
            with self.assertRaises(OSError):
                model.signature(entry)

    def test_bad_marker_blocks_only_its_entries_and_malformed_messages_do_not_abort(self):
        home = self.base / "gemini"
        folder = make_gemini(home, 1)
        (folder / ".project_root").write_bytes(b"\xff")
        path = folder / "chats" / "session-0.json"
        row = json.loads(path.read_text(encoding="utf8"))
        row.pop("summary")
        row["messages"] = [None, 12, {"type": "user", "content": "fixture"}]
        path.write_text(json.dumps(row), encoding="utf8")
        entries, errors = providers.scan([("Gemini", home)])
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].blocked)
        self.assertEqual(entries[0].title, "fixture")

    def test_duplicate_ids_keep_all_files_and_block_ambiguous_deletion(self):
        home = self.base / "gemini"
        folder = make_gemini(home, 1)
        chats = folder / "chats"
        (chats / "session-copy.json").write_bytes((chats / "session-0.json").read_bytes())
        entries, errors = providers.scan([("Gemini", home)])
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0].files), 2)
        self.assertTrue(entries[0].blocked)
        self.assertEqual(len(errors), 1)

    def test_linked_codex_rollouts_cannot_be_deleted_without_backup(self):
        from manager.operations import validate
        home = self.base / "codex"
        make_codex(home, 1)
        target = next((home / "sessions").iterdir())
        original = Path.lstat
        for mode, attributes in [(stat.S_IFLNK | 0o777, 0), (stat.S_IFREG | 0o600, 0x400)]:
            with self.subTest(mode=mode, attributes=attributes):
                def lstat(path, *args, **kwargs):
                    if path == target:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                    return original(path, *args, **kwargs)
                with patch.object(Path, "lstat", lstat):
                    entries, errors = providers.scan([("Codex", home)])
                self.assertEqual(len(entries), 1)
                self.assertIn(target, entries[0].files)
                self.assertTrue(entries[0].blocked)
                self.assertEqual(len(errors), 1)
                with self.assertRaises(ValueError):
                    validate(entries[0])

    def test_codex_rollouts_appearing_after_scan_require_refresh(self):
        home = self.base / "codex"
        make_codex(home, 1)
        target = next((home / "sessions").iterdir())
        for missing in (True, False):
            with self.subTest(missing=missing):
                if missing:
                    target.unlink()
                else:
                    target.write_bytes(b"fixture")
                entries, errors = providers.scan([("Codex", home)])
                self.assertFalse(errors)
                model.signature(entries[0])
                born = target if missing else target.with_name(target.name + ".zst")
                born.write_bytes(b"new fixture")
                with self.assertRaisesRegex(ValueError, "새로고침"):
                    model.signature(entries[0])
                born.unlink()


if __name__ == "__main__":
    if "--benchmark" in sys.argv or "--compare-baseline" in sys.argv:
        benchmark(compare="--compare-baseline" in sys.argv)
    else:
        unittest.main()
