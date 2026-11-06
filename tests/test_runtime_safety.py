"""Runtime and mutation safety: disposable fixtures and mocks only."""
import concurrent.futures
import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import operations as ops, runtime
from manager.model import ClosingConnection, Entry, signature
from manager.recycle import recycle
from manager.workspaces import fixed_problem, git_warnings


class RuntimeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="runtime-safety-")
        self.root = Path(self.temp.name)
        runtime.codex_cli.cache_clear()
        runtime._orca_cache = None
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(runtime.codex_cli.cache_clear)
        self.addCleanup(setattr, runtime, "_orca_cache", None)

    def candidate(self, folder="bin"):
        directory = self.root / folder
        directory.mkdir(exist_ok=True)
        path = directory / ("codex.exe" if os.name == "nt" else "codex")
        path.write_bytes(b"fixture")
        return path

    def test_cli_probe_rejects_nonzero_version_output(self):
        with patch.object(runtime, "_run_process", return_value=SimpleNamespace(returncode=1, stdout="99.0.0")):
            self.assertEqual(runtime.cli_version("fixture"), ())

    def test_cli_probe_timeout_is_bounded(self):
        with patch.object(runtime, "_run_process", side_effect=subprocess.TimeoutExpired("fixture", 2)) as run:
            self.assertEqual(runtime.cli_version("fixture"), ())
            self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_command_checks_after_discovery_before_spawning(self):
        events = []
        def resolved(*args):
            events.append("discovery")
            return "fixture.exe", "1.0.0"
        def blocked():
            events.append("check")
            raise RuntimeError("restarted")
        with patch.object(runtime, "codex_cli", side_effect=resolved), patch.object(runtime, "_run_process") as run:
            with self.assertRaises(RuntimeError):
                runtime.command("codex", ["delete"], before_run=blocked)
            self.assertEqual(events, ["discovery", "check"])
            run.assert_not_called()

    def test_concurrent_discovery_probes_once_and_invalidates_changed_binary(self):
        candidate = self.candidate()
        with patch.dict(os.environ, {"PATH": str(candidate.parent) + os.pathsep + str(candidate.parent)}), \
                patch.object(runtime, "cli_version", return_value=(1, 2, 3)) as version:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(lambda _: runtime.codex_cli(), range(8)))
            self.assertEqual(len(set(rows)), 1)
            self.assertEqual(version.call_count, 1)
            candidate.write_bytes(b"changed binary")
            runtime.codex_cli()
            self.assertEqual(version.call_count, 2)

    def test_discovery_rechecks_path_and_rejects_unknown_newer_candidate(self):
        old, new = self.candidate("old"), self.candidate("new")
        with patch.dict(os.environ, {"PATH": str(old.parent)}), \
                patch.object(runtime, "cli_version", return_value=(1, 0, 0)):
            self.assertEqual(runtime.codex_cli()[0], str(old.resolve()))
        with patch.dict(os.environ, {"PATH": str(old.parent) + os.pathsep + str(new.parent)}), \
                patch.object(runtime, "cli_version", side_effect=[(1, 0, 0), ()]):
            with self.assertRaises(RuntimeError):
                runtime.codex_cli()

    def test_discovery_has_total_deadline(self):
        with patch.object(runtime.time, "monotonic", side_effect=[0, 0, 9]), \
                patch.object(runtime, "cli_version", return_value=(1, 0, 0)) as version:
            with self.assertRaises(RuntimeError):
                runtime._codex_candidates((("first", 0, 0), ("second", 0, 0)))
            self.assertEqual(version.call_count, 1)

    def test_fallback_rejects_empty_malformed_and_unknown_process_rows(self):
        for output in ("", "{}", "null", '[{"app":"Unknown","pid":1}]', '[{"app":"Codex","pid":"1"}]'):
            with self.subTest(output=output), patch.object(runtime, "psutil", None), \
                    patch.object(runtime, "_run_process", return_value=SimpleNamespace(returncode=0, stdout=output)):
                with self.assertRaises((RuntimeError, ValueError)):
                    runtime.running_processes()

    def test_fallback_timeout_propagates_and_does_not_cache_empty(self):
        with patch.object(runtime, "psutil", None), patch.object(runtime, "_run_process", side_effect=[
                subprocess.TimeoutExpired("fixture", 8), SimpleNamespace(returncode=0, stdout="[]")]) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                runtime.running_processes()
            self.assertEqual(runtime.running_processes(), [])
            self.assertEqual(run.call_count, 2)

    @unittest.skipIf(runtime.psutil is None, "psutil optional fallback installation")
    def test_native_probe_uses_fresh_objects_when_pid_is_reused(self):
        idle = Mock(pid=1)
        idle.name.return_value = "idle.exe"
        codex = Mock(pid=1)
        codex.name.return_value = "codex.exe"
        codex.exe.return_value = "fixture-codex.exe"
        with patch.object(runtime.psutil, "pids", return_value=[1]), \
                patch.object(runtime.psutil, "Process", side_effect=[idle, codex]), \
                patch.object(runtime, "_run_process") as run:
            self.assertEqual(runtime.running_processes(), [])
            self.assertEqual(runtime.active_providers(), {"Codex"})
            run.assert_not_called()

    @unittest.skipIf(runtime.psutil is None, "psutil optional fallback installation")
    def test_native_node_access_denied_is_fail_closed(self):
        node = Mock(pid=2)
        node.name.return_value = "node.exe"
        node.cmdline.side_effect = runtime.psutil.AccessDenied(2)
        with patch.object(runtime.psutil, "pids", return_value=[2]), \
                patch.object(runtime.psutil, "Process", return_value=node), \
                patch.object(runtime, "_run_process") as run:
            with self.assertRaises(RuntimeError):
                runtime.running_processes()
            run.assert_not_called()

    def test_process_package_matching_handles_windows_paths_and_case(self):
        self.assertEqual(runtime._process_app("NODE.EXE", r"C:\pkg\@Google\Gemini-cli\index.js"), "Gemini")
        self.assertIsNone(runtime._process_app("node.exe", "unrelated"))

    def test_orca_concurrent_queries_single_flight_and_results_do_not_alias(self):
        data = json.dumps({"ok": True, "result": {"repos": [{"path": str(self.root), "displayName": "fixture"}]}})
        with patch.object(runtime, "command", return_value=data) as command:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                rows = list(pool.map(lambda _: runtime.orca_projects(), range(8)))
            self.assertEqual(command.call_count, 1)
            rows[0][0]["path"] = "changed"
            self.assertEqual(runtime.orca_projects()[0]["path"], str(self.root))

    def test_orca_errors_are_not_cached_as_empty_success(self):
        with patch.object(runtime, "command", side_effect=['{"ok":true,"result":{}}', '{"ok":true,"result":{"repos":[]}}']) as command:
            with self.assertRaises(RuntimeError):
                runtime.orca_projects()
            self.assertEqual(runtime.orca_projects(), [])
            self.assertEqual(command.call_count, 2)


class WindowsProcessSafetyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    def test_job_creation_failure_prevents_spawn(self):
        with patch.object(runtime, "_WindowsJob", side_effect=OSError("job denied")), patch.object(runtime.subprocess, "Popen") as spawn:
            with self.assertRaises(OSError):
                runtime._run_process([sys.executable, "-c", "pass"], timeout=0.1)
            spawn.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    def test_assignment_and_job_termination_failure_still_kills_suspended_root(self):
        job, process = Mock(), Mock()
        job.assign_and_resume.side_effect = OSError("assignment denied")
        job.terminate.side_effect = OSError("termination denied")
        job.active.return_value = 0
        process.poll.return_value = None
        with patch.object(runtime, "_WindowsJob", return_value=job), patch.object(runtime.subprocess, "Popen", return_value=process):
            with self.assertRaisesRegex(OSError, "assignment denied"):
                runtime._run_process([sys.executable, "-c", "pass"], timeout=0.1)
        process.kill.assert_called_once()
        process.wait.assert_called_once()
        job.close.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "Windows Job Object")
    def test_root_kill_failure_still_attempts_wait_and_preserves_cleanup_error(self):
        job, process = Mock(), Mock()
        job.assign_and_resume.side_effect = OSError("assignment denied")
        job.terminate.side_effect = OSError("termination denied")
        job.active.return_value = 0
        process.poll.return_value = None
        process.kill.side_effect = OSError("kill denied")
        with patch.object(runtime, "_WindowsJob", return_value=job), patch.object(runtime.subprocess, "Popen", return_value=process):
            with self.assertRaises(OSError) as caught:
                runtime._run_process([sys.executable, "-c", "pass"], timeout=0.1)
        process.wait.assert_called_once()
        self.assertTrue(any("kill denied" in note for note in getattr(caught.exception, "__notes__", [])))


class MutationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mutation-safety-")
        self.base = Path(self.temp.name)
        self.home = self.base / "agent"
        self.folder = self.home / "projects" / "fixture"
        self.folder.mkdir(parents=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / "source.py").write_text("preserve", encoding="utf-8")
        self.destination = self.base / "backups"
        self.sid = str(uuid.uuid4())
        self.chat = self.folder / f"{self.sid}.jsonl"
        self.chat.write_text("fixture chat", encoding="utf-8")
        self.history = self.home / "history.jsonl"
        self.history.write_text(json.dumps({"sessionId": self.sid}) + "\n", encoding="utf-8")
        self.entry = Entry("Claude", self.home, self.sid, "fixture", str(self.workspace), 0,
                           [self.chat], extra={"project_dir": str(self.folder)})
        self.recycler = Mock(side_effect=self.recycle_fixture)
        self.addCleanup(self.temp.cleanup)

    def recycle_fixture(self, path):
        path.resolve().relative_to(self.base.resolve())
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    def delete(self, **kwargs):
        return ops.delete([self.entry], {self.entry.key: signature(self.entry)},
                          kwargs.pop("destination", self.destination), detector=kwargs.pop("detector", lambda: set()),
                          recycler=self.recycler, **kwargs, create_backup=True)

    def junction(self, path, target):
        if os.name != "nt":
            path.symlink_to(target, target_is_directory=True)
        else:
            result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(path), str(target)], capture_output=True)
            if result.returncode:
                self.skipTest("fixture junction unavailable")

    def test_workspace_cannot_contain_its_own_backup(self):
        with self.assertRaises(ValueError):
            self.delete(destination=self.workspace / "backups", workspaces=[str(self.workspace)])
        self.recycler.assert_not_called()
        self.assertTrue(self.chat.exists())

    def test_detector_malformed_responses_never_allow_mutation(self):
        for response in (None, [], {}, "", {"Unknown"}):
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.delete(detector=lambda: response)
        self.recycler.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_detector_failure_after_backup_preserves_record(self):
        detector = Mock(side_effect=[set(), TimeoutError("fixture timeout")])
        archive, good, failed, _ = self.delete(detector=detector)
        self.assertTrue(archive.is_file())
        self.assertFalse(good)
        self.assertTrue(failed)
        self.recycler.assert_not_called()

    def test_verified_backup_replacement_blocks_record_deletion(self):
        def detector():
            if self.destination.exists():
                for archive in self.destination.glob("*.zip"):
                    archive.write_bytes(b"backup replaced after verification")
            return set()
        _, good, failed, _ = self.delete(detector=detector)
        self.assertFalse(good)
        self.assertTrue(failed)
        self.assertTrue(self.chat.exists())
        self.recycler.assert_not_called()

    def test_restart_between_files_stops_the_next_file(self):
        extra = self.home / "debug" / f"{self.sid}.txt"
        extra.parent.mkdir()
        extra.write_text("second", encoding="utf-8")
        self.entry.files.append(extra)
        detector = Mock(side_effect=[set(), set(), {"Claude"}])
        _, good, failed, _ = self.delete(detector=detector)
        self.assertFalse(good)
        self.assertTrue(failed)
        self.assertTrue(extra.exists())
        self.assertEqual(self.recycler.call_count, 1)

    def test_replaced_second_file_with_same_size_and_mtime_is_preserved(self):
        extra = self.home / "debug" / f"{self.sid}.txt"
        extra.parent.mkdir()
        extra.write_text("original", encoding="utf-8")
        old = extra.stat()
        self.entry.files.append(extra)
        def swap(path):
            self.recycle_fixture(path)
            if path == self.chat:
                extra.rename(extra.with_suffix(".old"))
                extra.write_text("replaced", encoding="utf-8")
                os.utime(extra, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.recycler.side_effect = swap
        _, good, failed, _ = self.delete()
        self.assertFalse(good)
        self.assertTrue(failed)
        self.assertEqual(extra.read_text(), "replaced")

    def test_restart_after_last_chat_preserves_history_container_and_workspace(self):
        detector = lambda: {"Claude"} if self.recycler.call_count else set()
        history = self.history.read_bytes()
        _, good, failed, _ = self.delete(detector=detector, project_groups=[self.entry.group],
                                         workspaces=[str(self.workspace)])
        self.assertEqual(len(good), 1)
        self.assertTrue(failed)
        self.assertEqual(self.history.read_bytes(), history)
        self.assertTrue(self.folder.exists())
        self.assertTrue(self.workspace.exists())

    def test_history_rechecks_process_immediately_before_replace(self):
        before = self.history.read_bytes()
        with self.assertRaises(RuntimeError):
            ops.trim_claude_history(self.home, {self.sid}, before_replace=Mock(side_effect=RuntimeError("restarted")))
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(list(self.home.glob(".history-clean-*")), [])

    def test_workspace_changed_during_chat_removal_is_preserved(self):
        def mutate(path):
            self.recycle_fixture(path)
            (self.workspace / "new.txt").write_text("new work", encoding="utf-8")
        self.recycler.side_effect = mutate
        _, _, failed, _ = self.delete(workspaces=[str(self.workspace)])
        self.assertTrue(failed)
        self.assertTrue((self.workspace / "new.txt").exists())

    def test_orca_restart_preserves_workspace_even_for_plain_provider_home(self):
        detector = lambda: {"Orca"} if self.recycler.call_count else set()
        _, _, failed, _ = self.delete(detector=detector, workspaces=[str(self.workspace)])
        self.assertTrue(failed)
        self.assertTrue(self.workspace.exists())

    def test_junction_inside_cache_blocks_backup_and_deletion(self):
        cache = self.home / "cache"
        cache.mkdir()
        self.junction(cache / "outside", self.workspace)
        self.entry = Entry("Codex", self.home, "cache:cache", "cache", "", 0, [cache], kind="cache")
        with self.assertRaises(ValueError):
            self.delete()
        self.recycler.assert_not_called()
        self.assertTrue((self.workspace / "source.py").exists())

    def test_junction_in_backup_ancestor_is_rejected(self):
        link = self.base / "linked"
        self.junction(link, self.workspace)
        with self.assertRaises(ValueError):
            self.delete(destination=link / "backups")
        self.recycler.assert_not_called()
        self.assertFalse((self.workspace / "backups").exists())

    def test_failed_archive_is_not_listed_or_left_partial(self):
        with patch.object(ops.zipfile.ZipFile, "testzip", return_value="corrupt"):
            with self.assertRaises(RuntimeError):
                self.delete()
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertTrue(self.chat.exists())
        self.recycler.assert_not_called()

    def test_backup_name_collision_preserves_existing_archive(self):
        fixed = ops.datetime.now()
        with patch.object(ops.uuid, "uuid4", return_value=SimpleNamespace(hex="12345678")), \
                patch.object(ops, "datetime") as clock:
            clock.now.return_value = fixed
            first = ops.backup([self.entry], self.destination, ())
            original = first.read_bytes()
            with self.assertRaises(FileExistsError):
                ops.backup([self.entry], self.destination, ())
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(list(self.destination.glob("*.partial")), [])

    def test_selected_project_metadata_is_measured_once_per_group(self):
        other = self.folder / f"{uuid.uuid4()}.jsonl"
        other.write_text("second fixture", encoding="utf-8")
        second = Entry("Claude", self.home, other.stem, "second", str(self.workspace), 0,
                       [other], extra={"project_dir": str(self.folder)})
        with patch.object(ops, "_project_state", wraps=ops._project_state) as measured:
            _, good, failed, _ = ops.delete([self.entry, second],
                {e.key: signature(e) for e in (self.entry, second)}, self.destination,
                project_groups=[self.entry.group], detector=lambda: set(), recycler=self.recycler, create_backup=True)
        self.assertEqual(len(good), 2)
        self.assertFalse(failed)
        self.assertEqual(measured.call_count, 2)  # One initial sample, one pre-removal comparison.

    def test_backup_collects_shared_project_only_once(self):
        entries = [self.entry]
        for _ in range(9):
            sid = str(uuid.uuid4())
            path = self.folder / f"{sid}.jsonl"
            path.write_text("fixture", encoding="utf-8")
            entries.append(Entry("Claude", self.home, sid, "fixture", str(self.workspace), 0,
                                 [path], extra={"project_dir": str(self.folder)}))
        with patch.object(ops, "_target_state", wraps=ops._target_state) as states:
            archive = ops.backup(entries, self.destination, [self.entry.group])
        self.assertEqual(sum(call.args[0] == self.folder for call in states.call_args_list), 2)
        with ops.zipfile.ZipFile(archive) as check:
            self.assertEqual(sum(name.endswith(".jsonl") for name in check.namelist()), 11)

    def test_shared_project_change_after_first_collection_is_not_overwritten(self):
        sid = str(uuid.uuid4())
        path = self.folder / f"{sid}.jsonl"
        path.write_text("fixture", encoding="utf-8")
        second = Entry("Claude", self.home, sid, "second", str(self.workspace), 0,
                       [path], extra={"project_dir": str(self.folder)})
        memory = self.folder / "memory.txt"
        memory.write_text("before", encoding="utf-8")
        validate = ops.validate
        collected = [False]
        state = ops._target_state
        def measured(path, home):
            result = state(path, home)
            if path == self.folder:
                collected[0] = True
            return result
        def mutate(entry):
            if entry is second and collected[0]:
                memory.write_text("changed after first collection", encoding="utf-8")
            return validate(entry)
        with patch.object(ops, "validate", side_effect=mutate), patch.object(ops, "_target_state", side_effect=measured):
            with self.assertRaises(ValueError):
                ops.backup([self.entry, second], self.destination, [self.entry.group])
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_failed_process_recheck_is_not_repeated_for_remaining_entries(self):
        sid = str(uuid.uuid4())
        path = self.folder / f"{sid}.jsonl"
        path.write_text("fixture", encoding="utf-8")
        second = Entry("Claude", self.home, sid, "second", str(self.workspace), 0,
                       [path], extra={"project_dir": str(self.folder)})
        detector = Mock(side_effect=[set(), TimeoutError("probe failed")])
        _, good, failed, _ = ops.delete([self.entry, second],
            {e.key: signature(e) for e in (self.entry, second)}, self.destination,
            detector=detector, recycler=self.recycler, create_backup=True)
        self.assertFalse(good)
        self.assertEqual(len(failed), 2)
        self.assertEqual(detector.call_count, 2)
        self.recycler.assert_not_called()
        self.assertTrue(path.exists())

    def test_codex_timeout_aborts_batch_and_postprocessing(self):
        result = benchmark_delete("Codex", count=20, timeout=True)
        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["failed"], 20)
        self.assertEqual(result["cli_calls"], 1)
        self.assertEqual(result["cli_timeout_budget_seconds"], ops.CODEX_DELETE_TIMEOUT)
        self.assertEqual(result["stages"]["_snapshot_database"]["calls"], 1)

    def test_source_change_during_backup_prevents_deletion_and_final_archive(self):
        original_write = ops.zipfile.ZipFile.write
        def changed(archive, filename, *args, **kwargs):
            result = original_write(archive, filename, *args, **kwargs)
            if Path(filename) == self.chat:
                self.chat.write_text("changed during backup", encoding="utf-8")
            return result
        with patch.object(ops.zipfile.ZipFile, "write", changed), self.assertRaises(ValueError):
            self.delete()
        self.recycler.assert_not_called()
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_orca_cache_remains_supported(self):
        cache = self.home / "Cache"
        cache.mkdir()
        (cache / "fixture").write_text("cache", encoding="utf-8")
        self.entry = Entry("Orca", self.home, "cache:Cache", "cache", "", 0, [cache], kind="cache")
        _, good, failed, _ = self.delete()
        self.assertEqual(len(good), 1)
        self.assertFalse(failed)

    def test_unknown_provider_or_kind_and_claude_container_depth_are_rejected(self):
        for field, value in (("provider", "unknown"), ("kind", "unknown")):
            previous = getattr(self.entry, field)
            setattr(self.entry, field, value)
            with self.assertRaises(ValueError):
                ops.validate(self.entry)
            setattr(self.entry, field, previous)
        self.entry.files = [self.home / "projects" / self.sid]
        self.entry.files[0].mkdir()
        with self.assertRaises(ValueError):
            ops.validate(self.entry)

    def test_concurrent_delete_is_rejected_without_mutating_twice(self):
        def nested(path):
            with self.assertRaises(RuntimeError):
                self.delete()
            self.recycle_fixture(path)
        self.recycler.side_effect = nested
        _, good, failed, _ = self.delete()
        self.assertEqual(len(good), 1)
        self.assertFalse(failed)
        self.assertEqual(self.recycler.call_count, 1)

    def test_backup_removal_accepts_generator_without_silently_doing_nothing(self):
        archive = ops.backup([self.entry], self.destination, ())
        self.assertEqual(ops.remove_backups((p for p in [archive]), self.destination), [archive])
        self.assertFalse(archive.exists())

    def test_backup_replacement_between_validation_and_unlink_is_preserved(self):
        first = ops.backup([self.entry], self.destination, ())
        second = ops.backup([self.entry], self.destination, ())
        unlink = Path.unlink
        def swap(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path == first:
                second.write_text("replaced backup", encoding="utf-8")
            return result
        with patch.object(Path, "unlink", swap), self.assertRaises(ValueError):
            ops.remove_backups([first, second], self.destination)
        self.assertEqual(second.read_text(), "replaced backup")

    def test_locked_sqlite_snapshot_has_deadline(self):
        source = self.base / "source.sqlite"
        with sqlite3.connect(source, factory=ClosingConnection) as db:
            db.execute("create table fixture(value)")
        with sqlite3.connect(source, factory=ClosingConnection) as locked:
            locked.execute("BEGIN EXCLUSIVE")
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                ops._snapshot_database(source, self.base / "copy.sqlite", timeout=0.05)
            self.assertLess(time.monotonic() - start, 1)

    def test_relative_root_and_junction_recycle_targets_rejected_before_com(self):
        for path in (Path("relative"), Path(self.base.anchor)):
            with self.assertRaises(ValueError):
                recycle(path)
        link = self.base / "linked"
        self.junction(link, self.workspace)
        with self.assertRaises(ValueError):
            recycle(link)

    def test_workspace_junction_and_parent_reference_are_blocked(self):
        link = self.base / "linked"
        self.junction(link, self.workspace)
        self.assertTrue(fixed_problem(link))
        self.assertTrue(fixed_problem(self.workspace / ".." / "workspace"))

    def test_git_worktree_discovery_failure_is_visible(self):
        (self.workspace / ".git").mkdir()
        with patch("manager.workspaces.command", side_effect=["## main...origin/main\n", RuntimeError("fixture")]):
            self.assertTrue(any("worktree" in message and "실패" in message for message in git_warnings(self.workspace)))


def benchmark_delete(provider="Claude", count=50, memory_files=10, cli_delay=0, process_delay=0,
                     timeout=False, operation=ops):
    """Opt-in full deletion benchmark; every mutation is inside one disposable fixture.

    All Codex subprocesses are intercepted, including version/discovery. Timings are
    inclusive (backup includes snapshot); this never executes a real delete CLI/COM.
    """
    metrics = {}
    cli_calls, timeout_budget = [0], [0]
    def timed(name, function):
        def measured(*args, **kwargs):
            item = metrics.setdefault(name, {"calls": 0, "seconds": 0})
            item["calls"] += 1
            started = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                item["seconds"] += time.perf_counter() - started
        return measured
    with tempfile.TemporaryDirectory(prefix="delete-benchmark-") as temporary:
        root = Path(temporary)
        home, workspace = root / "agent", root / "workspace"
        home.mkdir()
        workspace.mkdir()
        (workspace / "source.py").write_text("fixture source", encoding="utf-8")
        entries = []
        database = home / "state_5.sqlite"
        if provider == "Codex":
            folder = home / "sessions"
            folder.mkdir()
            with sqlite3.connect(database, factory=ClosingConnection) as db:
                db.execute("create table threads(id text primary key, updated_at real)")
                db.execute("create table fixture_blob(value blob)")
                db.execute("insert into fixture_blob values(?)", (b"x" * (2 * 1024 * 1024),))
        else:
            folder = home / "projects" / "fixture"
            (folder / "memory").mkdir(parents=True)
            for index in range(memory_files):
                (folder / "memory" / f"{index}.md").write_text("memory" * 20, encoding="utf-8")
        for _ in range(count):
            sid = str(uuid.uuid4())
            path = folder / f"{sid}.jsonl"
            path.write_text("chat" * 20, encoding="utf-8")
            extra = {"db": str(database)} if provider == "Codex" else {"project_dir": str(folder)}
            entries.append(Entry(provider, home, sid, "fixture", str(workspace), 0, [path], extra=extra))
        if provider == "Codex":
            with sqlite3.connect(database, factory=ClosingConnection) as db:
                db.executemany("insert into threads values(?,0)", [(e.session,) for e in entries])
        (home / "history.jsonl").write_text("".join(json.dumps({"sessionId": e.session}) + "\n" for e in entries), encoding="utf-8")
        expected = {entry.key: signature(entry) for entry in entries}
        executable = root / "bin" / ("codex.exe" if os.name == "nt" else "codex")
        executable.parent.mkdir()
        executable.write_bytes(b"fixture: never execute")
        def detector():
            if process_delay:
                time.sleep(process_delay)
            return set()
        def recycler(path):
            path.resolve().relative_to(root.resolve())
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        def simulated_process(args, **kwargs):
            if args[1:] == ["--version"]:
                return subprocess.CompletedProcess(args, 0, "1.0.0", "")
            if len(args) != 5 or args[1:3] != ["--no-daemon", "delete"] or args[4] != "--force":
                raise AssertionError("unexpected fixture subprocess")
            if Path(kwargs["env"]["CODEX_HOME"]).resolve() != home.resolve():
                raise AssertionError("fixture boundary")
            cli_calls[0] += 1
            timeout_budget[0] += kwargs["timeout"]
            if timeout:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            if cli_delay:
                time.sleep(cli_delay)
            with sqlite3.connect(database, factory=ClosingConnection) as db:
                db.execute("delete from threads where id=?", (args[3],))
            (folder / f"{args[3]}.jsonl").unlink()
            return subprocess.CompletedProcess(args, 0, "fixture deleted", "")
        runtime.codex_cli.cache_clear()
        with contextlib.ExitStack() as stack:
            for name in ("backup", "_snapshot_database", "validate", "signature", "_target_state",
                         "_project_state", "trim_claude_history", "safe_path"):
                stack.enter_context(patch.object(operation, name, timed(name, getattr(operation, name))))
            stack.enter_context(patch.object(runtime, "codex_cli", timed("cli_discovery", runtime.codex_cli)))
            stack.enter_context(patch.object(runtime, "_run_process", timed("subprocess", simulated_process)))
            stack.enter_context(patch.dict(os.environ, {"PATH": str(executable.parent)}))
            started = time.perf_counter()
            archive, good, failed, notes = operation.delete(entries, expected, root / "backups",
                project_groups=[entries[0].group], detector=timed("process_recheck", detector),
                runner=operation.command, recycler=timed("recycle", recycler), create_backup=True)
            elapsed = time.perf_counter() - started
        runtime.codex_cli.cache_clear()
        with ops.zipfile.ZipFile(archive) as check:
            members = len(check.namelist())
        return {"provider": provider, "selected": count, "memory_files": memory_files,
                "total_seconds": round(elapsed, 6), "succeeded": len(good), "failed": len(failed),
                "cli_calls": cli_calls[0], "cli_timeout_budget_seconds": timeout_budget[0],
                "archive_members": members, "archive_bytes": archive.stat().st_size,
                "stages": {key: {"calls": value["calls"], "seconds": round(value["seconds"], 6)}
                           for key, value in metrics.items()}}


if __name__ == "__main__":
    if "--benchmark-delete" in sys.argv:
        print(json.dumps([benchmark_delete("Claude"), benchmark_delete("Codex")], indent=2))
    else:
        unittest.main()
