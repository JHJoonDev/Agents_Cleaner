"""Synthetic DB signatures only: never open a real database or invoke deletion."""
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings, strategies as st
import pytest

from manager.project_sizes import protected, scan_project

SQLITE_HEADER = b'SQLite format 3\0'


def lmdb_header(offset=16, byteorder='little'):
    return bytes(offset) + (0xBEEFC0DE).to_bytes(4, byteorder) + bytes(12)


def make_database(folder, kind, name='storage', offset=16, byteorder='little'):
    folder.mkdir(parents=True, exist_ok=True)
    if kind == 'sqlite':
        paths = [folder / name]
        paths[0].write_bytes(SQLITE_HEADER + bytes(32))
    elif kind == 'lmdb':
        paths = [folder / 'data.mdb', folder / 'lock.mdb']
        paths[0].write_bytes(lmdb_header(offset, byteorder))
        paths[1].write_bytes(b'synthetic lock')
    elif kind == 'nosubdir':
        paths = [folder / name, folder / (name + '-lock')]
        paths[0].write_bytes(lmdb_header(offset, byteorder))
        paths[1].write_bytes(b'synthetic lock')
    else:
        database = folder / name
        database.mkdir()
        paths = [database / part for part in ['CURRENT', 'MANIFEST-000001', '000002.ldb', '000003.sst', '000004.log', 'LOCK', 'LOG']]
        for path in paths:
            path.write_bytes(b'synthetic database member')
    return paths


def assert_not_reclaimable(root, paths):
    before = {path: path.read_bytes() for path in paths}
    with patch('manager.artifacts._git_problem', return_value=''):
        project = scan_project(root, large=1)
    assert project.reclaimable == 0
    assert all(candidate.blocked for candidate in project.candidates)
    assert {path: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize('container', ['', 'output', 'node_modules'])
@pytest.mark.parametrize('name', ['storage', 'archive.bin', 'records.txt', 'cache.data'])
def test_sqlite_header_protects_unknown_extensions_and_parent_candidates(tmp_path, container, name):
    paths = make_database(tmp_path / container, 'sqlite', name)
    assert protected(paths[0])
    assert_not_reclaimable(tmp_path, paths)


@pytest.mark.parametrize('container', ['', 'output', 'node_modules'])
@pytest.mark.parametrize('kind', ['lmdb', 'nosubdir'])
@pytest.mark.parametrize('offset', [12, 16])
@pytest.mark.parametrize('byteorder', ['little', 'big'])
def test_lmdb_pair_protects_both_members_and_parent_candidates(tmp_path, container, kind, offset, byteorder):
    paths = make_database(tmp_path / container, kind, offset=offset, byteorder=byteorder)
    assert all(protected(path) for path in paths)
    assert_not_reclaimable(tmp_path, paths)


@pytest.mark.parametrize('container', ['', 'output', 'node_modules'])
def test_leveldb_and_rocksdb_directory_is_protected_as_a_whole(tmp_path, container):
    paths = make_database(tmp_path / container, 'leveldb')
    assert protected(paths[0].parent)
    assert all(protected(path) for path in paths)
    assert_not_reclaimable(tmp_path, paths)


@pytest.mark.parametrize('container', ['', 'output', 'node_modules'])
def test_standard_lmdb_pair_is_protected_even_during_initialization(tmp_path, container):
    folder = tmp_path / container
    folder.mkdir(exist_ok=True)
    paths = [folder / 'data.mdb', folder / 'lock.mdb']
    for path in paths:
        path.write_bytes(b'initializing database')
    assert all(protected(path) for path in paths)
    assert_not_reclaimable(tmp_path, paths)


@pytest.mark.parametrize('container', ['', 'output', 'node_modules'])
@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_header_detected_sqlite_also_protects_its_companions(tmp_path, container, suffix):
    paths = make_database(tmp_path / container, 'sqlite')
    companion = paths[0].with_name(paths[0].name + suffix)
    companion.write_bytes(b'synthetic transaction state')
    assert protected(companion)
    assert_not_reclaimable(tmp_path, [*paths, companion])


@pytest.mark.parametrize('names', [
    ['LOG', 'LOCK', 'CURRENT'], ['MANIFEST-000001', 'LOG', 'LOCK'],
    ['storage', 'storage-lock'],
    ['storage'], ['storage-lock'],
])
def test_generic_names_or_pairs_without_magic_remain_reclaimable(tmp_path, names):
    for name in names:
        (tmp_path / name).write_bytes(b'ordinary disposable fixture')
    assert all(not protected(tmp_path / name) for name in names)
    with patch('manager.artifacts._git_problem', return_value=''):
        project = scan_project(tmp_path, large=1)
    assert project.reclaimable > 0


@pytest.mark.parametrize('prefix', [b'SQLite format 2\0', b'SQLite format 3', b'not a database!\0'])
def test_sqlite_signature_requires_all_sixteen_bytes(tmp_path, prefix):
    target = tmp_path / 'ordinary'
    target.write_bytes(prefix)
    assert not protected(target)


@pytest.mark.parametrize('kind,expected', [('sqlite', [16]), ('nosubdir', [16, 20])])
def test_detection_reads_bounded_headers_only_without_database_locks(tmp_path, monkeypatch, kind, expected):
    paths = make_database(tmp_path, kind)
    original = Path.open
    sizes = []

    class Reader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def read(self, size):
            sizes.append(size)
            assert 0 < size <= 20
            return self.stream.read(size)

    def read_only(path, mode='r', *args, **kwargs):
        assert mode == 'rb'
        assert kwargs.get('buffering') == 0
        return Reader(original(path, mode, *args, **kwargs))

    monkeypatch.setattr(Path, 'open', read_only)
    assert protected(paths[0])
    assert sizes == expected


@pytest.mark.parametrize('container', ['', 'output', 'node_modules'])
def test_unreadable_signature_fails_closed_for_file_and_ancestor(tmp_path, monkeypatch, container):
    paths = make_database(tmp_path / container, 'sqlite')
    original = Path.open

    def unreadable(path, *args, **kwargs):
        if path == paths[0]:
            raise PermissionError('synthetic unreadable header')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', unreadable)
    with patch('manager.artifacts._git_problem', return_value=''):
        project = scan_project(tmp_path, large=1)
    assert project.reclaimable == 0
    assert project.errors
    assert all(candidate.blocked for candidate in project.candidates)


@pytest.mark.parametrize('offset', [12, 16])
def test_nosubdir_magic_without_a_lock_pair_does_not_protect_arbitrary_files(tmp_path, offset):
    path = tmp_path / 'ordinary'
    path.write_bytes(lmdb_header(offset))
    assert not protected(path)


@pytest.mark.parametrize('offset', [0, 4, 8, 20])
def test_nosubdir_pair_requires_magic_at_a_database_meta_header(tmp_path, offset):
    path = tmp_path / 'ordinary'
    path.write_bytes(lmdb_header(offset))
    path.with_name('ordinary-lock').write_bytes(b'ordinary fixture')
    assert not protected(path)


@settings(max_examples=60, derandomize=True, deadline=None)
@given(kind=st.sampled_from(['sqlite', 'lmdb', 'nosubdir', 'leveldb']),
       container=st.sampled_from(['', 'output', 'node_modules', 'build/deep']),
       parts=st.lists(st.text(alphabet='abcXYZ012_한글', min_size=1, max_size=8), min_size=1, max_size=4, unique=True))
def test_generated_trees_never_offer_database_or_ancestor_for_cleanup(kind, container, parts):
    import tempfile
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        branch = root.joinpath(*parts)
        paths = make_database(branch / container, kind, name='database-payload')
        before = {path: path.read_bytes() for path in paths}
        (root / 'disposable.tmp').write_bytes(b'ordinary fixture')
        with patch('manager.artifacts._git_problem', return_value=''):
            project = scan_project(root, large=1)
        for candidate in project.candidates:
            if not candidate.blocked:
                assert all(candidate.path != path and candidate.path not in path.parents for path in paths)
        assert {path: path.read_bytes() for path in paths} == before
