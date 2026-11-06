"""Instrumented fixture deletion; NEVER measures real Codex/Recycle Bin speed.

py -3.11 tests/benchmark_delete.py --suite
py -3.11 tests/benchmark_delete.py --counts 1 10 100 --latency-ms 0 10 --cache-modes skip include
CLI/process discovery and recycle operations are simulated; record backup stays mandatory.
Fixture creation, inventory and result verification are outside measured pipeline time.
"""
import argparse
from collections import Counter, defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
import itertools
import json
import math
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from manager import operations as ops
from manager import runtime
from manager.model import ClosingConnection, signature
from manager.providers import scan


@dataclass(frozen=True)
class Case:
    count: int = 10
    rollout_kib: int = 1
    db_kib: int = 64
    latency_ms: float = 0
    cache_mode: str = "skip"
    timeout_call: int = 0


class Meter:
    def __init__(self):
        self.calls, self.inclusive, self.exclusive = Counter(), defaultdict(float), defaultdict(float)
        self.stack = []

    @contextmanager
    def stage(self, name):
        self.calls[name] += 1
        frame = [name, time.perf_counter(), 0]
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = time.perf_counter() - frame[1]
            self.inclusive[name] += elapsed
            self.exclusive[name] += elapsed - frame[2]
            self.stack.pop()
            if self.stack:
                self.stack[-1][2] += elapsed

    def wrap(self, name, function):
        def measured(*args, **kwargs):
            with self.stage(name):
                return function(*args, **kwargs)
        return measured


class Fixture:
    def __init__(self, root, case):
        self.root, self.case = Path(root), case
        self.home, self.source = self.root / "profile", self.root / "source"
        self.destination, self.trash = self.root / "backups", self.root / "fake-recycle"
        for path in (self.home, self.source, self.trash, self.home / "sessions"):
            path.mkdir(parents=True, exist_ok=True)
        (self.source / "preserve.txt").write_text("fixture source must survive", encoding="utf8")
        self.database = self.home / "state_5.sqlite"
        self.sentinel = str(uuid.UUID(int=1))
        self.paths, self.timed_out, self.attempted, self.after_timeout = {}, set(), [], []
        self.timeout_budgets, self.injected_wait, self.before_run_calls = [], 0.0, 0
        rng = random.Random(12345)
        payload = json.dumps({"type": "fixture", "payload": rng.randbytes(case.rollout_kib * 512).hex()}).encode() + b"\n"
        with sqlite3.connect(self.database, factory=ClosingConnection) as db:
            db.execute("create table threads(id text primary key,cwd text,title text,updated_at integer,rollout_path text)")
            db.execute("create table fixture_padding(payload blob)")
            db.execute("insert into fixture_padding values(?)", (rng.randbytes(case.db_kib * 1024),))
            for i in range(case.count + 1):
                sid = str(uuid.UUID(int=i + 1))
                path = self.home / "sessions" / ("rollout-" + sid + ".jsonl")
                path.write_bytes(payload)
                self.paths[sid] = path
                db.execute("insert into threads values(?,?,?,?,?)", (sid, str(self.source), "fixture " + sid, 100, str(path)))
        if case.cache_mode != "none":
            cache = self.home / "cache"
            cache.mkdir()
            for i in range(4):
                (cache / f"fixture-{i}.bin").write_bytes(rng.randbytes(32 * 1024))
        entries, errors = scan([("Codex", self.home)])
        if errors:
            raise RuntimeError(errors)
        self.entries = [e for e in entries if e.session != self.sentinel]
        self.database_bytes = self.database.stat().st_size
        self.rollout_bytes = sum(p.stat().st_size for sid, p in self.paths.items() if sid != self.sentinel)

    def contained(self, path):
        resolved = Path(path).resolve()
        resolved.relative_to(self.root.resolve())
        return resolved

    def recycle(self, path):
        path = self.contained(path)
        if self.timed_out:
            self.after_timeout.append("recycle:" + path.name)
        shutil.move(str(path), str(self.trash / (uuid.uuid4().hex + "-" + path.name)))

    def _command(self, sid, before_run, timeout):
        if sid == self.sentinel or sid not in self.paths:
            raise AssertionError("unselected or unknown fixture session")
        if not callable(before_run):
            raise AssertionError("command-hook fixture requires the fresh before_run guard")
        self.before_run_calls += 1
        before_run()
        if self.timed_out:
            self.after_timeout.append("cli:" + sid)
        self.attempted.append(sid)
        self.timeout_budgets.append(timeout)
        if self.case.latency_ms:
            started = time.perf_counter()
            time.sleep(self.case.latency_ms / 1000)
            self.injected_wait += time.perf_counter() - started
        if len(self.attempted) == self.case.timeout_call:
            self.timed_out.add(sid)
            raise subprocess.TimeoutExpired("simulated-codex", timeout)
        with sqlite3.connect(self.database, factory=ClosingConnection) as db:
            db.execute("delete from threads where id=?", (sid,))
        self.contained(self.paths[sid]).unlink()
        return "fixture deleted"

    def verify(self, archive, good, failed):
        with sqlite3.connect(self.database, factory=ClosingConnection) as db:
            remaining = {row[0] for row in db.execute("select id from threads")}
        deleted = {e.session for e in good if e.kind == "conversation"}
        expected_remaining = set(self.paths) - deleted
        if remaining != expected_remaining or self.sentinel not in remaining:
            raise AssertionError("fixture DB did not preserve unselected/failed sessions")
        for sid, path in self.paths.items():
            if path.exists() != (sid in remaining):
                raise AssertionError("fixture rollout and DB result differ")
        if not self.timed_out.issubset(remaining):
            raise AssertionError("timed-out fixture session was mutated")
        if self.after_timeout:
            raise AssertionError("mutation continued after CLI timeout: " + ", ".join(self.after_timeout))
        if self.before_run_calls != len(self.attempted):
            raise AssertionError("fresh guard and CLI attempt counts differ")
        if self.case.timeout_call and self.case.timeout_call <= self.case.count:
            if (len(self.timed_out) != 1 or len(self.attempted) != self.case.timeout_call
                    or len(deleted) != self.case.timeout_call - 1
                    or len(failed) != len(self.entries) - len(good)):
                raise AssertionError("timeout batch did not preserve all unattempted entries")
        elif failed or {e.key for e in good} != {e.key for e in self.entries}:
            raise AssertionError(f"unexpected fixture deletion failure: {failed}")
        for entry in self.entries:
            if entry.kind == "cache" and entry.files[0].exists() != (entry not in good):
                raise AssertionError("cache result does not match the filesystem")
        if (self.source / "preserve.txt").read_text(encoding="utf8") != "fixture source must survive":
            raise AssertionError("fixture source was modified")
        with zipfile.ZipFile(archive) as saved:
            if saved.testzip():
                raise AssertionError("fixture backup is corrupt")
            names = saved.namelist()
            has_cache = any("/cache/" in name for name in names)
            if has_cache != (self.case.cache_mode == "include"):
                raise AssertionError("cache backup policy was not preserved")
            manifest = json.loads(saved.read("manifest.json"))
            if len(manifest) != len(self.entries):
                raise AssertionError("backup omitted selected fixture entries")
            by_session = {item["session"]: item for item in manifest}
            if set(by_session) != {e.session for e in self.entries}:
                raise AssertionError("backup session identities differ from selection")
            for entry in self.entries:
                row = by_session[entry.session]
                if entry.kind == "cache":
                    cache_names = {name for name in names if name.startswith(row["prefix"] + "/cache/")}
                    expected_cache = {row["prefix"] + f"/cache/fixture-{i}.bin" for i in range(4)}
                    if (row["backed_up"] != (self.case.cache_mode == "include")
                            or cache_names != (expected_cache if self.case.cache_mode == "include" else set())):
                        raise AssertionError("cache archive members or manifest policy differ")
                elif not row["backed_up"] or not any(name.endswith("/sessions/" + self.paths[entry.session].name) for name in names):
                    raise AssertionError("mandatory selected rollout backup missing")
            if any(name.endswith(self.paths[self.sentinel].name) for name in names):
                raise AssertionError("unselected rollout unexpectedly archived")
            snapshots = [name for name in names if name.endswith("database-snapshot/state_5.sqlite")]
            if len(snapshots) != 1:
                raise AssertionError("fixture database was not snapshotted once")
            snapshot = self.root / "verified-snapshot.sqlite"
            snapshot.write_bytes(saved.read(snapshots[0]))
            with sqlite3.connect(snapshot, factory=ClosingConnection) as db:
                if db.execute("select count(*) from threads").fetchone()[0] != self.case.count + 1:
                    raise AssertionError("snapshot does not preserve the pre-deletion DB")
        return len(remaining) - 1


def benchmark(case):
    if case.timeout_call > case.count:
        raise ValueError("timeout-call exceeds selected conversation count")
    with tempfile.TemporaryDirectory(prefix="ai-delete-benchmark-") as tmp:
        fixture = Fixture(tmp, case)
        start = time.perf_counter()
        expected = {entry.key: signature(entry) for entry in fixture.entries}
        prepare_seconds = time.perf_counter() - start
        meter, io = Meter(), Counter()
        def forbidden(*args, **kwargs):
            raise AssertionError("real external process/recycle is forbidden in this benchmark")
        def command(exe, args, env=None, timeout=90, before_run=None, **kwargs):
            if exe != "codex" or env is None or Path(env.get("CODEX_HOME", "")) != fixture.home:
                raise AssertionError("unexpected CLI profile")
            if len(args) != 4 or args[:2] != ["--no-daemon", "delete"] or args[-1] != "--force":
                raise AssertionError("unexpected fixture CLI request")
            return fixture._command(args[2], before_run, timeout)
        simulated_command = meter.wrap("cli_simulated", command)
        def counted(name, function):
            def count(*args, **kwargs):
                io[name] += 1
                return function(*args, **kwargs)
            return count
        with ExitStack() as stack:
            stack.enter_context(patch.object(ops, "command", simulated_command))
            stack.enter_context(patch.object(runtime.subprocess, "run", forbidden))
            stack.enter_context(patch.object(runtime.subprocess, "Popen", forbidden))
            if hasattr(runtime, "_run_process"):
                stack.enter_context(patch.object(runtime, "_run_process", forbidden))
            stack.enter_context(patch.object(runtime, "running_processes", forbidden))
            stack.enter_context(patch("manager.workspaces.profile_roots", return_value=[]))
            for name in ("validate", "signature", "backup", "_target_state", "_project_state", "_snapshot_database", "read_db"):
                if hasattr(ops, name):
                    stack.enter_context(patch.object(ops, name, meter.wrap(name, getattr(ops, name))))
            for name in ("write", "testzip"):
                stack.enter_context(patch.object(zipfile.ZipFile, name, meter.wrap("zip_" + name, getattr(zipfile.ZipFile, name))))
            for name in ("stat", "lstat", "glob"):
                stack.enter_context(patch.object(Path, name, counted(name, getattr(Path, name))))
            with meter.stage("delete_total"):
                archive, good, failed, notes = ops.delete(
                    fixture.entries, expected, fixture.destination,
                    detector=meter.wrap("detector_simulated", lambda: set()), runner=simulated_command,
                    recycler=meter.wrap("recycle_simulated", fixture.recycle),
                    include_caches=case.cache_mode == "include", all_entries=fixture.entries, create_backup=True)
        verification_error = ""
        remaining = None
        try:
            remaining = fixture.verify(archive, good, failed)
        except AssertionError as exc:
            verification_error = str(exc)
        return {"mode": "simulated_command_hook", "case": asdict(case), "spawned_processes": 0,
                "total_s": round(meter.inclusive["delete_total"], 6), "prepare_signature_s": round(prepare_seconds, 6),
                "stages_inclusive_s": {k: round(v, 6) for k, v in meter.inclusive.items()},
                "stages_exclusive_s": {k: round(v, 6) for k, v in meter.exclusive.items()},
                "calls": dict(meter.calls), "io_calls": dict(io),
                "database_bytes": fixture.database_bytes, "rollout_bytes": fixture.rollout_bytes,
                "archive_bytes": archive.stat().st_size, "successes": len(good), "failures": len(failed),
                "cli_attempts": len(fixture.attempted), "injected_timeouts": len(fixture.timed_out),
                "before_run_calls": fixture.before_run_calls,
                "cli_timeout_budgets_s": fixture.timeout_budgets,
                "injected_sleep_requested_s": round(len(fixture.attempted) * case.latency_ms / 1000, 6),
                "injected_sleep_observed_s": round(fixture.injected_wait, 6),
                "timeout_injection": "immediate-exception" if case.timeout_call else "none",
                "mutations_after_timeout": len(fixture.after_timeout),
                "remaining_selected": remaining, "notes_count": len(notes),
                "verified": not verification_error, "verification_error": verification_error}


def cases(args):
    if args.suite:
        baseline = [Case(count=n, cache_mode=cache) for n in (1, 10, 100) for cache in ("skip", "include")]
        return baseline + [Case(rollout_kib=1024), Case(db_kib=4096), Case(timeout_call=3),
                           Case(count=10, latency_ms=10), Case(count=100, latency_ms=10)]
    return [Case(*values, timeout_call=args.timeout_call) for values in itertools.product(
        args.counts, args.rollout_kib, args.db_kib, args.latency_ms, args.cache_modes)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="store_true", help="11 targeted size/cache/latency/timeout scenarios")
    parser.add_argument("--counts", nargs="+", type=int, default=[1, 10, 100])
    parser.add_argument("--rollout-kib", nargs="+", type=int, default=[1])
    parser.add_argument("--db-kib", nargs="+", type=int, default=[64])
    parser.add_argument("--latency-ms", nargs="+", type=float, default=[0])
    parser.add_argument("--cache-modes", nargs="+", choices=["none", "skip", "include"], default=["skip", "include"])
    parser.add_argument("--timeout-call", type=int, default=0)
    args = parser.parse_args()
    if (min(args.counts + args.rollout_kib + args.db_kib) < 1 or min(args.latency_ms) < 0 or args.timeout_call < 0
            or not all(math.isfinite(value) for value in args.latency_ms)):
        parser.error("sizes/counts must be positive; latency/timeout-call must be nonnegative")
    if not args.suite and args.timeout_call > min(args.counts):
        parser.error("timeout-call must be reachable for every selected count")
    print(json.dumps({"warning": "SIMULATED CLI: these timings are not real Codex deletion times",
                      "python": sys.version.split()[0], "instrumented": True,
                      "phase_note": "inclusive stages overlap; exclusive stages sum to total; fixture/verification excluded"}), flush=True)
    failed = False
    for case in cases(args):
        result = benchmark(case)
        print(json.dumps(result), flush=True)
        failed |= not result["verified"]
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
