import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import artifacts, operations
from manager.model import Entry, signature
from manager.runtime import LiveApps


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="artifact-test-")
        self.base = Path(self.temp.name)
        self.tmp = self.base / "temp"
        self.user = self.base / "user"
        self.project = self.base / "project"
        for path in (self.tmp, self.user, self.project):
            path.mkdir()
        self.now = time.time()
        self.old = self.now - 45 * artifacts.DAY
        self.patches = [patch.dict(os.environ, {"TEMP": str(self.tmp), "USERPROFILE": str(self.user)}),
                        patch("manager.artifacts.live_cwds", return_value=[]),
                        patch("manager.artifacts.process_owners", return_value=[]),
                        patch("manager.runtime.active_providers", return_value=LiveApps())]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.temp.cleanup)

    def age(self, path=None, days=45):
        path = path or self.base
        stamp = self.now - days * artifacts.DAY
        for parent, dirs, files in os.walk(path):
            for name in files:
                p = Path(parent) / name
                if not p.is_symlink():
                    os.utime(p, (stamp, stamp))
            for name in dirs:
                p = Path(parent) / name
                if not p.is_symlink():
                    os.utime(p, (stamp, stamp))
            os.utime(parent, (stamp, stamp))

    def target(self, name, root=None):
        path = (root or self.project) / name
        path.mkdir(parents=True)
        (path / "generated.bin").write_bytes(b"generated")
        return path

    def conversation(self, project=None, updated=None):
        home = self.base / "records"
        home.mkdir(exist_ok=True)
        return Entry("Claude", home, str(uuid.uuid4()), "private title", str(project or self.project),
                     self.old if updated is None else updated)

    def scan(self, entries=None, live=()):
        errors = []
        result = artifacts.scan_artifacts([self.conversation()] if entries is None else entries,
                                          errors, live=list(live), now=self.now)
        self.assertFalse(errors, errors)
        return result

    def one(self, name="node_modules", root=None):
        path = self.target(name, root)
        self.age()
        found = self.scan()
        return next(e for e in found if e.files == [path])

    def test_all_project_categories_and_no_descent_into_targets(self):
        for name in ("node_modules", ".venv", "venv", ".venv-local", "target", "dist", "build", "__pycache__", ".pytest_cache"):
            p = self.target(name)
            if name.startswith(".venv") or name == "venv":
                (p / "pyvenv.cfg").write_text("home=python")
        (self.project / "package.json").write_text("{}")
        (self.project / "Cargo.toml").write_text("[package]")
        self.target("node_modules/nested/__pycache__")
        self.age()
        found = self.scan()
        self.assertEqual(len(found), 9)
        self.assertEqual({e.extra["category"] for e in found}, {"deps-node", "deps-venv", "build-target", "build-dist", "pycache"})
        self.assertTrue(all(not e.blocked for e in found))
        self.assertTrue(all(e.extra["reinstall"] for e in found if e.extra["tier"] == 2))

    def test_unmarked_build_venv_output_and_deep_targets_are_excluded(self):
        for name in ("venv", "target", "dist", "build", "output", "a/b/c/d/node_modules"):
            self.target(name)
        self.age()
        self.assertEqual(self.scan(), [])

    def test_target_cachedir_and_python_build_markers(self):
        p = self.target("target")
        (p / "CACHEDIR.TAG").write_text("Signature")
        self.target("python/dist")
        (self.project / "python/pyproject.toml").write_text("")
        self.age()
        self.assertEqual({e.extra["category"] for e in self.scan()}, {"build-target", "build-dist"})

    def test_temp_patterns_are_explicit_and_claude_uuid_only(self):
        for name in ("codex_one", "codex-two", "herdr-jhj-three", "ai-storage-db-four", "other.tmp", "agent-five"):
            self.target(name, self.tmp)
        sid = str(uuid.uuid4())
        self.target(f"claude/hash/{sid}", self.tmp)
        self.target("claude/hash/not-session", self.tmp)
        self.age()
        found = self.scan([])
        self.assertEqual(len(found), 5)
        self.assertEqual({e.extra["category"] for e in found}, {"temp-agent", "temp-claude"})
        self.assertTrue(all(e.extra["tier"] == 1 and e.extra["method"] == "delete" for e in found))

    def test_seven_day_and_24_hour_thresholds(self):
        path = self.target("codex_test", self.tmp)
        for days, text in ((0, "24시간"), (5, "7일"), (8, "")):
            self.age(path, days)
            found = self.scan([])
            self.assertEqual(len(found), 1)
            self.assertIn(text, found[0].blocked)
            if not text:
                self.assertFalse(found[0].blocked)

    def test_project_idle_uses_conversation_and_source_changes(self):
        self.one()
        found = self.scan([self.conversation(updated=self.now - 10 * artifacts.DAY)])
        self.assertIn("30일", found[0].blocked)
        (self.project / "source.py").write_text("recent work")
        found = self.scan()
        self.assertIn("30일", found[0].blocked)

    def test_nested_project_roots_deduplicate(self):
        path = self.target("nested/node_modules")
        self.age()
        found = self.scan([self.conversation(), self.conversation(self.project / "nested")])
        self.assertEqual([e.files[0] for e in found], [path])

    def test_herdr_and_live_cwd_discover_projects(self):
        self.target("node_modules")
        self.age()
        terminal = self.conversation()
        terminal.kind, terminal.provider = "cache", "Herdr"
        self.assertEqual(len(self.scan([terminal])), 1)
        found = self.scan([], live=[self.project])
        self.assertEqual(len(found), 1)
        self.assertIn("Herdr", found[0].blocked)

    def test_live_cwd_and_unknown_state_fail_closed(self):
        e = self.one()
        self.assertIn("Herdr", self.scan(live=[self.project / "src"])[0].blocked)
        with patch("manager.artifacts.live_cwds", return_value=None):
            found = artifacts.scan_artifacts([self.conversation()], [], now=self.now)
            self.assertEqual(found[0].blocked, artifacts.LIVE_UNKNOWN)
            with self.assertRaisesRegex(ValueError, "Herdr"):
                artifacts.revalidate(e, now=self.now)

    def test_drive_and_home_live_cwds_do_not_block_everything(self):
        e = self.one()
        self.assertFalse(artifacts.live_problem(e, [Path(self.project.anchor), Path.home(), Path.home().parent]))
        direct = Entry("Artifacts", self.base, "x", "x", "label", 0, [Path.home() / "direct"], "artifact")
        self.assertTrue(artifacts.live_problem(direct, [Path.home()]))

    def test_running_claude_session_protects_scratchpad(self):
        sid = str(uuid.uuid4())
        self.target(f"claude/hash/{sid}", self.tmp)
        self.age()
        with patch("manager.runtime.active_providers", return_value=LiveApps({"Claude"}, claude_sessions=[sid])):
            self.assertIn("Claude", self.scan([])[0].blocked)
        with patch("manager.runtime.active_providers", return_value=LiveApps({"Claude"}, claude_sessions=[str(uuid.uuid4())])):
            self.assertFalse(self.scan([])[0].blocked)

    def test_running_codex_protects_codex_temp_only(self):
        self.target("codex_test", self.tmp)
        self.target("herdr-jhj-test", self.tmp)
        self.age()
        with patch("manager.runtime.active_providers", return_value=LiveApps({"Codex"})):
            found = {e.files[0].name: e for e in self.scan([])}
            self.assertIn("Codex", found["codex_test"].blocked)
            self.assertFalse(found["herdr-jhj-test"].blocked)

    def test_backup_newest_three_per_folder(self):
        for category in ("runtime-backups", "profile-backups"):
            for n in range(5):
                p = self.target(f"{category}/2026010{n}", artifacts.backup_root())
                self.age(p, days=20 - n)
        found = self.scan([])
        self.assertEqual(len(found), 10)
        self.assertEqual(sum("최근 백업 3개" in e.blocked for e in found), 6)
        self.assertEqual(sum(not e.blocked for e in found), 4)

    @unittest.skipUnless(shutil.which("git"), "git unavailable")
    def test_git_tracked_target_skipped_and_untracked_allowed(self):
        tracked = self.target("dist")
        self.target("node_modules")
        (self.project / "package.json").write_text("{}")
        for args in (["init", "-q"], ["add", "dist/generated.bin"]):
            subprocess.run(["git", "-C", str(self.project), *args], check=True,
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.age()
        found = self.scan()
        self.assertFalse(any(e.files == [tracked] for e in found))
        self.assertEqual(len(found), 1)

    def test_git_failure_lists_blocked_target(self):
        (self.project / ".git").mkdir()
        self.target("node_modules")
        self.age()
        with patch("manager.artifacts.subprocess.run", side_effect=OSError("git missing")):
            self.assertIn("Git", self.scan()[0].blocked)

    def test_project_root_cannot_be_deleted_as_artifact(self):
        e = self.one()
        e.extra["protected_roots"].append(str(e.files[0] / "nested-project"))
        with self.assertRaisesRegex(ValueError, "프로젝트"):
            operations.validate(e)

    def test_forged_category_method_root_and_path_refused(self):
        e = self.one()
        for key, value in (("category", "output"), ("method", "delete"), ("tier", 1)):
            old = e.extra[key]
            e.extra[key] = value
            with self.assertRaises(ValueError):
                operations.validate(e)
            e.extra[key] = old
        e.files = [self.project]
        with self.assertRaises(ValueError):
            operations.validate(e)

    def test_symlink_and_internal_link_are_refused(self):
        e = self.one()
        outside = self.target("outside", self.base)
        link = e.files[0] / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                self.skipTest("symlinks unavailable")
            # Junction creation does not need Windows developer-mode privileges.
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if result.returncode:
                self.skipTest("links and junctions unavailable")
        self.addCleanup(lambda: os.rmdir(link) if link.exists() and not link.is_symlink() else link.unlink(missing_ok=True))
        with self.assertRaises(ValueError):
            operations.validate(e)
        self.assertTrue((outside / "generated.bin").exists())

    def test_permanent_and_recycle_routing_and_no_artifact_backup(self):
        temp = self.target("codex_test", self.tmp)
        project = self.target("node_modules")
        self.age()
        entries = self.scan()
        recycler = Mock()
        archive, good, failed, _ = operations.delete(entries, {e.key: signature(e) for e in entries},
            detector=lambda: LiveApps({"Herdr"}), recycler=recycler, create_backup=True)
        self.assertIsNone(archive)
        self.assertFalse(failed)
        self.assertEqual(len(good), 2)
        self.assertFalse(temp.exists())
        self.assertTrue(project.exists())
        recycler.assert_called_once_with(project)
        destination = self.base / "backup"
        self.assertIsNone(operations.backup(entries, destination, ()))
        self.assertFalse(destination.exists())

    def test_file_change_after_scan_is_a_per_item_failure(self):
        e = self.one()
        before = signature(e)
        (e.files[0] / "generated.bin").write_text("changed")
        recycler = Mock()
        _, good, failed, _ = operations.delete([e], {e.key: before}, detector=lambda: set(), recycler=recycler)
        self.assertFalse(good)
        self.assertEqual(len(failed), 1)
        recycler.assert_not_called()

    def test_change_after_scan_before_selection_is_refused(self):
        e = self.one()
        (e.files[0] / "new.bin").write_text("new")
        self.age()
        with self.assertRaisesRegex(ValueError, "스캔 이후"):
            artifacts.revalidate(e, now=self.now)

    def test_deep_recent_file_rechecked_before_mutation(self):
        path = self.target("node_modules/deep/package")
        self.age()
        e = self.scan()[0]
        (path / "generated.bin").write_text("fresh")
        with self.assertRaisesRegex(ValueError, "24시간"):
            artifacts.revalidate(e, now=self.now)

    def test_new_live_cwd_before_delete_preserves_item(self):
        e = self.one()
        recycler = Mock()
        with patch("manager.artifacts.live_cwds", return_value=[self.project]):
            _, good, failed, _ = operations.delete([e], {e.key: signature(e)}, detector=lambda: set(), recycler=recycler)
        self.assertFalse(good)
        self.assertEqual(len(failed), 1)
        recycler.assert_not_called()

    def test_standalone_cli_project_and_unknown_ownership_are_preserved(self):
        e = self.one()
        for owners in ([self.project / "src"], None):
            with patch("manager.artifacts.process_owners", return_value=owners):
                with self.assertRaisesRegex(ValueError, "Claude|Codex"):
                    artifacts.revalidate(e, now=self.now, apps=LiveApps({"Codex"}))

    def test_scan_integration_keeps_protected_projects_off_artifacts(self):
        from manager.providers import scan
        e = self.one()
        with patch("manager.providers.profile_roots", return_value=[]), \
                patch("manager.artifacts.scan_artifacts", return_value=[e]) as scanner:
            found, _ = scan([], include_artifacts=True)
        scanner.assert_called_once()
        self.assertIn(e, found)
        self.assertNotIn("protected_projects", e.extra)

    def test_git_tracked_after_scan_is_refused(self):
        e = self.one()
        with patch("manager.artifacts._git_problem", return_value="tracked"):
            with self.assertRaisesRegex(ValueError, "Git"):
                artifacts.revalidate(e, now=self.now)

    def test_backup_that_becomes_newest_is_preserved(self):
        for n in range(4):
            self.target(f"runtime-backups/{n}", artifacts.backup_root())
        self.age()
        found = self.scan([])
        e = next(e for e in found if not e.blocked)
        os.utime(e.files[0], (self.old + artifacts.DAY, self.old + artifacts.DAY))
        # Refresh the item metadata to isolate the count policy from the change policy.
        _, e.extra["scan_state"] = artifacts._snapshot(e.files[0], recursive=True)
        with self.assertRaisesRegex(ValueError, "최근 백업 3개"):
            artifacts.revalidate(e, now=self.now)

    def test_tier_two_scan_does_not_walk_large_dependency_tree(self):
        self.target("node_modules/a/b/c/d")
        self.age()
        original = artifacts._children
        def children(path):
            if "node_modules" in path.parts and path.name != "node_modules":
                raise AssertionError("dependency subtree traversed during scan")
            return original(path)
        with patch("manager.artifacts._children", side_effect=children):
            self.assertEqual(len(self.scan()), 1)

    def test_read_only_cli_json(self):
        import main
        entries = [self.one()]
        for flag in ("--inventory", "--artifacts"):
            output = io.StringIO()
            with patch.object(sys, "argv", ["main.py", flag]), patch("manager.providers.scan", return_value=(entries, [])), \
                    patch.object(main, "run_gui") as gui, contextlib.redirect_stdout(output):
                main.main()
            payload = json.loads(output.getvalue())
            gui.assert_not_called()
            self.assertNotIn("private title", output.getvalue())
            if flag == "--inventory":
                self.assertEqual(payload["artifacts"]["2"]["count"], 1)
                self.assertGreater(payload["artifacts"]["2"]["bytes"], 0)
            else:
                self.assertEqual(payload[0]["category"], "deps-node")
                self.assertNotIn("title", payload[0])


if __name__ == "__main__":
    unittest.main()
