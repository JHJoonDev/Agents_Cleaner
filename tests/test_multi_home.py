"""Claude launcher homes and read-only Herdr live inventory."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import herdr, providers, runtime
from manager.model import Entry


class MultiHomeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.user = Path(temp.name)
        for name in (".claude", ".claude-acct2", ".claude-unused", "custom"):
            (self.user / name).mkdir()
        (self.user / ".claude-acct2" / "projects").mkdir()
        self.env = patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.user / "custom"),
                                           "CODEX_HOME": str(self.user / "missing")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_discovery_includes_default_launcher_and_explicit_homes(self):
        roots = providers.profile_roots(self.user, self.user / "roaming")
        self.assertEqual({p for name, p in roots if name == "Claude"},
                         {self.user / n for n in (".claude", ".claude-acct2", "custom")})

    def test_alias_of_default_is_deduplicated(self):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.user / ".claude" / "../.claude")}):
            homes = runtime.claude_homes(self.user)
        self.assertEqual(len(homes), 2)

    def test_linked_home_and_projects_are_skipped(self):
        from manager.model import is_link
        for target in (self.user / ".claude-acct2", self.user / ".claude-acct2" / "projects"):
            with self.subTest(target=target), patch("manager.model.is_link", side_effect=lambda p: Path(p) == target or is_link(p)):
                self.assertNotIn(self.user / ".claude-acct2", runtime.claude_homes(self.user))

    def write_session(self, home, sid="session"):
        folder = self.user / home / "sessions"
        folder.mkdir(exist_ok=True)
        path = folder / "123.json"
        path.write_text(json.dumps({"pid": 123, "sessionId": sid}), encoding="utf8")
        return path

    def test_sessions_found_in_each_home_even_with_other_config(self):
        process = Mock()
        process.Process.return_value.create_time.return_value = 1
        for home in (".claude", ".claude-acct2", "custom"):
            with self.subTest(home=home):
                path = self.write_session(home)
                with patch("manager.runtime.Path.home", return_value=self.user), patch.object(runtime, "psutil", process):
                    self.assertEqual(runtime.claude_session(123)["sessionId"], "session")
                path.unlink()

    def test_conflicting_or_stale_ownership_fails_closed(self):
        first = self.write_session(".claude", "a")
        second = self.write_session(".claude-acct2", "b")
        process = Mock()
        process.Process.return_value.create_time.return_value = 1
        with patch("manager.runtime.Path.home", return_value=self.user), patch.object(runtime, "psutil", process):
            self.assertIsNone(runtime.claude_session(123))
            second.unlink()
            os.utime(first, (1, 1))
            process.Process.return_value.create_time.return_value = 100
            self.assertIsNone(runtime.claude_session(123))

    def test_artifacts_are_never_app_locked(self):
        entry = Entry("Artifacts", self.user / "orca", "artifact:x", "x", "x", 0, kind="artifact")
        self.assertIn("Artifacts", runtime.PROVIDERS)
        for live in (runtime.LiveApps(runtime.PROVIDERS, claude_all=True), set(runtime.PROVIDERS)):
            self.assertFalse(runtime.entry_running(entry, live))
        self.assertTrue(runtime.workspace_blocker({"Herdr"}))


class HerdrLiveTests(unittest.TestCase):
    def run_rows(self, rows):
        return patch("manager.herdr.subprocess.run", return_value=Mock(stdout=json.dumps(rows)))

    @patch("manager.herdr.shutil.which", return_value="hc")
    def test_live_only_and_extended_paths(self, _):
        cwd = str(Path.cwd())
        extended = "\\\\?\\" + cwd if os.name == "nt" else cwd
        with self.run_rows({"terminals": [{"state": "live", "cwd": extended},
                                        {"state": "finished", "cwd": "bad"}]}) as run:
            self.assertEqual(herdr.live_cwds(), [Path(cwd)])
        self.assertEqual(run.call_args.args[0][-2:], ["ls", "--json"])
        self.assertLessEqual(run.call_args.kwargs["timeout"], 5)
        self.assertEqual(run.call_args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @patch("manager.herdr.shutil.which", return_value="hc")
    def test_malformed_unknown_and_subprocess_failures_return_none(self, _):
        for data in ({}, {"terminals": {}}, {"terminals": [None]},
                     {"terminals": [{"state": "unknown"}]},
                     {"terminals": [{"state": "live"}]},
                     {"terminals": [{"state": "live", "cwd": "relative"}]}):
            with self.subTest(data=data), self.run_rows(data):
                self.assertIsNone(herdr.live_cwds())
        for exc in (OSError("missing"), subprocess.TimeoutExpired("hc", 3),
                    subprocess.CalledProcessError(1, "hc"), ValueError("bad json")):
            with self.subTest(error=type(exc)), patch("manager.herdr.subprocess.run", side_effect=exc):
                self.assertIsNone(herdr.live_cwds())

    def test_missing_hc_fails_closed(self):
        with patch("manager.herdr.Path.is_file", return_value=False), patch("manager.herdr.shutil.which", return_value=None):
            self.assertIsNone(herdr.live_cwds())

    @patch("manager.herdr.shutil.which", return_value="hc")
    def test_empty_success_and_foreground_folder(self, _):
        with self.run_rows({"terminals": []}):
            self.assertEqual(herdr.live_cwds(), [])
        cwd = str(Path.cwd())
        with self.run_rows([{"state": "live", "cwd": "bad", "foreground": {"cwd": cwd}}]):
            self.assertEqual(herdr.live_cwds(), [Path(cwd)])

    def test_root_like_does_not_include_projects(self):
        home = Path.home()
        for path in (home, *home.parents):
            self.assertTrue(herdr.is_root_like(path))
        self.assertFalse(herdr.is_root_like(home / "project"))
