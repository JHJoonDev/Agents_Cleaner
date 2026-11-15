from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
import manager.recycle as native

def test_failed_com_creation_never_releases_null_interfaces_and_uninitializes_once(tmp_path):
    target = tmp_path / 'synthetic-target'
    target.write_bytes(b'preserve')
    released = []
    ole = SimpleNamespace(CoInitializeEx=Mock(return_value=0),
        CoCreateInstance=Mock(return_value=-2147467259), CoUninitialize=Mock())
    shell = SimpleNamespace(SHCreateItemFromParsingName=Mock())
    kernel = SimpleNamespace(GetDriveTypeW=Mock(return_value=3))
    def function_type(*types):
        def resolve(index):
            assert index == 'release'
            def release(obj):
                released.append(obj.value)
                return 0
            return release
        return resolve
    table = SimpleNamespace(contents=[None, None, 'release'])
    with patch.object(native.ctypes, 'windll', SimpleNamespace(kernel32=kernel, ole32=ole, shell32=shell)), \
         patch.object(native.ctypes, 'cast', return_value=table), \
         patch.object(native.ctypes, 'WINFUNCTYPE', side_effect=function_type):
        with pytest.raises(OSError, match='80004005'):
            native.recycle(target)
    assert released == []
    ole.CoInitializeEx.assert_called_once()
    ole.CoCreateInstance.assert_called_once()
    ole.CoUninitialize.assert_called_once()
    shell.SHCreateItemFromParsingName.assert_not_called()
    assert target.read_bytes() == b'preserve'
