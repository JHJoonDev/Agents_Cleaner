# Controlled Cleaner tests

Use Python 3.11 with `requirements-dev.txt` installed in an isolated test venv.
Run from the repository root with `python qa/run_tests.py -q -rsx`.
This entry point loads the fence before pytest itself. The pytest configuration
also enables `-p qa_pytest_guard` for direct pytest invocations.
The plugin must load before collection; `qa/check_guard_startup.py` checks this.

Checks: `python qa/check_guard_startup.py`, `python -m mypy --strict`, then
`python qa/run_tests.py -q -rsx`. Disable unrelated plugin autoload with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. Generated fixtures/reports stay ignored under
`.test-artifacts/`; no real-profile cleanup, recycle or GUI test is authorized.

The in-process guard is a **test fence, not a security boundary**. It blocks
ordinary Python deletion and common ctypes entry points, but Python introspection
can recover the original native base class. A strict xfail records that limitation
using only GetLastError. Never run hostile code or real-profile cleanup behind it.
Tests and AST mutants must stay trusted and use synthetic worktree fixtures;
native recycle, process shutdown and COM actions must be Python fakes.
