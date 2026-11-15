import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid
import pytest
from manager import artifacts, operations, workspaces
from manager.model import Entry, signature

def disposable(base,category='deps-node',days=31):
    project=base/'project';target=project/'node_modules';target.mkdir(parents=True);child=target/'payload';child.write_bytes(b'fixture')
    now=100_000_000;old=now-days*86400
    for p in [child,target,project]:os.utime(p,(old,old))
    entry=Entry('Artifacts',project,'fixture','title',str(project),old,[target],kind='artifact',extra={
        'category':category,'tier':2,'method':'recycle','project_updated':old,'scan_state':artifacts._snapshot(target,recursive=False)[1]})
    return entry,now,child

@pytest.mark.parametrize('days,allowed',[(30,False),(30+1/86400,True),(31,True)])
def test_project_idle_threshold_is_strict_and_scan_errors_fail_closed(days,allowed):
    with tempfile.TemporaryDirectory()as temporary:
        entry,now,child=disposable(Path(temporary),days=days)
        with patch.object(artifacts,'_git_problem',return_value=''),patch.object(artifacts,'process_owners',return_value=[]):
            if allowed:artifacts.revalidate(entry,live=[],now=now,apps=set())
            else:
                with pytest.raises(ValueError):artifacts.revalidate(entry,live=[],now=now,apps=set())
            with patch.object(artifacts,'_project_targets',return_value=(0,[],['denied'])),pytest.raises(ValueError):
                artifacts.revalidate(entry,live=[],now=now,apps=set())

def test_shape_requires_exact_provider_kind_single_target_and_whitelisted_temp_structure():
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);root=base/'temp';root.mkdir();good=root/'codex_fixture';good.mkdir();invalid=root/'other';invalid.mkdir()
        entry=Entry('Artifacts',root,'fixture','title','none',0,[good],kind='artifact',extra={'category':'temp-agent','tier':1,'method':'delete'})
        with patch.dict(os.environ,{'TEMP':str(root)}):
            artifacts.validate_shape(entry)
            entry.blocked='explicit block'
            with patch.object(artifacts,'process_owners',return_value=[]),pytest.raises(ValueError):artifacts.revalidate(entry,live=[],now=10_000_000,apps=set())
            entry.blocked=''
            for field,value in [('provider','Claude'),('kind','cache'),('files',[good,invalid]),('files',[invalid])]:
                previous=getattr(entry,field);setattr(entry,field,value)
                with pytest.raises(ValueError):artifacts.validate_shape(entry)
                setattr(entry,field,previous)
            wrong=base/'wrong';wrong.mkdir();candidate=wrong/'codex_fixture';candidate.mkdir()
            entry.home=wrong;entry.files=[candidate]
            with pytest.raises(ValueError):artifacts.validate_shape(entry)
            leaf=root/'claude'/'project'/str(uuid.uuid4());leaf.mkdir(parents=True)
            entry.home=root;entry.files=[leaf];entry.extra['category']='temp-claude'
            artifacts.validate_shape(entry)
            malformed=root/'not-claude'/'project'/str(uuid.uuid4());malformed.mkdir(parents=True)
            entry.files=[malformed]
            with pytest.raises(ValueError):artifacts.validate_shape(entry)
            entry.extra['category']='temp-agent';nested=root/'outer'/'codex_nested';nested.mkdir(parents=True);entry.files=[nested]
            with pytest.raises(ValueError):artifacts.validate_shape(entry)
            user=base/'user';backups=user/'.config/herdr-app/data';wrong=backups/'other'/'fixture';wrong.mkdir(parents=True)
            entry.home=backups;entry.files=[wrong];entry.extra={'category':'herdr-profile-backup','tier':3,'method':'recycle'}
            with patch.dict(os.environ,{'USERPROFILE':str(user)}),pytest.raises(ValueError):artifacts.validate_shape(entry)

def test_project_artifact_category_cannot_claim_an_arbitrary_private_directory():
    with tempfile.TemporaryDirectory()as temporary:
        entry,_,_=disposable(Path(temporary));private=entry.home/'private-data';private.mkdir();entry.files=[private]
        with pytest.raises(ValueError):artifacts.validate_shape(entry)

def test_artifact_home_ancestor_reparse_is_rejected_independently_of_target_path():
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);entry,now,child=disposable(base);original=Path.lstat
        def info(path):
            if path==base:return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
            return original(path)
        with patch.object(Path,'lstat',info),pytest.raises(ValueError):artifacts.validate_shape(entry)

def test_unknown_relative_and_unrelated_live_paths_have_distinct_safety_decisions():
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);entry,now,child=disposable(base);other=base/'unrelated';other.mkdir()
        for invalid in [None,{},set(),[Path('relative')]]:assert artifacts.live_problem(entry,invalid)
        assert artifacts.live_problem(entry,[other])==''
        assert artifacts.live_problem(entry,[entry.files[0]])
        assert artifacts.process_problem(entry,set(),owners=None)
        assert artifacts.process_problem(entry,set(),owners=[other])==''
        assert artifacts.process_problem(entry,set(),owners=[entry.home])
        entry.extra['category']='temp-agent'
        assert artifacts.process_problem(entry,{'Claude'},owners=[])==''
        entry.extra['category']='temp-claude'
        assert artifacts.process_problem(entry,set(),owners=[])==''

@pytest.mark.parametrize('kind,provider,protect',[('conversation','Claude',True),('cache','Herdr',True),('cache','Claude',False)])
def test_newly_discovered_project_roots_are_protected_but_cache_labels_are_not_targets(kind,provider,protect):
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);entry,now,child=disposable(base);known=Entry(provider,base/'other-profile','known','title',str(entry.files[0]),0,kind=kind)
        recycler=Mock();original=artifacts.revalidate
        def revalidate(row,**kwargs):return original(row,live=[],now=now,apps=kwargs['apps'])
        with patch.object(artifacts,'_git_problem',return_value=''),patch.object(artifacts,'process_owners',return_value=[]),patch.object(artifacts,'revalidate',revalidate):
            _,good,failed,_=operations.delete([entry],{entry.key:signature(entry)},all_entries=[entry,known],detector=lambda:set(),recycler=recycler)
        assert recycler.called is not protect
        assert bool(good)is not protect
        assert bool(failed)is protect
        assert child.exists()

def test_artifact_recycle_checks_identity_even_when_content_signature_has_not_changed():
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);entry,now,child=disposable(base);recycler=Mock();original=artifacts.revalidate
        def revalidate(row,**kwargs):
            original(row,live=[],now=now,apps=kwargs['apps']);child.chmod(stat.S_IREAD)
        try:
            with patch.object(artifacts,'_git_problem',return_value=''),patch.object(artifacts,'process_owners',return_value=[]),patch.object(artifacts,'revalidate',revalidate):
                _,good,failed,_=operations.delete([entry],{entry.key:signature(entry)},detector=lambda:set(),recycler=recycler)
            assert good==[]and failed and not recycler.called and child.exists()
        finally:child.chmod(stat.S_IWRITE|stat.S_IREAD)
