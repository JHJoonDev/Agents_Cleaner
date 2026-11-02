"""Read-only/offscreen fixture benchmark; reports timings without time assertions.

Run: py -3.11 tests/benchmark_startup.py --counts 100 1000 --repeat 3
Compare: py -3.11 tests/benchmark_startup.py --compare-ref HEAD --counts 1000 10000 --groups 20 --repeat 1
No real profiles, command discovery or process enumeration are used.
"""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
IMPORT_START = time.perf_counter()
from manager.providers import scan
from PySide6.QtWidgets import QApplication
from manager.qt_ui import App
IMPORT_SECONDS = time.perf_counter() - IMPORT_START


def duration(function):
    start = time.perf_counter()
    result = function()
    return time.perf_counter() - start, result


def benchmark(count, repeat, app, groups=1, show_window=False):
    with tempfile.TemporaryDirectory(prefix="ai-cleaner-benchmark-") as tmp:
        root = Path(tmp)
        home, project = root / "profile", root / "source"
        project.mkdir()
        folder = home / "projects" / "fixture"
        folder.mkdir(parents=True)
        projects = [project / f"project-{i}" for i in range(min(count, groups))]
        for path in projects:
            path.mkdir()
        for i in range(count):
            text = json.dumps({"cwd": str(projects[i % len(projects)]), "type": "user", "message": {"content": "fixture benchmark"}}) + "\n"
            (folder / f"{uuid.UUID(int=i + 1)}.jsonl").write_text(text, encoding="utf8")
        roots = [("Claude", home)]
        scan_times, render_times, select_times, filter_times = [], [], [], []
        create_seconds, window = duration(lambda: App(roots=roots, allow_delete=False, auto_refresh=False))
        try:
            if show_window:
                window.show()
                app.processEvents()
            for _ in range(repeat):
                seconds, (entries, errors) = duration(lambda: scan(roots))
                if errors or len(entries) != count:
                    raise RuntimeError(f"fixture scan mismatch: {len(entries)} entries, {len(errors)} errors")
                scan_times.append(seconds)
                size_seconds, _ = duration(lambda: [e.extra.update(display_size=e.size) for e in entries])
                grouped = defaultdict(list)
                for entry in entries:
                    grouped[entry.group].append(entry)
                window.entries, window.groups = entries, dict(grouped)
                window.entry_by_key = {e.key: e for e in entries}
                seconds, _ = duration(window.render)
                render_times.append(seconds)
                seconds, _ = duration(lambda: window.set_records_checked(entries, True))
                select_times.append(seconds)
                seconds, _ = duration(lambda: window.filter_records("fixture benchmark"))
                filter_times.append(seconds)
                window.clear()
                app.processEvents()
            calls = Counter()
            original_stat = Path.stat
            def counted_stat(path, *args, **kwargs):
                calls["stat"] += 1
                return original_stat(path, *args, **kwargs)
            with patch.object(Path, "stat", counted_stat):
                scan(roots)
            scan_stat_calls = calls["stat"]
            calls.clear()
            with patch.object(Path, "stat", counted_stat):
                window.render()
                window.set_records_checked(entries, True)
                window.filter_records("")
            previous_entries = window.entries
            errors = []
            with patch("manager.qt_logic.running_processes", return_value=[]), \
                 patch("manager.qt_logic.codex_cli", return_value=("fixture-codex", "0.0.0")), \
                 patch("manager.workspaces.profile_roots", return_value=[]), \
                 patch("manager.qt_logic.message", side_effect=lambda *a, **k: errors.append(str(a[2:]))):
                start = time.perf_counter()
                window.refresh()
                longest_event_turn = 0
                while window.busy:
                    turn_start = time.perf_counter()
                    app.processEvents()
                    longest_event_turn = max(longest_event_turn, time.perf_counter() - turn_start)
                    if time.perf_counter() - start > 60:
                        raise RuntimeError("fixture refresh exceeded 60s")
                    time.sleep(.001)
                refresh_seconds = time.perf_counter() - start
            if (errors or window.errors or window.entries is previous_entries
                    or len(window.entries) != count or len(window.model.record_items) != count):
                raise RuntimeError(f"fixture refresh failed: {errors or window.errors}")
            paint_seconds = None
            if show_window:
                paint_seconds, screenshot = duration(window.tree.grab)
                if screenshot.isNull():
                    raise RuntimeError("offscreen fixture viewport could not be painted")
            return {"entries": count, "groups": len(projects), "repeat": repeat, "window_visible": show_window,
                    "window_construct_s": round(create_seconds, 6),
                    "scan_median_s": round(statistics.median(scan_times), 6), "size_last_s": round(size_seconds, 6),
                    "render_median_s": round(statistics.median(render_times), 6),
                    "select_all_median_s": round(statistics.median(select_times), 6),
                    "filter_median_s": round(statistics.median(filter_times), 6),
                    "scan_stat_calls": scan_stat_calls, "ui_stat_calls": calls["stat"],
                    "refresh_total_s": round(refresh_seconds, 6),
                    "longest_gui_event_turn_s": round(longest_event_turn, 6),
                    "viewport_paint_s": round(paint_seconds, 6) if paint_seconds is not None else None}
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()


def compare_reference(args):
    """Use the identical fixture/benchmark code for both source versions."""
    repository = Path(__file__).resolve().parents[1]
    script = Path(__file__).resolve()
    forwarded = ["--counts", *map(str, args.counts), "--repeat", str(args.repeat), "--groups", str(args.groups)]
    if args.show_window:
        forwarded.append("--show-window")
    def run(path, cwd, label):
        child = subprocess.Popen([sys.executable, str(path), *forwarded], cwd=cwd,
                                 stdout=subprocess.PIPE, text=True, encoding="utf8")
        try:
            for line in child.stdout:
                row = json.loads(line)
                print(json.dumps({"source": label, **row}), flush=True)
        finally:
            child.stdout.close()
        if child.wait():
            raise RuntimeError(f"benchmark failed: {label}")
    with tempfile.TemporaryDirectory(prefix="ai-cleaner-reference-") as tmp:
        snapshot = Path(tmp)
        names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", args.compare_ref, "--", "scripts"],
                                        cwd=repository, text=True).splitlines()
        if not names:
            raise RuntimeError("reference has no scripts")
        for name in names:
            path = snapshot / name
            path.resolve().relative_to(snapshot.resolve())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(subprocess.check_output(["git", "show", f"{args.compare_ref}:{name}"], cwd=repository))
        reference_script = snapshot / "tests" / script.name
        reference_script.parent.mkdir()
        reference_script.write_bytes(script.read_bytes())
        run(reference_script, snapshot, args.compare_ref)
        run(script, repository, "working-tree")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", type=int, nargs="+", default=[100, 1000])
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--groups", type=int, default=1)
    parser.add_argument("--compare-ref", help="read-only git reference snapshot followed by working tree")
    parser.add_argument("--show-window", action="store_true", help="also measure offscreen viewport painting")
    args = parser.parse_args()
    if args.repeat < 1 or args.groups < 1 or any(count < 1 for count in args.counts):
        parser.error("counts, groups and repeat must be positive")
    if args.compare_ref:
        compare_reference(args)
        return
    app_seconds, app = duration(lambda: QApplication.instance() or QApplication([]))
    print(json.dumps({"python": sys.version.split()[0], "platform": app.platformName(),
                      "qt_import_s": round(IMPORT_SECONDS, 6), "qapplication_s": round(app_seconds, 6)}), flush=True)
    for count in args.counts:
        print(json.dumps(benchmark(count, args.repeat, app, args.groups, args.show_window)), flush=True)


if __name__ == "__main__":
    main()
