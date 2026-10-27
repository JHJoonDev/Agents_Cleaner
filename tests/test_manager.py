import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager.model import ClosingConnection, Entry, signature
from manager.operations import backup, delete, list_backups, remove_backups, validate
from manager.providers import clean_path, scan


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-storage-test-")
        self.base = Path(self.temp.name)
        self.home = self.base / "agent"
        self.home.mkdir()
        self.original = self.base / "original-project"
        self.original.mkdir()
        (self.original / "source.py").write_text("keep source", encoding="utf8")
        self.dest = self.base / "backup"
        self.bin = self.base / "recycle"
        self.bin.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def recycle(self, p):
        shutil.move(str(p), str(self.bin / (uuid.uuid4().hex + "-" + p.name)))

    def claude(self, count=2):
        folder = self.home / "projects" / "encoded-project"
        folder.mkdir(parents=True)
        ids = [str(uuid.uuid4()) for _ in range(count)]
        for sid in ids:
            row = {"sessionId": sid, "cwd": str(self.original), "type": "user", "message": {"content": "Test chat"}}
            (folder / f"{sid}.jsonl").write_text(json.dumps(row) + "\n", encoding="utf8")
        (folder / "memory").mkdir()
        (folder / "memory" / "MEMORY.md").write_text("project memory", encoding="utf8")
        (self.home / "history.jsonl").write_text("".join(json.dumps({"sessionId": x}) + "\n" for x in ids) + "malformed\n", encoding="utf8")
        entries, errors = scan([("Claude", self.home)])
        self.assertFalse(errors)
        return entries

    def apply(self, entries, groups=(), **kwargs):
        expected = {e.key: signature(e) for e in entries}
        archive, good, failed, self.notes = delete(entries, expected, self.dest, groups, detector=lambda: set(),
                                                   recycler=self.recycle, **kwargs)
        return archive, good, failed

    def test_only_checked_session_deleted_original_auth_and_other_chat_preserved(self):
        entries = self.claude()
        secret = self.home / ".credentials.json"
        secret.write_text("private", encoding="utf8")
        chosen, untouched = entries
        archive, good, failed = self.apply([chosen])
        self.assertEqual(len(good), 1)
        self.assertFalse(failed)
        self.assertFalse(chosen.files[0].exists())
        self.assertTrue(untouched.files[0].exists())
        self.assertEqual(secret.read_text(), "private")
        self.assertTrue((self.original / "source.py").exists())
        history = (self.home / "history.jsonl").read_text()
        self.assertNotIn(chosen.session, history)
        self.assertIn(untouched.session, history)
        self.assertIn("malformed", history)
        with zipfile.ZipFile(archive) as z:
            self.assertIsNone(z.testzip())
            self.assertFalse(any("credentials" in p for p in z.namelist()))
            self.assertTrue(any(chosen.session in p for p in z.namelist()))

    def test_project_selection_deletes_only_app_container(self):
        entries = self.claude()
        folder = Path(entries[0].extra["project_dir"])
        shutil.rmtree(folder / "memory")
        archive, good, failed = self.apply(entries, {entries[0].group})
        self.assertEqual(len(good), 2)
        self.assertFalse(failed)
        self.assertFalse(folder.exists())
        self.assertTrue(self.original.exists())

    def test_project_selection_keeps_claude_project_memory(self):
        entries = self.claude()
        folder = Path(entries[0].extra["project_dir"])
        archive, good, failed = self.apply(entries, {entries[0].group})
        self.assertEqual(len(good), 2)
        self.assertFalse(failed)
        self.assertEqual((folder / "memory" / "MEMORY.md").read_text(), "project memory")
        self.assertFalse(list(folder.glob("*.jsonl")))
        self.assertTrue(any("메모리" in n for n in self.notes))
        with zipfile.ZipFile(archive) as z:
            self.assertTrue(any(p.endswith("MEMORY.md") for p in z.namelist()))

    def test_unknown_remaining_session_prevents_container_deletion(self):
        entries = self.claude()
        folder = Path(entries[0].extra["project_dir"])
        _, good, failed = self.apply([entries[0]], {entries[0].group})
        self.assertEqual(len(good), 1)
        self.assertTrue(failed)
        self.assertTrue(folder.exists())
        self.assertTrue(entries[1].files[0].exists())

    def test_active_app_blocked_before_any_backup_or_delete(self):
        e = self.claude(1)[0]
        with self.assertRaises(RuntimeError):
            delete([e], {e.key: signature(e)}, self.dest, detector=lambda: {"Claude"}, recycler=self.recycle)
        self.assertFalse(self.dest.exists())
        self.assertTrue(e.files[0].exists())

    def test_changed_file_blocked(self):
        e = self.claude(1)[0]
        old = signature(e)
        e.files[0].write_text("changed chat", encoding="utf8")
        with self.assertRaises(ValueError):
            delete([e], {e.key: old}, self.dest, detector=lambda: set(), recycler=self.recycle)
        self.assertTrue(e.files[0].exists())

    def test_app_restarting_after_backup_prevents_delete(self):
        e = self.claude(1)[0]
        calls = iter([set(), {"Claude"}])
        archive, good, failed, _ = delete([e], {e.key: signature(e)}, self.dest,
                                         detector=lambda: next(calls), recycler=self.recycle)
        self.assertTrue(archive.exists())
        self.assertFalse(good)
        self.assertTrue(failed)
        self.assertTrue(e.files[0].exists())

    def test_link_cannot_escape_agent_home(self):
        link = self.home / "cache"
        try:
            link.symlink_to(self.original, target_is_directory=True)
        except OSError:
            import _winapi
            _winapi.CreateJunction(str(self.original), str(link))
        e = Entry("Claude", self.home, "cache:cache", "cache", "", 0, [link], "cache")
        with self.assertRaises(ValueError):
            validate(e)
        self.assertTrue((self.original / "source.py").exists())

    def test_cache_group_selection_has_no_project_container(self):
        folder = self.home / "cache"
        folder.mkdir()
        (folder / "temporary.bin").write_bytes(b"cache")
        entries, errors = scan([("Claude", self.home)])
        self.assertFalse(errors)
        archive, good, failed = self.apply(entries, {entries[0].group})
        self.assertEqual(len(good), 1)
        self.assertFalse(failed)
        self.assertFalse(folder.exists())
        self.assertTrue(archive.exists())

    def test_cache_backup_is_optional(self):
        folder = self.home / "cache"
        folder.mkdir()
        (folder / "temporary.bin").write_bytes(b"cache")
        entries, _ = scan([("Claude", self.home)])
        archive, good, failed = self.apply(entries, include_caches=False)
        self.assertEqual(len(good), 1)
        self.assertFalse(folder.exists())
        with zipfile.ZipFile(archive) as z:
            self.assertFalse(any(n.endswith("temporary.bin") for n in z.namelist()))
            self.assertFalse(json.loads(z.read("manifest.json"))[0]["backed_up"])

    def test_remove_backups_only_accepts_own_zips(self):
        e = self.claude(1)[0]
        archive, _, _ = self.apply([e])
        other = self.dest / "notes.zip"
        other.write_bytes(b"keep")
        outside = self.base / "ai-records-outside.zip"
        outside.write_bytes(b"keep")
        for bad in (other, outside, self.original / "source.py"):
            with self.assertRaises(ValueError):
                remove_backups([bad], self.dest)
        self.assertEqual([r[0] for r in list_backups(self.dest)], [archive])
        remove_backups([archive], self.dest)
        self.assertFalse(archive.exists())
        self.assertTrue(other.exists() and outside.exists())

    def test_extended_length_paths_are_shown_normally(self):
        self.assertEqual(clean_path(r"\\?\C:\Work\app"), r"C:\Work\app")
        self.assertEqual(clean_path(r"\\?\UNC\server\share"), r"\\server\share")
        self.assertEqual(clean_path(r"C:\Work"), r"C:\Work")

    def test_corrupt_backup_prevents_delete(self):
        e = self.claude(1)[0]
        with patch("zipfile.ZipFile.testzip", return_value="bad-entry"):
            with self.assertRaises(RuntimeError):
                self.apply([e])
        self.assertTrue(e.files[0].exists())

    def test_root_and_config_cannot_be_cache_targets(self):
        for path in (self.home, self.home / "settings.json", self.original):
            e = Entry("Claude", self.home, "cache:cache", "bad", "", 0, [path], "cache")
            with self.assertRaises(ValueError):
                validate(e)

    def test_real_source_inside_cache_is_protected(self):
        p = self.home / "cache"
        p.mkdir()
        e = Entry("Claude", self.home, "cache:cache", "cache", "", 0, [p], "cache",
                  extra={"protected_projects": [str(p / "original")]})
        with self.assertRaises(ValueError):
            validate(e)

    def test_backup_inside_delete_target_is_rejected(self):
        e = self.claude(1)[0]
        folder = Path(e.extra["project_dir"])
        with self.assertRaises(ValueError):
            backup([e], folder / "backup", {e.group})
        self.assertFalse((folder / "backup").exists())

    def test_backup_failure_leaves_original_intact(self):
        e = self.claude(1)[0]
        with patch("manager.operations.backup", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.apply([e])
        self.assertTrue(e.files[0].exists())

    def test_invalid_project_container_rejected(self):
        e = self.claude(1)[0]
        e.extra["project_dir"] = str(self.original)
        with self.assertRaises(ValueError):
            self.apply([e], {e.group})
        self.assertTrue((self.original / "source.py").exists())

    def test_gemini_tmp_is_not_a_bulk_cache(self):
        folder = self.home / "tmp" / "hash"
        chats = folder / "chats"
        chats.mkdir(parents=True)
        (folder / ".project_root").write_text(str(self.original), encoding="utf8")
        sid = str(uuid.uuid4())
        p = chats / "session-test.json"
        p.write_text(json.dumps({"sessionId": sid, "messages": [{"type": "user", "content": "hello"}]}), encoding="utf8")
        entries, errors = scan([("Gemini", self.home)])
        self.assertFalse(errors)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].kind, "conversation")
        _, good, failed = self.apply(entries)
        self.assertFalse(failed)
        self.assertTrue(folder.exists())
        self.assertTrue(self.original.exists())

    def test_codex_uses_native_uuid_command_and_snapshots_db(self):
        sid = str(uuid.uuid4())
        other = str(uuid.uuid4())
        dbpath = self.home / "state_5.sqlite"
        with sqlite3.connect(dbpath, factory=ClosingConnection) as db:
            db.executescript("create table threads(id text, cwd text, title text, updated_at integer, rollout_path text, project_id text); create table projects(id text); create table project_roots(project_id text,path text); create table project_idempotency_keys(project_id text);")
            db.execute("insert into projects values('project-1')")
            for ident in (sid, other):
                p = self.home / "sessions" / f"rollout-{ident}.jsonl"
                p.parent.mkdir(exist_ok=True)
                p.write_text("chat", encoding="utf8")
                db.execute("insert into threads values(?,?,?,?,?,?)", (ident, str(self.original), "chat", 100, str(p), "project-1"))
        entries, errors = scan([("Codex", self.home)])
        self.assertFalse(errors)
        chosen = next(e for e in entries if e.session == sid)
        calls = []
        def native(exe, args, **kwargs):
            calls.append((exe, args, kwargs["env"]["CODEX_HOME"]))
            with sqlite3.connect(dbpath, factory=ClosingConnection) as db:
                db.execute("delete from threads where id=?", (sid,))
            chosen.files[0].unlink()
        archive, good, failed = self.apply([chosen], {chosen.group}, runner=native)
        self.assertFalse(failed)
        self.assertEqual(calls[0], ("codex", ["--no-daemon", "delete", sid, "--force"], str(self.home)))
        with sqlite3.connect(dbpath, factory=ClosingConnection) as db:
            self.assertIsNotNone(db.execute("select 1 from threads where id=?", (other,)).fetchone())
            self.assertIsNotNone(db.execute("select 1 from projects").fetchone())
        self.assertTrue(any("Codex 프로젝트 등록은 보존" in n for n in self.notes))
        with zipfile.ZipFile(archive) as z:
            snapshot = next(n for n in z.namelist() if n.endswith("database-snapshot/state_5.sqlite"))
            extracted = self.base / "snapshot.sqlite"
            extracted.write_bytes(z.read(snapshot))
            with sqlite3.connect(extracted, factory=ClosingConnection) as db:
                self.assertEqual(db.execute("select count(*) from threads").fetchone()[0], 2)
        self.assertTrue(self.original.exists())


    def test_codex_compressed_rollout_is_listed_and_backed_up(self):
        sid = str(uuid.uuid4())
        dbpath = self.home / "state_5.sqlite"
        rollout = self.home / "sessions" / f"rollout-{sid}.jsonl"
        rollout.parent.mkdir()
        packed = rollout.with_name(rollout.name + ".zst")
        packed.write_bytes(b"compressed chat")
        with sqlite3.connect(dbpath, factory=ClosingConnection) as db:
            db.execute("create table threads(id text, cwd text, title text, updated_at integer, rollout_path text, project_id text)")
            db.execute("insert into threads values(?,?,?,?,?,?)", (sid, "\\\\?\\" + str(self.original), "chat", 100, str(rollout), None))
        entries, errors = scan([("Codex", self.home)])
        self.assertFalse(errors)
        self.assertEqual(entries[0].files, [packed])
        self.assertEqual(entries[0].project, str(self.original))
        self.assertGreater(entries[0].size, 0)
        def native(exe, args, **kwargs):
            with sqlite3.connect(dbpath, factory=ClosingConnection) as db:
                db.execute("delete from threads where id=?", (sid,))
            packed.unlink()
        archive, good, failed = self.apply(entries, runner=native)
        self.assertFalse(failed)
        with zipfile.ZipFile(archive) as z:
            name = next(n for n in z.namelist() if n.endswith(".jsonl.zst"))
            self.assertEqual(z.read(name), b"compressed chat")


    def test_marked_work_folder_goes_to_recycle_bin_after_its_chats(self):
        entries = self.claude()
        (self.original / "result.csv").write_text("output", encoding="utf8")
        archive, good, failed = self.apply(entries, {entries[0].group}, workspaces=[str(self.original)])
        self.assertEqual(len(good), 2)
        self.assertFalse(failed)
        self.assertFalse(self.original.exists())
        self.assertTrue(any(p.name.endswith("original-project") for p in self.bin.iterdir()))
        self.assertTrue(any("작업 폴더 휴지통 이동" in n for n in self.notes))
        with zipfile.ZipFile(archive) as z:
            self.assertIn(str(self.original), json.loads(z.read("workspaces.json")))
            self.assertFalse(any(n.endswith("result.csv") for n in z.namelist()))

    def test_work_folder_needs_every_chat_that_used_it(self):
        entries = self.claude()
        with self.assertRaises(ValueError):
            self.apply([entries[0]], workspaces=[str(self.original)], all_entries=entries)
        self.assertTrue((self.original / "source.py").exists())
        self.assertTrue(entries[0].files[0].exists())

    def test_work_folder_kept_when_a_chat_fails(self):
        e = self.claude(1)[0]
        calls = iter([set(), {"Claude"}])
        _, good, failed, _ = delete([e], {e.key: signature(e)}, self.dest, detector=lambda: next(calls),
                                    recycler=self.recycle, workspaces=[str(self.original)])
        self.assertFalse(good)
        self.assertTrue((self.original / "source.py").exists())

    def test_protected_folders_are_never_work_folders(self):
        from manager.workspaces import PROGRAM_ROOT, fixed_problem
        for path in (Path.home(), Path(Path.home().anchor), Path.home() / "Desktop", self.home,
                     self.home / "projects", PROGRAM_ROOT, self.base / "missing"):
            self.assertTrue(fixed_problem(path, [self.home]), path)
        self.assertEqual(fixed_problem(self.original, [self.home]), "")

    def test_parent_folder_with_other_projects_is_blocked(self):
        from manager.workspaces import workspace_problem
        entries = self.claude(1)
        sub = Entry("Claude", self.home, str(uuid.uuid4()), "sub chat", str(self.original / "sub"), 0, [])
        reason = workspace_problem(str(self.original), [sub], set(), [self.home])
        self.assertIn("선택되지 않음", reason)

    @unittest.skipUnless(shutil.which("git"), "git 필요")
    def test_git_warnings_for_uncommitted_and_no_remote(self):
        from manager.workspaces import git_warnings
        import subprocess
        subprocess.run(["git", "init", "-q", str(self.original)], check=True)
        warnings = git_warnings(self.original)
        self.assertTrue(any("커밋하지 않은" in w for w in warnings))
        self.assertTrue(any("원격" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
