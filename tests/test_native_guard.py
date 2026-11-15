"""Native negative controls; no real user target, COM action or shell execution."""
import ctypes
import os
from pathlib import Path
import sys
import _ctypes
import pytest
import sandbox_guard as guard

assert guard.EARLY_PLUGIN_ACTIVE
# Eager-import bypass: attempt to bind DeleteFileW BEFORE autouse fixtures.
# Original aliases bypass our module-attribute mocks and exercise the audit hook.
CACHED_DELETE = None
CACHE_DENIAL = ''
try:
    with guard.expected_denial():
        CACHED_DELETE = guard.CTYPES_LOADERS['WinDLL']('kernel32', use_last_error=True).DeleteFileW
except guard.OutsideSandbox as exc:
    CACHE_DENIAL = str(exc)

def test_cached_delete_cannot_be_bound_before_fixtures_or_delete_outer_sentinel():
    parent = (guard.ROOT / '.test-artifacts' / ('native-control-' + str(os.getpid()))).resolve()
    assert guard.ROOT / '.test-artifacts' in parent.parents
    assert guard.SANDBOX not in parent.parents
    parent.mkdir(exist_ok=True)
    sentinel = parent / 'review-owned.fixture'
    sentinel.write_text('synthetic sentinel', encoding='utf8')
    # If binding ever regresses, this can touch ONLY this owned synthetic file.
    # No arbitrary path is passed to a cached pointer.
    if CACHED_DELETE is not None:
        CACHED_DELETE.argtypes = [ctypes.c_wchar_p]
        CACHED_DELETE.restype = ctypes.c_int
        with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
            CACHED_DELETE(str(sentinel))
    assert CACHED_DELETE is None, 'native pointer escaped the pre-collection guard'
    assert 'ctypes.dlopen' in CACHE_DENIAL
    assert sentinel.read_text(encoding='utf8') == 'synthetic sentinel'

@pytest.mark.parametrize('loader', sorted(guard.CTYPES_LOADERS))
def test_original_loader_aliases_are_blocked_by_audit_before_dll_load(loader):
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox, match='ctypes.dlopen'):
        guard.CTYPES_LOADERS[loader]('kernel32')

@pytest.mark.parametrize('name', ['DeleteFileW', 'MoveFileExW', 'RemoveDirectoryW', 'SHFileOperationW', 'CoCreateInstance', 'SHCreateItemFromParsingName', 'GetProcAddress', 1])
def test_preloaded_handle_symbol_resolution_is_denied_including_unknowns_and_ordinals(name):
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        guard.PYTHON_API[name]

@pytest.mark.parametrize('loader', ['cdll', 'windll', 'oledll', 'pydll'])
def test_loader_objects_do_not_allow_alternate_native_entrypoints(loader):
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        getattr(ctypes, loader).LoadLibrary('shell32')

def test_direct_low_level_call_event_is_denied_before_any_call():
    # Zero-byte memmove is a non-destructive fallback if the hook regresses.
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox, match='ctypes.call_function'):
        _ctypes.call_function(ctypes._memmove_addr, (0, 0, 0))

@pytest.mark.parametrize('event,args', [
    ('ctypes.dlsym', (guard.PYTHON_API, 'DeleteFileW')),
    ('ctypes.dlsym/handle', (0, 'DeleteFileW')),
    ('ctypes.call_function', (0, ())),
])
def test_handle_and_call_audit_events_fail_closed(event, args):
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        sys.audit(event, *args)

@pytest.mark.parametrize('name', ['CFUNCTYPE', 'WINFUNCTYPE', 'PYFUNCTYPE'])
def test_arbitrary_address_and_com_prototypes_cannot_be_constructed(name):
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        getattr(ctypes, name)(ctypes.c_int)

def test_private_raw_function_pointer_constructor_is_also_closed():
    assert ctypes._CFuncPtr is _ctypes.CFuncPtr
    class Raw(ctypes._CFuncPtr):
        _flags_ = 0
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        Raw(0)

def test_existing_stdlib_function_type_cannot_create_a_new_native_callable():
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        type(ctypes.memmove)(0)

@pytest.mark.parametrize('name', sorted(guard.CTYPES_FACTORIES))
def test_original_factory_aliases_cannot_create_address_callables(name):
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        guard.CTYPES_FACTORIES[name](ctypes.c_int)(0)

def test_dangerous_native_names_and_unknowns_are_denied_without_a_native_library():
    for name in ['DeleteFileW', 'MoveFileW', 'SHFileOperationW', 'IFileOperation', 'CoCreateInstance', 'unexpected_native_symbol']:
        with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
            getattr(guard.NativeLibrary(), name)

@pytest.mark.xfail(strict=True, reason='QA-BUG: original native base remains reachable; this in-process test fence is not a security boundary')
def test_original_native_base_cannot_make_an_unchecked_callable():
    class Raw(type(ctypes.GetLastError).__bases__[0]):
        _flags_ = 0
        _argtypes_ = ()
        _restype_ = ctypes.c_ulong
    address = ctypes.cast(ctypes.GetLastError, ctypes.c_void_p).value
    assert address
    # Only a thread-local error read. Never use a file/process/COM address here.
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        Raw(address)()
