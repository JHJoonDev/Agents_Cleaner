"""Python 3.11+. Prefer PySide6; keep Tkinter as an optional fallback."""
import argparse
import json
import sys
from collections import Counter


def main():
    parser = argparse.ArgumentParser(description="AI 프로젝트·대화 체크리스트 정리 (원본 작업 폴더 보존)")
    parser.add_argument("--inventory", action="store_true", help="읽기 전용 통계만 출력. 제목·내용은 출력하지 않음")
    parser.add_argument("--read-only", action="store_true", help="GUI에서 삭제 기능 비활성화")
    args = parser.parse_args()
    if args.inventory:
        from manager.providers import scan
        entries, errors = scan()
        print(json.dumps({"counts": dict(Counter(e.provider for e in entries)),
                          "conversations": sum(e.kind == "conversation" for e in entries),
                          "caches": sum(e.kind == "cache" for e in entries), "errors": errors}, ensure_ascii=True, indent=2))
        return
    run_gui(allow_delete=not args.read_only)


def run_gui(allow_delete=True):
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6가 없어 기존 Tkinter 화면으로 실행합니다.")
        from manager.ui import App
        App(allow_delete=allow_delete).mainloop()
        return
    from manager.qt_ui import App
    application = QApplication.instance() or QApplication(sys.argv[:1])
    window = App(allow_delete=allow_delete)
    window.show()
    application.exec()


if __name__ == "__main__":
    main()
