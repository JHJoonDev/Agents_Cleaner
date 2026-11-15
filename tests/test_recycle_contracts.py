"""Exercise Windows recycle guard/marshalling with pure Python COM substitutes."""
import contextlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
import manager.recycle as native

FLAGS=0x80000|0x20000000|0x100000|0x400|0x4|0x10|0x4000

@contextlib.contextmanager
def fake_backend(target,*,drive=3,action='delete',scheduled=None):
    state=SimpleNamespace(calls=[],performed=False,flags=None,releases=[])
    def create(cls,outer,context,interface,out):
        assert context==1
        out._obj.value=1;return 0
    def item(path,context,interface,out):out._obj.value=2;return 0
    def initialize(reserved,mode):assert mode==2;return 0
    ole=SimpleNamespace(CoInitializeEx=Mock(side_effect=initialize),CoCreateInstance=Mock(side_effect=create),CoUninitialize=Mock())
    kernel=SimpleNamespace(GetDriveTypeW=Mock(return_value=drive))
    shell=SimpleNamespace(SHCreateItemFromParsingName=Mock(side_effect=item))
    def function_type(restype,*types):
        def resolve(index):
            def call(obj,*args):
                assert index in{2,5,18,21,22},f'unexpected IFileOperation slot {index}'
                state.calls.append(index)
                if index==2:state.releases.append(obj.value)
                if index==5:
                    state.flags=args[0];assert state.flags==FLAGS
                if index==18 and scheduled:scheduled()
                if index==21:
                    state.performed=True
                    if action=='delete':target.unlink()
                if index==22:args[0]._obj.value=int(action=='abort')
                return 0
            return call
        return resolve
    table=SimpleNamespace(contents=list(range(23)))
    with patch.object(native.ctypes,'windll',SimpleNamespace(kernel32=kernel,ole32=ole,shell32=shell)),patch.object(native.ctypes,'cast',return_value=table),patch.object(native.ctypes,'WINFUNCTYPE',side_effect=function_type):
        yield state,ole

def test_valid_local_recycle_uses_required_undo_flags_and_releases_both_interfaces():
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'synthetic')
        with fake_backend(target)as(state,ole):native.recycle(target)
        assert state.flags==FLAGS and state.performed and not target.exists()
        assert sorted(state.releases)==[1,2]
        ole.CoUninitialize.assert_called_once_with()

@pytest.mark.parametrize('drive',[0,1,2,4,5,6])
def test_only_a_fixed_local_drive_is_eligible(drive):
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'preserve')
        with fake_backend(target,drive=drive)as(state,ole),pytest.raises(RuntimeError):native.recycle(target)
        assert not state.performed and target.exists()
        ole.CoInitializeEx.assert_not_called()

@pytest.mark.parametrize('bad',['relative.txt','C:/','C:/fake/../file','C:/fake/file:stream',r'\\server\share\file'])
def test_invalid_path_forms_never_reach_com_even_if_metadata_says_they_exist(bad):
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'preserve');info=target.lstat()
        with fake_backend(target)as(state,ole),patch.object(Path,'exists',return_value=True),patch.object(Path,'lstat',return_value=info),patch.object(native,'is_link',return_value=False):
            with pytest.raises((ValueError,RuntimeError)):native.recycle(Path(bad))
            ole.CoInitializeEx.assert_not_called()
            assert not state.performed

def test_missing_and_initial_reparse_targets_are_rejected_before_com():
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'preserve')
        with fake_backend(target)as(state,ole):
            with pytest.raises(ValueError):native.recycle(target.with_name('missing'))
            with patch.object(native,'is_link',return_value=True),pytest.raises(ValueError):native.recycle(target)
            assert not state.performed
            ole.CoInitializeEx.assert_not_called()

@pytest.mark.parametrize('action',['abort','no-op'])
def test_aborted_or_incomplete_native_operations_are_never_reported_as_success(action):
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'preserve')
        with fake_backend(target,action=action)as(state,ole),pytest.raises(RuntimeError):native.recycle(target)
        assert state.performed and target.exists()
        assert sorted(state.releases)==[1,2]
        ole.CoUninitialize.assert_called_once()

def test_identity_change_after_shell_item_creation_blocks_perform_operations():
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'preserve');before=target.stat()
        def changed():os.utime(target,ns=(before.st_atime_ns,before.st_mtime_ns+1_000_000_000))
        with fake_backend(target,scheduled=changed)as(state,ole),pytest.raises(RuntimeError):native.recycle(target)
        assert not state.performed and target.exists()
        assert sorted(state.releases)==[1,2]

def test_late_reparse_change_blocks_perform_operations_independently_of_identity():
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'preserve');changed=[False]
        def scheduled():changed[0]=True
        with fake_backend(target,scheduled=scheduled)as(state,ole),patch.object(native,'is_link',side_effect=lambda path:changed[0]),pytest.raises(RuntimeError):native.recycle(target)
        assert not state.performed and target.exists()

def test_non_windows_host_is_refused_without_initializing_com():
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'preserve')
        with fake_backend(target)as(state,ole),patch.object(native,'os',SimpleNamespace(name='posix')),pytest.raises(RuntimeError):native.recycle(target)
        ole.CoInitializeEx.assert_not_called()
        assert not state.performed

def test_failed_com_initialization_preserves_target_and_reports_the_unsigned_hresult():
    with tempfile.TemporaryDirectory()as temporary:
        target=Path(temporary)/'fixture';target.write_bytes(b'preserve')
        with fake_backend(target)as(state,ole):
            ole.CoInitializeEx.side_effect=lambda *args:-2147467259
            with pytest.raises(OSError,match='80004005'):native.recycle(target)
            ole.CoCreateInstance.assert_not_called()
            ole.CoUninitialize.assert_not_called()
        assert not state.performed and target.exists()
