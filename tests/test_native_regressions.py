"""Mocked native-command boundaries and post-delete metadata safety."""
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest
from manager import operations,model
from test_guard_regressions import codex,claude

def finish_native(entry):
    with sqlite3.connect(entry.extra['db'],factory=model.ClosingConnection)as db:
        db.execute('delete from threads where id=?',(entry.session,))
    entry.files[0].unlink()

def test_injected_native_runner_has_the_documented_environment_and_timeout_interface():
    with tempfile.TemporaryDirectory()as temporary:
        entry=codex(Path(temporary));called=[]
        def native(exe,args,*,env,timeout):
            assert exe=='codex'and args==['--no-daemon','delete',entry.session,'--force']
            assert env['CODEX_HOME']==str(entry.home)and timeout==operations.CODEX_DELETE_TIMEOUT
            called.append(entry.session);finish_native(entry)
        _,good,failed,_=operations.delete([entry],{entry.key:model.signature(entry)},detector=lambda:set(),runner=native)
        assert good==[entry]and failed==[]and called==[entry.session]

def test_injected_runner_is_not_called_after_app_activity_changes_at_confirmation():
    with tempfile.TemporaryDirectory()as temporary:
        entry=codex(Path(temporary));active=[False];checks=[0];called=[];original=operations.validate
        def confirmed(row):
            original(row);checks[0]+=1
            if checks[0]>=2:active[0]=True
        def native(exe,args,**kwargs):called.append(entry.session);finish_native(entry)
        with patch.object(operations,'validate',confirmed):
            _,good,failed,_=operations.delete([entry],{entry.key:model.signature(entry)},detector=lambda:{'Codex'}if active[0]else set(),runner=native)
        assert good==[]and failed and called==[]and entry.files[0].exists()

def test_real_command_adapter_receives_a_callback_and_rechecks_a_discovery_time_change():
    with tempfile.TemporaryDirectory()as temporary:
        entry=codex(Path(temporary));reached=[];boundary=[]
        def command(exe,args,*,env,timeout,before_run):
            reached.append('discovery')
            entry.files[0].write_bytes(b'changed while discovering native executable')
            before_run()
            boundary.append('mutation');finish_native(entry)
        with patch.object(operations,'command',command):
            _,good,failed,_=operations.delete([entry],{entry.key:model.signature(entry)},detector=lambda:set(),runner=command)
        assert good==[]and failed and reached==['discovery']and boundary==[]
        assert entry.files[0].exists()

def test_a_success_exit_without_database_deletion_is_not_reported_as_success():
    with tempfile.TemporaryDirectory()as temporary:
        entry=codex(Path(temporary));calls=[]
        def no_op(exe,args,**kwargs):calls.append('exit zero');return 0
        _,good,failed,_=operations.delete([entry],{entry.key:model.signature(entry)},detector=lambda:set(),runner=no_op)
        assert calls==['exit zero']and good==[]and failed and entry.files[0].exists()

@pytest.mark.parametrize('stage',['after-record','during-index-preparation'])
def test_app_restart_after_record_removal_preserves_that_profiles_history(stage):
    with tempfile.TemporaryDirectory()as temporary:
        entry=claude(Path(temporary));history=entry.home/'history.jsonl'
        raw='{"sessionId":"'+entry.session+'","display":"synthetic history"}\n';history.write_text(raw,encoding='utf8');active=[False]
        original=operations.trim_claude_history
        def recycle(path):
            path.unlink()
            if stage=='after-record':active[0]=True
        def trim(*args,**kwargs):
            if stage=='during-index-preparation':active[0]=True
            return original(*args,**kwargs)
        with patch.object(operations,'trim_claude_history',side_effect=trim)as prepare:
            _,good,failed,notes=operations.delete([entry],{entry.key:model.signature(entry)},detector=lambda:{'Claude'}if active[0]else set(),recycler=recycle)
        if stage=='after-record':prepare.assert_not_called()
        assert good==[entry]and failed and notes
        assert history.read_text(encoding='utf8')==raw
