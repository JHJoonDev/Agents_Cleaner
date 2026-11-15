"""Read-only bootstrap controls. Never call the deliberately pre-bound native pointer."""
from pathlib import Path
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
fixture = ROOT / '.test-artifacts' / 'guard-startup'
fixture.mkdir(parents=True, exist_ok=True)
env = os.environ.copy()
for key in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','CODEX_HOME','CLAUDE_CONFIG_DIR','HERDR_APP_HOME','TEMP','TMP','TMPDIR'):
    env[key] = str(fixture)
env.pop('PYTEST_ADDOPTS', None)
env.pop('PYTEST_PLUGINS', None)
env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
env['PYTHONDONTWRITEBYTECODE'] = '1'
cached = """import ctypes
cached_delete = ctypes.WinDLL('kernel32', use_last_error=True).DeleteFileW
try:
    import qa_pytest_guard
except AssertionError as exc:
    assert 'predates early guard' in str(exc), str(exc)
    print('pre-bound DeleteFileW refused before tests; pointer never called')
else:
    raise AssertionError('early guard accepted a pre-bound native pointer')
"""
cold_runtime = """import sys
import qa_pytest_guard
import importlib.abc
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts'))
class NoModelOrSQLite(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'manager.model', 'sqlite3'}:
            raise ModuleNotFoundError('unexpected cold runtime dependency: ' + fullname)
sys.meta_path.insert(0, NoModelOrSQLite())
import manager.runtime
assert 'manager.model' not in sys.modules and 'sqlite3' not in sys.modules
print('cold runtime imports no model/sqlite3')
"""
cases = [
    ('prebound-pointer', ['-c', cached], 0, 'pointer never called'),
    ('missing-plugin', ['-m','pytest','-o','addopts=','-p','no:qa_pytest_guard','--collect-only','-q','tests/test_native_guard.py'], 4, 'QA guard must load before collection'),
    ('runtime-cold-import', ['-c', cold_runtime], 0, 'cold runtime imports no model/sqlite3'),
]
rows = []
for name, args, expected, marker in cases:
    result = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding='utf8', errors='replace', timeout=30)
    detail = result.stdout + result.stderr
    assert result.returncode == expected and marker in detail, detail
    rows.append({'control':name,'exit':result.returncode,'passed':True,'marker':marker})
path = ROOT / '.test-artifacts' / 'guard-startup-check.json'
path.write_text(json.dumps(rows,indent=2)+'\n',encoding='utf8')
print(json.dumps(rows))
