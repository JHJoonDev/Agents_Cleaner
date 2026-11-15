"""Test fence for trusted tests/AST mutants, not an in-process security boundary.

Raw native base classes remain reachable through Python introspection. Keep all
inputs synthetic; never use this shim to isolate arbitrary or hostile code.
"""
from __future__ import annotations
import contextlib
import os
from pathlib import Path
import platform
import shlex
import shutil
import sys
import tempfile
import ctypes
import gc

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SYSTEM_ROOT = os.environ.get('SystemRoot','C:/Windows')
SANDBOX = (ROOT / '.test-artifacts' / f'sandbox-{os.getpid()}').resolve()
SANDBOX.mkdir(parents=True, exist_ok=True)
HOME = SANDBOX / 'home'
HOME.mkdir(exist_ok=True)
for name, suffix in {
    'HOME':'home', 'USERPROFILE':'home', 'APPDATA':'home/roaming',
    'LOCALAPPDATA':'home/local', 'CODEX_HOME':'home/.codex',
    'CLAUDE_CONFIG_DIR':'home/.claude', 'HERDR_APP_HOME':'home/herdr-app',
    'TEMP':'temp', 'TMP':'temp', 'TMPDIR':'temp',
    'AGENTS_CLEANER_SIZE_ROOT':'projects', 'AGENTS_CLEANER_NOTIFIER_DIR':'notifier',
    'ORCA_DEV_REPO_ROOT':'orca', 'ProgramData':'program-data',
    'ProgramFiles':'program-files', 'ProgramFiles(x86)':'program-files-x86',
}.items():
    target = SANDBOX / suffix
    target.mkdir(parents=True, exist_ok=True)
    os.environ[name] = str(target)
os.environ['HOMEDRIVE'] = HOME.drive
os.environ['HOMEPATH'] = str(HOME)[len(HOME.drive):]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ.pop('ORCA_CLI_COMMAND', None)
os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
_git = shutil.which('git')
os.environ['PATH'] = str(Path(_git).parent) if _git else str(SANDBOX/'bin')
tempfile.tempdir = os.environ['TEMP']
sys.dont_write_bytecode = True
# Avoid platform's Windows fallback invoking cmd.exe for the `ver` command.
platform._uname_cache = platform.uname_result('Windows', 'qa-fixture', '11', '10.0.26100', 'AMD64')

class OutsideSandbox(AssertionError):
    pass

VIOLATIONS: list[str] = []
DENIALS: list[str] = []
EARLY_PLUGIN_ACTIVE = False
_expected = 0

@contextlib.contextmanager
def expected_denial():
    global _expected
    _expected += 1
    try:
        yield
    finally:
        _expected -= 1

def deny(message):
    DENIALS.append(message)
    if not _expected:
        VIOLATIONS.append(message)
    raise OutsideSandbox(message)

def check(path, *, parent_only=False):
    if isinstance(path, int):
        deny('QA cannot authorize a deletion by bare file descriptor')
    target = Path(os.fsdecode(path)).absolute()
    # unlink/rename remove the link object, not its referent. Ancestors still resolve.
    resolved = target.parent.resolve() / target.name if parent_only else target.resolve()
    if resolved == SANDBOX or SANDBOX not in resolved.parents:
        deny(f'QA outside sandbox: {resolved}')

def audit(event, args):
    if event in {'ctypes.dlopen', 'ctypes.dlsym', 'ctypes.dlsym/handle', 'ctypes.call_function'}:
        # Native arguments/vtables cannot be constrained by the Python path audit.
        # Unknown DLLs, symbols, ordinals and addresses fail closed too.
        detail = args[1] if event.startswith('ctypes.dlsym') else args[0] if args else 'unknown'
        deny(f'QA forbids native loading/symbol/call: {event}: {detail}')
    elif event in {'os.remove','os.rmdir'}:
        check(args[0], parent_only=True)
    elif event == 'os.rename':
        check(args[0], parent_only=True)
        check(args[1], parent_only=True)
    elif event == 'shutil.rmtree':
        check(args[0])
    elif event == 'subprocess.Popen':
        tokens = shlex.split(args[1], posix=False) if isinstance(args[1], str) else args[1]
        tokens = [str(t).strip('"') for t in tokens]
        if Path(str(args[0] or tokens[0])).name.lower() in {'git','git.exe'}:
            if '-C' in tokens:
                index = tokens.index('-C')
                check(tokens[index+1])
                command = tokens[index+2]
            else:
                command = tokens[1]
                if command != 'init':
                    deny('QA git command needs an explicit fixture repository')
                check(tokens[-1])
            if command in {'init','add','status','ls-files'} or (command == 'worktree' and 'list' in tokens):
                return  # Synthetic fixture git only; no commit, push, hooks, agent or shell.
        deny(f'QA forbids real process/command action: {event}')
    elif event in {'os.system','os.kill','os.startfile'}:
        deny(f'QA forbids real process/command action: {event}')
    elif event == 'socket.connect':
        deny('QA forbids network access')

sys.addaudithook(audit)

# Retain original entry points for negative controls that bypass attribute mocks.
CTYPES_LOADERS = {name: getattr(ctypes, name) for name in ('CDLL', 'PyDLL', 'WinDLL', 'OleDLL') if hasattr(ctypes, name)}
CTYPES_FACTORIES = {name: getattr(ctypes, name) for name in ('CFUNCTYPE', 'PYFUNCTYPE', 'WINFUNCTYPE') if hasattr(ctypes, name)}
PYTHON_API = ctypes.pythonapi
_original_function_type = ctypes._CFuncPtr
_memory_functions = [getattr(ctypes, name) for name in ('memmove', 'memset', '_cast', '_string_at', '_wstring_at', 'GetLastError') if hasattr(ctypes, name)]
_safe_function_ids = {id(value) for value in _memory_functions}
_safe_prototypes = {type(value) for value in _memory_functions} | {PYTHON_API._FuncPtr}
# Refuse a startup that already cached native functions/prototypes before the plugin.
# CFuncPtr calls are not all covered by ctypes.call_function on every CPython path.
for _value in gc.get_objects():
    if isinstance(_value, _original_function_type) and id(_value) not in _safe_function_ids:
        deny('QA native function pointer predates early guard: ' + str(getattr(_value, '__name__', type(_value).__name__)))
if any(prototype not in _safe_prototypes for prototype in _original_function_type.__subclasses__()):
    deny('QA native function prototype predates early guard')

NATIVE_ACTIONS = frozenset({
    'deletefilea', 'deletefilew', 'removedirectorya', 'removedirectoryw',
    'movefilea', 'movefilew', 'movefileexa', 'movefileexw', 'replacefilea', 'replacefilew',
    'shfileoperationa', 'shfileoperationw', 'shcreateitemfromparsingname',
    'cocreateinstance', 'ifileoperation', 'ntdeletefile', 'zwdeletefile',
})
def native_action(name):
    kind = 'delete/move/recycle' if str(name).casefold() in NATIVE_ACTIONS else 'unknown native action'
    deny(f'QA forbids {kind}: {name}')

class NativeLibrary:
    def __getattr__(self, name):
        # App identity is an explicit pure-Python no-op; no DLL is loaded.
        if name == 'SetCurrentProcessExplicitAppUserModelID':
            return lambda *args: None
        native_action(name)

class NativeLoader:
    def __getattr__(self, name):
        if name in {'kernel32', 'Kernel32', 'shell32', 'ole32', 'user32'}:
            return NativeLibrary()
        native_action(name)
    def LoadLibrary(self, name):
        native_action(name)

def blocked_native(*args, **kwargs):
    native_action(args[0] if args else 'native function/prototype')

class BlockedFunctionPointer(_original_function_type):
    _flags_ = 0
    def __new__(cls, *args, **kwargs):
        native_action('raw function pointer construction')
    def __call__(self, *args, **kwargs):
        native_action('raw function pointer call')

# Existing stdlib memory/last-error functions remain usable, but their types may
# not be reused to manufacture a callable at a new arbitrary address.
def blocked_pointer_new(cls, *args, **kwargs):
    native_action('native function pointer construction')
for _prototype in _safe_prototypes:
    _prototype.__new__ = staticmethod(blocked_pointer_new)

for _name in CTYPES_LOADERS.keys() | CTYPES_FACTORIES.keys():
    setattr(ctypes, _name, blocked_native)
for _name in ('cdll', 'pydll', 'windll', 'oledll'):
    if hasattr(ctypes, _name):
        setattr(ctypes, _name, NativeLoader())
ctypes.pythonapi = NativeLibrary()
ctypes._CFuncPtr = BlockedFunctionPointer
import _ctypes
_ctypes.CFuncPtr = BlockedFunctionPointer
