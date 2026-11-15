import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from manager.project_sizes import scan_project

@pytest.mark.parametrize('suffix',['.sqlite-wal','.sqlite-shm','.sqlite3-wal','.sqlite3-shm','.db-journal','.sqlite-journal','.sqlite3-journal'])
@pytest.mark.xfail(strict=True,reason='QA-BUG: project-size planner protects SQLite DBs but not their WAL/SHM companions')
def test_sqlite_companion_files_are_protected_like_the_database(suffix):
    with tempfile.TemporaryDirectory()as temporary:
        root=Path(temporary);(root/('state'+suffix.rsplit('-',1)[0])).write_bytes(b'synthetic-db')
        companion=root/('state'+suffix);companion.write_bytes(b'synthetic SQLite transaction companion')
        with patch('manager.artifacts._git_problem',return_value=''):
            project=scan_project(root,large=1)
        match=next(candidate for candidate in project.candidates if candidate.path==companion)
        assert match.blocked,'SQLite companion must not be an unblocked cleanup candidate'
