"""Adversarial regressions using temporary profiles and fake process/recycle APIs."""
import contextlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from datetime import datetime
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import operations, providers, runtime
from manager.model import signature
from manager.providers import scan


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-adversarial-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "profile"
        self.project = self.root / "source"
        self.home.mkdir()
        self.project.mkdir()
        (self.project / "source.py").write_text("preserve source", encoding="utf8")
        self.destination = self.root / "backups"
        self.trash = self.root / "fake-recycle"
        self.trash.mkdir()
        roots_patch = patch("manager.workspaces.profile_roots", return_value=[])
        roots_patch.start()
        self.addCleanup(roots_patch.stop)

    def claude_file(self, folder="project", message=None):
        path = self.home / "projects" / folder / (str(uuid.uuid4()) + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"cwd": str(self.project), "type": "user", "message": {"content": "fixture"}}
        if message is not None:
            row["message"] = message
        path.write_text(json.dumps(row) + "\n", encoding="utf8")
        return path

    def gemini_file(self, name, row):
        path = self.home / "tmp" / "fixture" / "chats" / ("session-" + name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(row), encoding="utf8")
        return path

    def recycle(self, path):
        path = Path(path)
        path.resolve().relative_to(self.root.resolve())
        shutil.move(str(path), str(self.trash / (uuid.uuid4().hex + "-" + path.name)))


class ScanAdversarialTests(FixtureCase):
    def test_duplicate_roots_are_scanned_once(self):
        self.claude_file()
        (self.home / "cache").mkdir()
        with patch("manager.providers.claude", wraps=__import__("manager.providers", fromlist=["claude"]).claude) as reader:
            entries, errors = scan([("Claude", self.home), ("Claude", self.home / ".")])
        self.assertFalse(errors)
        self.assertEqual(reader.call_count, 1, "one physical profile must be read once")
        self.assertEqual(len(entries), len({e.key for e in entries}))

    def test_bad_gemini_json_keeps_valid_siblings(self):
        sid = str(uuid.uuid4())
        self.gemini_file("good", {"sessionId": sid, "messages": []})
        self.gemini_file("broken", {}).write_text("{broken", encoding="utf8")
        entries, errors = scan([("Gemini", self.home)])
        self.assertEqual([e.session for e in entries], [sid])
        self.assertTrue(errors, "a damaged file must be diagnosed")

    def test_non_object_claude_message_is_isolated(self):
        good = self.claude_file()
        self.claude_file(message="malformed message")
        entries, _ = scan([("Claude", self.home)])
        self.assertIn(good.stem, {e.session for e in entries})

    def test_non_object_gemini_message_is_isolated(self):
        sid = str(uuid.uuid4())
        self.gemini_file("good", {"sessionId": sid, "messages": []})
        self.gemini_file("bad", {"sessionId": str(uuid.uuid4()), "messages": [None]})
        entries, _ = scan([("Gemini", self.home)])
        self.assertIn(sid, {e.session for e in entries})

    def test_corrupt_codex_db_does_not_hide_other_profiles_or_change_bytes(self):
        path = self.home / "state_5.sqlite"
        original = b"fixture is not a sqlite database"
        path.write_bytes(original)
        other = self.root / "other-profile"
        (other / "cache").mkdir(parents=True)
        entries, errors = scan([("Codex", self.home), ("Claude", other)])
        self.assertEqual(len(errors), 1)
        self.assertEqual([e.provider for e in entries], ["Claude"])
        self.assertEqual(path.read_bytes(), original)

    def test_duplicate_session_files_have_unique_selection_keys(self):
        sid = str(uuid.uuid4())
        row = {"sessionId": sid, "messages": []}
        self.gemini_file("first", row)
        self.gemini_file("second", row)
        entries, errors = scan([("Gemini", self.home)])
        self.assertEqual(len(entries), len({e.key for e in entries}), "duplicate keys collapse UI selection")
        self.assertEqual({p for e in entries for p in e.files}, set((self.home / "tmp" / "fixture" / "chats").glob("*.json")))

    def test_invalid_timestamp_cannot_break_datetime_display(self):
        for i, updated in enumerate([float("inf"), float("-inf"), float("nan"), 1e30]):
            self.gemini_file(str(i), {"sessionId": str(uuid.uuid4()), "messages": [], "lastUpdated": updated})
        entries, _ = scan([("Gemini", self.home)])
        self.assertEqual(len(entries), 4)
        for entry in entries:
            self.assertTrue(math.isfinite(entry.updated), "nonfinite timestamps crash Qt datetime formatting")
            datetime.fromtimestamp(entry.updated)

    def test_inaccessible_project_metadata_keeps_healthy_sibling(self):
        good = self.claude_file(folder="good")
        projects = self.home / "projects"
        (projects / "bad").mkdir()
        original_scandir = providers.os.scandir
        class Denied:
            def __init__(self, entry):
                self.path, self.name = entry.path, entry.name
            def stat(self, **kwargs):
                raise PermissionError("fixture child metadata inaccessible")
        @contextlib.contextmanager
        def scandir(folder):
            with original_scandir(folder) as children:
                rows = list(children)
                if Path(folder) == projects:
                    rows.sort(key=lambda e: e.name == "bad")
                    rows = [Denied(e) if e.name == "bad" else e for e in rows]
                yield iter(rows)
        with patch.object(providers.os, "scandir", scandir):
            entries, errors = scan([("Claude", self.home)])
        self.assertIn(good.stem, {e.session for e in entries})
        self.assertTrue(errors)


@unittest.skipUnless(sys.platform == "win32", "Windows process detector")
class ProcessDetectionTests(unittest.TestCase):
    def setUp(self):
        # Exercise the PowerShell fallback even when psutil is installed.
        fallback = patch.object(runtime, "psutil", None, create=True)
        fallback.start()
        self.addCleanup(fallback.stop)

    def test_blank_process_output_fails_closed(self):
        result = subprocess.CompletedProcess([], 0, "", "")
        with patch("manager.runtime._run_process", return_value=result):
            with self.assertRaises((RuntimeError, ValueError)):
                runtime.active_providers()

    def test_empty_json_object_process_output_fails_closed(self):
        result = subprocess.CompletedProcess([], 0, "{}", "")
        with patch("manager.runtime._run_process", return_value=result):
            with self.assertRaises((RuntimeError, ValueError)):
                runtime.active_providers()

    def test_process_query_timeout_propagates(self):
        with patch("manager.runtime._run_process", side_effect=subprocess.TimeoutExpired("fixture", 20)):
            with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                runtime.active_providers()


class DeletionAdversarialTests(FixtureCase):
    def entry(self):
        self.claude_file()
        entries, errors = scan([("Claude", self.home)])
        self.assertFalse(errors)
        return entries[0]

    def test_detector_failure_stops_before_backup_or_mutation(self):
        entry = self.entry()
        recycler = Mock()
        with patch("manager.operations.backup") as backup:
            with self.assertRaises(RuntimeError):
                operations.delete([entry], {entry.key: signature(entry)}, self.destination,
                                  detector=Mock(side_effect=RuntimeError("fixture detector failed")), recycler=recycler)
        backup.assert_not_called()
        recycler.assert_not_called()
        self.assertTrue(entry.files[0].exists())

    def test_scanned_orca_cache_can_be_cleaned(self):
        cache = self.home / "Cache"
        cache.mkdir()
        (cache / "fixture.bin").write_bytes(b"fixture cache")
        entries, errors = scan([("Orca", self.home)])
        self.assertFalse(errors)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        _, good, failed, _ = operations.delete(
            entries, {entry.key: signature(entry)}, self.destination,
            detector=lambda: set(), recycler=self.recycle, include_caches=False)
        self.assertEqual(len(good), 1)
        self.assertFalse(failed)
        self.assertFalse(cache.exists())

    def test_gemini_project_marker_change_after_backup_preserves_container(self):
        chat = self.gemini_file("fixture", {"sessionId": str(uuid.uuid4()), "messages": []})
        container = chat.parent.parent
        marker = container / ".project_root"
        marker.write_text(str(self.project), encoding="utf8")
        entries, errors = scan([("Gemini", self.home)])
        self.assertFalse(errors)
        entry = entries[0]
        moved = []
        def recycle(path):
            moved.append(Path(path))
            self.recycle(path)
            if Path(path) == chat:
                marker.write_text(str(self.project / "new-project-after-backup"), encoding="utf8")
        _, good, failed, _ = operations.delete(
            entries, {entry.key: signature(entry)}, self.destination, {entry.group},
            detector=lambda: set(), recycler=recycle)
        self.assertEqual(len(good), 1)
        self.assertNotIn(container, moved, "changed app metadata was not present in the verified backup")
        self.assertTrue(marker.exists())
        self.assertTrue(failed)

    def test_backup_inside_marked_workspace_rejected_before_mutation(self):
        entry = self.entry()
        recycler = Mock()
        with self.assertRaises(ValueError):
            operations.delete([entry], {entry.key: signature(entry)}, self.project / "backups",
                              detector=lambda: set(), recycler=recycler,
                              workspaces=[str(self.project)], all_entries=[entry])
        recycler.assert_not_called()
        self.assertTrue(entry.files[0].exists())

    def test_restart_after_final_chat_preserves_app_project_container(self):
        entry = self.entry()
        project_container = Path(entry.extra["project_dir"])
        running = set()
        moved = []
        def recycle(path):
            moved.append(Path(path))
            self.recycle(path)
            running.add("Claude")
        _, good, failed, _ = operations.delete(
            [entry], {entry.key: signature(entry)}, self.destination, {entry.group},
            detector=lambda: set(running), recycler=recycle)
        self.assertEqual(len(good), 1)
        self.assertNotIn(project_container, moved, "must recheck processes before container mutation")
        self.assertTrue(project_container.exists())
        self.assertTrue(failed)

    def test_restart_after_final_chat_preserves_marked_work_folder(self):
        entry = self.entry()
        running = set()
        moved = []
        def recycle(path):
            moved.append(Path(path))
            self.recycle(path)
            running.add("Claude")
        _, good, failed, _ = operations.delete(
            [entry], {entry.key: signature(entry)}, self.destination,
            detector=lambda: set(running), recycler=recycle,
            workspaces=[str(self.project)], all_entries=[entry])
        self.assertEqual(len(good), 1)
        self.assertNotIn(self.project, moved, "must recheck processes before workspace mutation")
        self.assertTrue((self.project / "source.py").exists())
        self.assertTrue(failed)


@unittest.skipUnless(sys.platform == "win32", "Windows owned process tree")
class OwnedProcessDeadlineTests(unittest.TestCase):
    def test_timeout_reaps_owned_child_before_following_mutation(self):
        with tempfile.TemporaryDirectory(prefix="ai-owned-child-") as tmp:
            root = Path(tmp)
            helper = root / "fixture_process.py"
            helper.write_text(
                "import pathlib, subprocess, sys, time\n"
                "root = pathlib.Path(sys.argv[1])\n"
                "if sys.argv[2] == 'child':\n"
                "    time.sleep(1.5)\n"
                "    (root / 'child-mutated').write_text('unexpected surviving child')\n"
                "    time.sleep(2.5)\n"
                "else:\n"
                "    child = subprocess.Popen([sys.executable, __file__, str(root), 'child'])\n"
                "    (root / 'child.pid').write_text(str(child.pid))\n"
                "    time.sleep(6)\n", encoding="utf8")
            started = time.monotonic()
            child = None
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    runtime._run_process([sys.executable, str(helper), str(root), "parent"],
                                         timeout=.5, capture_output=True)
                self.assertTrue((root / "child.pid").exists(), "fixture parent never spawned its child")
                pid = int((root / "child.pid").read_text())
                import psutil
                try:
                    child = psutil.Process(pid)
                except psutil.NoSuchProcess:
                    pass
                if child is not None:
                    child.wait(timeout=1)
                self.assertLess(time.monotonic() - started, 3, "inherited pipes exceeded the timeout cleanup bound")
                self.assertFalse((root / "child-mutated").exists())
            finally:
                # Every fixture child exits itself after four seconds, including a broken runtime.
                if child is not None:
                    try:
                        child.wait(timeout=5)
                    except psutil.NoSuchProcess:
                        pass

    def test_spawn_failure_is_reported_without_running_another_program(self):
        with tempfile.TemporaryDirectory(prefix="ai-spawn-failure-") as tmp:
            missing = Path(tmp) / "does-not-exist.exe"
            with self.assertRaises(OSError):
                runtime._run_process([str(missing)], timeout=.5, capture_output=True)
            self.assertFalse(missing.exists())


class DeletionBenchmarkTests(unittest.TestCase):
    def test_mock_pipeline_counts_calls_and_preserves_unselected_data(self):
        from benchmark_delete import Case, benchmark
        result = benchmark(Case(count=2, cache_mode="skip"))
        self.assertTrue(result["verified"], result["verification_error"])
        self.assertEqual(result["spawned_processes"], 0)
        self.assertEqual(result["cli_attempts"], 2)
        self.assertEqual(result["calls"]["_snapshot_database"], 1)
        self.assertEqual(result["remaining_selected"], 0)
        self.assertAlmostEqual(sum(result["stages_exclusive_s"].values()), result["total_s"], places=4)

    def test_cli_timeout_aborts_following_mutations(self):
        from benchmark_delete import Case, benchmark
        result = benchmark(Case(count=3, timeout_call=1))
        self.assertTrue(result["verified"], result["verification_error"])
        self.assertEqual(result["injected_timeouts"], 1)
        self.assertEqual(result["cli_attempts"], 1)
        self.assertEqual(result["mutations_after_timeout"], 0)

    def test_unreachable_timeout_is_rejected(self):
        from benchmark_delete import Case, benchmark
        with self.assertRaises(ValueError):
            benchmark(Case(count=1, timeout_call=2))


if __name__ == "__main__":
    unittest.main()
