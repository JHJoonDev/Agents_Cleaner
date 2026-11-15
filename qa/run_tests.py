"""Start the sandbox fence before importing pytest or collecting application tests."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import qa_pytest_guard  # noqa: E402,F401: intentionally before pytest
import pytest  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(pytest.main(['-p', 'qa_pytest_guard', *sys.argv[1:]]))
