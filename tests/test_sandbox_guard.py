import os
from pathlib import Path
import shutil
import subprocess
import pytest
import sandbox_guard as guard

@pytest.mark.parametrize('operation',[
    lambda p: os.unlink(p), lambda p: os.rmdir(p), lambda p: shutil.rmtree(p),
    lambda p: os.rename(p, guard.SANDBOX/'renamed'),
    lambda p: os.rename(guard.SANDBOX/'fixture', p),
])
def test_outside_deletions_are_stopped_before_any_filesystem_action(operation):
    outside = guard.ROOT/'never-created-guard-sentinel'
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        operation(outside)

def test_guard_uses_resolved_ancestors_and_blocks_parent_traversal():
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        os.unlink(guard.SANDBOX/'..'/'escape')

def test_process_guard_blocks_before_spawn():
    with guard.expected_denial(), pytest.raises(guard.OutsideSandbox):
        subprocess.run(['hc','server','stop'])

def test_fixture_delete_and_rename_remain_possible():
    source = Path(os.environ['TEMP'])/'allowed-source'
    target = source.with_name('allowed-target')
    source.write_text('synthetic',encoding='utf8')
    source.rename(target)
    target.unlink()
    assert not target.exists()

def test_default_app_storage_roots_are_redirected_before_any_cleanup_or_scan():
    from manager import providers,herdr,operations,project_sizes,workspaces,qt_style
    import tempfile
    roots=[path for _,path in providers.profile_roots()]
    roots.extend(herdr.protected_homes())
    roots.extend([operations.DEFAULT_BACKUP_DIR,project_sizes.DEFAULT_ROOT,Path.home(),Path(tempfile.gettempdir()),Path(workspaces.os.environ['SystemRoot']),Path(qt_style.os.environ['SystemRoot'])])
    for path in roots:guard.check(path)
