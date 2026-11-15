"""Load with pytest -p before plugin autoload, conftests and test collection."""
from pathlib import Path
import sys
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent / 'tests'))
import sandbox_guard as guard

guard.EARLY_PLUGIN_ACTIVE = True

# Pytest imports colorama before conftests on Windows. Use its documented
# no-WinAPI path instead of opening a real console DLL under the guard.
console = ModuleType('colorama.win32')
console.STDOUT = -11
console.STDERR = -12
console.ENABLE_VIRTUAL_TERMINAL_PROCESSING = 4
console.windll = None
console.SetConsoleTextAttribute = lambda *args: None
console.winapi_test = lambda *args: False
sys.modules.setdefault('colorama.win32', console)
