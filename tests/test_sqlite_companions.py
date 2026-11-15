from pathlib import Path
from unittest.mock import patch
import pytest
from manager.project_sizes import protected, scan_project

@pytest.mark.parametrize('extension', ['.db', '.sqlite', '.sqlite3'])
@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
@pytest.mark.parametrize('container', ['', 'output', 'node_modules'])
def test_sqlite_companions_block_file_and_parent_cleanup(tmp_path, extension, suffix, container):
    folder = tmp_path / container
    folder.mkdir(exist_ok=True)
    companion = folder / ('state' + extension + suffix)
    companion.write_bytes(b'synthetic transaction data')
    before = companion.read_bytes()
    assert protected(companion)
    assert protected(Path(companion.name.upper()))
    with patch('manager.artifacts._git_problem', return_value=''):
        project = scan_project(tmp_path, large=1)
    candidate = next(c for c in project.candidates if c.path == (folder if container else companion))
    assert candidate.blocked
    assert project.reclaimable == 0
    assert companion.read_bytes() == before

@pytest.mark.parametrize('name', ['build.log', 'journal.txt', 'notes.db-journal.txt', 'cache.sqlite-shm.backup', 'archive.sqlite3-wal.zip'])
def test_unrelated_files_do_not_become_protected_by_substring(name):
    assert not protected(Path(name))
