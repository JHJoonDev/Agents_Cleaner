import importlib
import json
from pathlib import Path
import sys
from runtime_ast import executable_hash

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))

def test_all_application_executable_asts_match_reviewed_runtime_snapshot():
    expected = json.loads((Path(__file__).parent/'fixtures/runtime_ast.json').read_text(encoding='utf8'))
    actual = {str(p.relative_to(ROOT)).replace('\\','/'): executable_hash(p.read_text(encoding='utf8')) for p in (ROOT/'scripts').rglob('*.py')}
    assert actual == expected

def test_all_application_modules_import_without_scanning_or_mutating_storage():
    for path in (ROOT/'scripts/manager').glob('*.py'):
        importlib.import_module('manager' if path.stem=='__init__' else 'manager.'+path.stem)
    importlib.import_module('main')
    from manager.model import Entry
    entry = Entry('Claude', Path('synthetic'), 'id', 'title', 'project', 0)
    assert entry.extra == {} and entry.files == [] and entry.kind == 'conversation'
