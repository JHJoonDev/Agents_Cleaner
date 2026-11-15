import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from manager import app_control as control, runtime, operations
from manager.model import Entry, signature, safe_path, _link_stat
from manager.project_sizes import cache_name, protected, scan_project
from manager.qt_logic import Logic

def fake_process(pid,created=10):
    return SimpleNamespace(pid=pid,create_time=lambda:created,is_running=lambda:True,
        children=lambda **kwargs:[],kill=Mock(),name=lambda:'synthetic')

def fake_psutil(processes):
    return SimpleNamespace(Process=lambda pid:processes[pid],Error=OSError,NoSuchProcess=ProcessLookupError,wait_procs=Mock())

def test_force_stop_protects_self_ancestors_and_reused_pids_and_handles_children():
    parent=fake_process(1);child=fake_process(2);grandchild=fake_process(4);protected_process=fake_process(999);reused=fake_process(3,20)
    parent.children=lambda recursive=False:[child,protected_process]+([grandchild]if recursive else[])
    ps=fake_psutil({1:parent,2:child,3:reused,999:protected_process})
    targets=[control.Target(1,10,'parent'),control.Target(3,10,'old'),control.Target(999,10,'self')]
    with patch.object(runtime,'psutil',ps),patch.object(control,'protected_pids',return_value={999}),patch.object(control,'remaining',return_value=[]):
        assert control.force_stop(targets)==([],[])
    parent.kill.assert_called_once_with();child.kill.assert_called_once_with()
    grandchild.kill.assert_called_once_with()
    protected_process.kill.assert_not_called();reused.kill.assert_not_called()
    ps.wait_procs.assert_called_once()
    assert {p.pid for p in ps.wait_procs.call_args.args[0]}=={1,2,4}
    assert 0<=ps.wait_procs.call_args.kwargs['timeout']<=3

def test_alive_requires_matching_creation_time_and_running_state():
    p=fake_process(1);ps=fake_psutil({1:p})
    with patch.object(runtime,'psutil',ps):
        assert control._alive(control.Target(1,10,'same')) is p
        assert control._alive(control.Target(1,11,'reused')) is None
        p.is_running=lambda:False
        assert control._alive(control.Target(1,10,'stopped')) is None
        ps.Process=Mock(side_effect=OSError('gone'))
        assert control._alive(control.Target(1,10,'gone')) is None

def test_protected_pids_and_remaining_never_return_ancestors_as_targets():
    me=SimpleNamespace(pid=12,parents=lambda:[SimpleNamespace(pid=10),SimpleNamespace(pid=11)])
    with patch.object(runtime,'psutil',SimpleNamespace(Process=lambda:me)):
        assert control.protected_pids()=={10,11,12}
    with patch.object(runtime,'psutil',None):assert control.protected_pids()=={os.getpid()}
    targets=[control.Target(10,1,'protected'),control.Target(20,1,'gone'),control.Target(30,1,'live')]
    with patch.object(control,'protected_pids',return_value={10}),patch.object(control,'_alive',side_effect=lambda t:t.pid==30):
        assert control.remaining(targets)==[targets[2]]

@pytest.mark.parametrize('exception',[ProcessLookupError('gone'),OSError('denied')])
def test_force_stop_handles_inaccessible_children_and_reports_only_real_failures(exception):
    process=fake_process(1);process.children=Mock(side_effect=OSError('unreadable children'));process.kill=Mock(side_effect=exception)
    ps=fake_psutil({1:process})
    with patch.object(runtime,'psutil',ps),patch.object(control,'protected_pids',return_value=set()),patch.object(control,'remaining',return_value=[]):
        left,notes=control.force_stop([control.Target(1,10,'fixture')])
    assert left==[]
    assert bool(notes)==(type(exception) is OSError)
    process.kill.assert_called_once();ps.wait_procs.assert_called_once()

@pytest.mark.parametrize('bits',[0,1,0x10,0x20,0x400,0x401,0x410,0x800])
def test_only_the_reparse_flag_marks_an_ordinary_windows_directory_as_a_link(bits):
    import stat
    assert _link_stat(SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=bits))==bool(bits&0x400)
    assert _link_stat(SimpleNamespace(st_mode=stat.S_IFLNK,st_file_attributes=bits))

@pytest.mark.parametrize('readonly',[False,True])
def test_cli_passes_readonly_policy_to_the_gui_without_opening_it(readonly):
    import main
    with patch('sys.argv',['fixture']+(['--read-only']if readonly else[])),patch.object(main,'run_gui')as gui:
        main.main()
    gui.assert_called_once_with(allow_delete=not readonly)

@pytest.mark.parametrize('failure',[False,True])
def test_herdr_shutdown_is_explicit_mocked_and_failures_are_reported(failure):
    target=control.Target(1,10,'synthetic');group=control.AppGroup('Herdr',targets=[target])
    command=Mock(side_effect=OSError('fixture denied') if failure else None)
    with patch.object(control,'visible_windows',return_value=[]),patch.object(control,'_alive',return_value=object()),patch.object(runtime,'command',command),patch.object(control,'remaining',return_value=[]),patch.object(control.time,'sleep',side_effect=AssertionError('an empty remaining set must return immediately')):
        left,notes=control.request_close([group],grace=0)
    assert left==[]
    assert bool(notes)==failure
    if failure:assert 'fixture denied' in notes[0]
    command.assert_called_once_with('hc',['server','stop'],timeout=15)

def test_replacement_herdr_is_not_stopped_and_self_hosting_groups_fail_before_any_calls():
    command=Mock();target=control.Target(1,10,'old')
    with patch.object(control,'visible_windows',return_value=[]),patch.object(control,'_alive',return_value=None),patch.object(runtime,'command',command),patch.object(control,'remaining',return_value=[]),patch.object(control.time,'sleep',side_effect=AssertionError('an empty remaining set must return immediately')):
        assert control.request_close([control.AppGroup('Herdr',targets=[target])])==([],[])
        with pytest.raises(RuntimeError):control.request_close([control.AppGroup('Herdr',holds_self=True)])
    command.assert_not_called()

def test_window_grace_wait_honors_deadline_without_real_sleep():
    target=control.Target(1,10,'window');clock=[0.0];calls=[0]
    def sleep(delta):
        calls[0]+=1
        assert calls[0]<20,'deadline did not stop polling'
        clock[0]+=delta
    with patch.object(control,'visible_windows',return_value=[(123,1),(456,2)]),patch.object(control,'_alive',return_value=object()),patch.object(control,'close_window',return_value=True)as close,patch.object(control,'remaining',return_value=[target]),patch.object(control.time,'monotonic',side_effect=lambda:clock[0]),patch.object(control.time,'sleep',side_effect=sleep):
        assert control.request_close([control.AppGroup('Codex',targets=[target])],grace=.4)==([target],[])
    close.assert_called_once_with(123);assert .4<=clock[0]<=.65

def test_no_window_shutdown_does_not_wait_and_empty_force_stop_has_no_process_wait():
    target=control.Target(1,10,'headless');ps=fake_psutil({})
    with patch.object(control,'visible_windows',return_value=[]),patch.object(control,'remaining',return_value=[target]),patch.object(control.time,'sleep',side_effect=AssertionError('must not wait without an explicit close request')):
        assert control.request_close([control.AppGroup('Codex',targets=[target])],grace=10)==([target],[])
    with patch.object(runtime,'psutil',ps),patch.object(control,'protected_pids',return_value=set()),patch.object(control,'remaining',return_value=[]):
        assert control.force_stop([])==([],[])
    ps.wait_procs.assert_not_called()

def test_successful_herdr_close_observes_grace_for_a_server_that_is_still_running():
    target=control.Target(1,10,'server');clock=[0.0];calls=[0]
    def sleep(delta):
        calls[0]+=1;assert calls[0]<20
        clock[0]+=delta
    with patch.object(control,'visible_windows',return_value=[]),patch.object(control,'_alive',return_value=object()),patch.object(runtime,'command')as command,patch.object(control,'remaining',return_value=[target]),patch.object(control.time,'monotonic',side_effect=lambda:clock[0]),patch.object(control.time,'sleep',side_effect=sleep):
        assert control.request_close([control.AppGroup('Herdr',targets=[target])],grace=.4)==([target],[])
    command.assert_called_once();assert .4<=clock[0]<=.65

@pytest.mark.parametrize('name,expected', [('node_modules',True),('NODE_MODULES',True),('.venv311',True),('venv-other',True),('node_modules-old',False),('data',False),('out',False)])
def test_cache_names_are_exact_or_the_documented_venv_prefix(name,expected):
    assert cache_name(name) is expected

@pytest.mark.parametrize('name', ['.env','.env.local','data.db','data.sqlite','data.sqlite3','private.pem','private.key','data.db-wal','data.db-shm','datasets','dataset-private','ToxAuto-private'])
def test_protected_data_classes_never_become_reclaimable(name):
    assert protected(Path(name))

def test_size_threshold_is_strict_and_cache_candidates_do_not_double_count():
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);(root/'output').mkdir();(root/'output/x.bin').write_bytes(b'1234567890')
        (root/'equal.bin').write_bytes(b'1234567890');(root/'above.bin').write_bytes(b'12345678901')
        (root/'node_modules').mkdir();(root/'node_modules/x.bin').write_bytes(b'12')
        with patch('manager.artifacts._git_problem',return_value=''):
            result=scan_project(root,large=10)
        assert result.size==33
        assert {c.path.name for c in result.candidates}=={'above.bin','node_modules'}
        assert result.reclaimable==13

@pytest.mark.parametrize('busy,allow,errors,groups,eligible,expected',[
    (True,True,False,False,True,False),(False,False,False,False,True,False),
    (False,True,True,True,True,False),(False,True,False,False,False,False),
    (False,True,False,False,True,True),
])
def test_submit_review_rechecks_readonly_busy_errors_and_selection(busy,allow,errors,groups,eligible,expected):
    with tempfile.TemporaryDirectory() as temporary:
        home=Path(temporary);path=home/'item';path.write_bytes(b'fixture')
        entry=Entry('Claude',home,'id','title','project',0,[path]);calls=[]
        host=SimpleNamespace(busy=busy,allow_delete=allow,errors=['unreadable']if errors else[],
            can_select=lambda e:eligible,status_text=Mock(),launch=lambda work,event:calls.append((work(),event)))
        with patch('manager.qt_logic.message'),patch.object(operations,'delete',return_value='done')as deletion:
            Logic.submit_review(host,[entry],{entry.group}if groups else set(),[entry],{'workspaces':[]})
        assert bool(calls)==expected
        assert deletion.called==expected
        if expected:
            assert calls==[('done','delete')]
            assert deletion.call_args.args==([entry],{entry.key:signature(entry)})
        assert path.read_bytes()==b'fixture'
