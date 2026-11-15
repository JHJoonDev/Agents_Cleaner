"""Sandbox is installed before importing any app/test module."""
import sys
if 'qa_pytest_guard' not in sys.modules:
    raise RuntimeError('QA guard must load before collection: python -m pytest -p qa_pytest_guard')
import ctypes
import os
from pathlib import Path
import subprocess
from collections.abc import MutableMapping
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sandbox_guard as guard
if not guard.EARLY_PLUGIN_ACTIVE:
    raise RuntimeError('QA early guard plugin was not activated')
sys.path.insert(0, str(guard.ROOT/'scripts'))
from hypothesis.configuration import set_hypothesis_home_dir
set_hypothesis_home_dir(guard.SANDBOX/'hypothesis')

def blocked(*args, **kwargs):
    guard.deny('QA forbids native process/recycle actions; inject a mock')

class SyntheticProcess:
    def __init__(self, pid=None): self.pid = pid or os.getpid()
    def create_time(self): return 2000
    def parents(self): return []
    def children(self, **kwargs): return []
    def is_running(self): return False
    def name(self): return 'synthetic'
    kill = terminate = send_signal = blocked

class AppEnvironment(MutableMapping):
    """Virtualize app protection/font roots without breaking the OS DLL loader."""
    def __getitem__(self,key):
        value=os.environ[key]
        if key.casefold()=='systemroot' and value.casefold()==guard.NATIVE_SYSTEM_ROOT.casefold():
            return str(guard.SANDBOX/'system-root')
        return value
    def __setitem__(self,key,value):os.environ[key]=value
    def __delitem__(self,key):del os.environ[key]
    def __iter__(self):return iter(os.environ)
    def __len__(self):return len(os.environ)
    def copy(self):return dict(self)

class AppOS:
    environ=AppEnvironment()
    def __getattr__(self,name):return getattr(os,name)

def pytest_configure(config):
    config.option.basetemp = str(guard.SANDBOX / 'pytest')

@pytest.fixture(autouse=True)
def deletion_boundary(monkeypatch):
    start = len(guard.VIOLATIONS)
    for name in guard.CTYPES_LOADERS.keys() | guard.CTYPES_FACTORIES.keys():
        monkeypatch.setattr(ctypes, name, guard.blocked_native)
    for name in ('cdll', 'pydll', 'windll', 'oledll'):
        monkeypatch.setattr(ctypes, name, guard.NativeLoader())
    monkeypatch.setattr(os, 'kill', blocked)
    import psutil
    monkeypatch.setattr(psutil, 'Process', SyntheticProcess)
    monkeypatch.setattr(psutil, 'pids', lambda: [])
    monkeypatch.setattr(psutil, 'process_iter', lambda *args, **kwargs: iter(()))
    sys.path.insert(0, str(guard.ROOT/'scripts'))
    from manager import runtime
    from manager import workspaces, qt_style
    monkeypatch.setattr(workspaces,'os',AppOS())
    monkeypatch.setattr(qt_style,'os',AppOS())
    original_run = runtime._run_process
    def fixture_process(args, **kwargs):
        if Path(args[0]).name.lower() in {'git','git.exe'}:
            return subprocess.run(args, **kwargs)
        return original_run(args, **kwargs)
    monkeypatch.setattr(runtime, '_run_process', fixture_process)
    yield
    assert guard.VIOLATIONS[start:] == [], 'outer QA guard caught an unsafe action (even if app swallowed it)'

def pytest_collection_modifyitems(items):
    for item in items:
        # These existing integration tests spawn actual child processes or visible Tk/Qt windows.
        if (item.cls and item.cls.__name__ in {'StopProcessTests','MonitorAndDialogTests','QtSmokeTests',
                'ArtifactUiTests','ProjectSizesUiTests','StartupPerfTests','TkWorkerTests','SizeTabTests','OwnedProcessDeadlineTests'}):
            item.add_marker(pytest.mark.skip(reason='QA: real process/GUI integration is outside offline sandbox scope'))
        if item.path.name in {'test_runtime_safety.py','test_artifacts.py'} and ('junction' in item.name or item.name == 'test_symlink_and_internal_link_are_refused'):
            item.add_marker(pytest.mark.skip(reason='QA: original junction fixture starts cmd.exe; reparse metadata covered offline'))
