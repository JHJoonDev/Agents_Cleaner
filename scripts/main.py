"""Python 3.11+. Prefer PySide6; keep Tkinter as an optional fallback."""
import argparse
import json
import sys
from collections import Counter


def main():
    parser = argparse.ArgumentParser(description="AI 프로젝트·대화 체크리스트 정리 (원본 작업 폴더 보존)")
    parser.add_argument("--inventory", action="store_true", help="읽기 전용 통계만 출력. 제목·내용은 출력하지 않음")
    parser.add_argument("--artifacts", action="store_true", help="읽기 전용 작업 산출물 목록을 JSON으로 출력")
    parser.add_argument("--read-only", action="store_true", help="GUI에서 삭제 기능 비활성화")
    parser.add_argument("--size-report", nargs="?", const="reports/project-sizes.md", help="용량 Markdown 보고서만 저장 (삭제 없음)")
    parser.add_argument("--size-root", action="append", help="용량 조회 루트 (여러 번 지정 가능)")
    parser.add_argument("--notify", action="store_true", help="용량 보고서 Telegram 요약")
    args = parser.parse_args()
    if args.size_report:
        from pathlib import Path
        from manager.project_sizes import scan_roots, write_report, notify_report
        projects, errors = scan_roots(args.size_root)
        target = write_report(projects, errors, args.size_report)
        print(str(target.resolve()))
        if args.notify:
            notify_report(projects, target.resolve())
        return
    if args.inventory or args.artifacts:
        from manager.providers import scan
        entries, errors = scan()
        artifacts = [e for e in entries if e.kind == "artifact"]
        if args.artifacts:
            print(json.dumps([{"category": e.extra["category"], "tier": e.extra["tier"],
                               "method": e.extra["method"], "path": str(e.files[0]),
                               "project": e.project, "bytes": e.size,
                               "idle_days": e.extra["idle_days"], "blocked": e.blocked}
                              for e in artifacts], ensure_ascii=True, indent=2))
            if errors:
                print(json.dumps({"errors": errors}, ensure_ascii=True), file=sys.stderr)
            return
        tiers = {tier: {"count": 0, "bytes": 0} for tier in (1, 2, 3)}
        for e in artifacts:
            tiers[e.extra["tier"]]["count"] += 1
            tiers[e.extra["tier"]]["bytes"] += e.size
        print(json.dumps({"counts": dict(Counter(e.provider for e in entries)),
                          "conversations": sum(e.kind == "conversation" for e in entries),
                          "caches": sum(e.kind == "cache" for e in entries),
                          "artifacts": tiers, "errors": errors}, ensure_ascii=True, indent=2))
        return
    run_gui(allow_delete=not args.read_only)


def run_gui(allow_delete=True):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AgentsCleaner.App")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6가 없어 기존 Tkinter 화면으로 실행합니다.")
        from manager.ui import App
        App(allow_delete=allow_delete).mainloop()
        return
    from manager.qt_ui import App
    if sys.platform == "win32":
        # Own taskbar group, so Windows shows the app icon instead of python's.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AgentsCleaner")
    application = QApplication.instance() or QApplication(sys.argv[:1])
    window = App(allow_delete=allow_delete)
    window.show()
    application.exec()


if __name__ == "__main__":
    main()
