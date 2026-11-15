# Controlled Cleaner tests

Run from this worktree with `python -m pytest -p qa_pytest_guard`.
The plugin must load before collection; `qa/check_guard_startup.py` checks this.

The in-process guard is a **test fence, not a security boundary**. It blocks
ordinary Python deletion and common ctypes entry points, but Python introspection
can recover the original native base class. A strict xfail records that limitation
using only GetLastError. Never run hostile code or real-profile cleanup behind it.
Tests and AST mutants must stay trusted and use synthetic worktree fixtures;
native recycle, process shutdown and COM actions must be Python fakes.
