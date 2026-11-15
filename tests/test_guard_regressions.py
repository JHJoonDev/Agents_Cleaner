"""Deterministic adversarial cases prepared after the first mutation measurement."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import uuid
import pytest
from manager import model, operations, artifacts
from manager.model import Entry

def claude(home):
    sid=str(uuid.uuid4());path=home/'projects'/'fixture'/f'{sid}.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{}',encoding='utf8')
    return Entry('Claude',home,sid,'fixture','label',0,[path])

def codex(home,dbpath=None):
    sid=str(uuid.uuid4());path=home/'sessions'/'saved.jsonl';path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
    dbpath=dbpath or home/'state_5.sqlite';dbpath.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(dbpath,factory=model.ClosingConnection)as db:
        db.execute('create table threads(id text,updated_at integer,rollout_path text)')
        db.execute('insert into threads values(?,?,?)',(sid,100,str(path)))
    return Entry('Codex',home,sid,'fixture','label',100,[path],extra={'db':str(dbpath),'rollout_candidates':[str(path),str(path)+'.zst']})

def test_path_root_reparse_ancestors_and_unreadable_metadata_are_rejected():
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);entry=claude(home);leaf=entry.files[0]
        with pytest.raises(ValueError):model.safe_path(home,home)
        original=Path.lstat
        for target in [home,leaf.parent,leaf]:
            def info(path):
                if path==target:return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
                return original(path)
            with patch.object(Path,'lstat',info),pytest.raises(ValueError):model.safe_path(leaf,home)
        with patch.object(Path,'lstat',side_effect=PermissionError('metadata denied')):
            assert model.is_link(leaf) is True
        assert model._link_stat(SimpleNamespace(st_mode=stat.S_IFDIR)) is False

def test_signature_refuses_new_rollout_candidates_directories_and_strict_read_failures():
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);entry=codex(home);model.signature(entry)
        added=Path(entry.extra['rollout_candidates'][1]);added.write_bytes(b'new compressed rollout')
        with pytest.raises(ValueError):model.signature(entry)
        directory=home/'directory';directory.mkdir()
        with pytest.raises(ValueError):model.signature(Entry('Codex',home,'id','title','project',0,[directory],extra={'rollout_candidates':[str(directory)]}))
        cache=home/'cache';cache.mkdir();child=cache/'unreadable';child.write_bytes(b'fixture')
        original=Path.lstat
        def info(path):
            if path==child:raise PermissionError('fixture child denied')
            return original(path)
        with patch.object(Path,'lstat',info),pytest.raises(PermissionError):
            model.signature(Entry('Claude',home,'cache:cache','title','project',0,[cache],kind='cache'))

@pytest.mark.parametrize('change',[{'provider':'Unknown'},{'provider':'Herdr'},{'provider':'Orca'},{'kind':'workspace'},{'session':'not-a-uuid'},{'blocked':'fixture reason'}])
def test_conversation_validation_rejects_unsupported_and_blocked_rows(change):
    with tempfile.TemporaryDirectory()as temporary:
        entry=claude(Path(temporary));operations.validate(entry)
        for key,value in change.items():setattr(entry,key,value)
        with pytest.raises(ValueError):operations.validate(entry)

def test_cache_and_project_allowlists_cannot_be_forged_with_well_formed_paths():
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);cache=home/'cache';cache.mkdir();other=home/'other';other.mkdir()
        good=Entry('Claude',home,'cache:cache','title','project',0,[cache],kind='cache');operations.validate(good)
        for session,files in [('cache:other',[other]),('cache:cache',[other]),('cache:cache',[home])]:
            with pytest.raises(ValueError):operations.validate(Entry('Claude',home,session,'title','project',0,files,kind='cache'))
        source=cache/'source';source.mkdir();good.extra={'protected_projects':[str(source)]}
        with pytest.raises(ValueError):operations.validate(good)
        entry=claude(home)
        for folder in [home/'projects',home/'other'/'fixture',home/'projects'/'fixture'/'extra']:
            folder.mkdir(parents=True,exist_ok=True)
            with pytest.raises(ValueError):operations.validate_project_folder(entry,folder)
        protected=entry.files[0].parent;entry.extra={'protected_projects':[str(protected)]}
        with pytest.raises(ValueError):operations.validate_project_folder(entry,protected)

@pytest.mark.parametrize('name',['nested/state_5.sqlite','wrong.sqlite'])
def test_codex_database_must_be_directly_under_its_profile_and_named_state(name):
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);entry=codex(home,home/name)
        with pytest.raises(ValueError):operations.validate(entry)

@pytest.mark.parametrize('delta,accept',[(0,True),(1,True),(-1,True),(2,False),(-2,False)])
def test_codex_update_tolerance_has_an_inclusive_one_second_boundary(delta,accept):
    with tempfile.TemporaryDirectory()as temporary:
        entry=codex(Path(temporary));entry.updated+=delta
        if accept:operations.validate(entry)
        else:
            with pytest.raises(ValueError):operations.validate(entry)

def test_checked_nodes_rejects_an_internal_reparse_and_no_links_checks_ancestors():
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);parent=home/'cache';parent.mkdir();child=parent/'reparse';child.mkdir()
        original=Path.lstat
        def info(path):
            if path==child:return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
            return original(path)
        with patch.object(Path,'lstat',info),pytest.raises(ValueError):list(operations._checked_nodes(parent,home))
        def ancestor(path):
            if path==parent:return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
            return original(path)
        with patch.object(Path,'lstat',ancestor),pytest.raises(ValueError):operations._no_links(child)

def temp_artifact(base,age_days):
    root=base/'temp';root.mkdir();path=root/'codex_fixture';path.mkdir();child=path/'payload';child.write_bytes(b'fixture')
    now=10_000_000;old=now-age_days*86400
    os.utime(child,(old,old));os.utime(path,(old,old))
    entry=Entry('Artifacts',root,'fixture','title','none',old,[path],kind='artifact',extra={
        'category':'temp-agent','tier':1,'method':'delete','scan_state':artifacts._snapshot(path,recursive=True)[1]})
    return entry,now

@pytest.mark.parametrize('days,allowed',[(1,False),(7,False),(7+1/86400,True),(8,True)])
def test_artifact_idle_boundaries_and_valid_shape(days,allowed):
    with tempfile.TemporaryDirectory()as temporary:
        entry,now=temp_artifact(Path(temporary),days)
        with patch.dict(os.environ,{'TEMP':str(entry.home)}),patch.object(artifacts,'process_owners',return_value=[]):
            if allowed:artifacts.revalidate(entry,live=[],now=now,apps=set());operations.validate(entry)
            else:
                with pytest.raises(ValueError):artifacts.revalidate(entry,live=[],now=now,apps=set())

def test_artifact_shape_cannot_bypass_tier_method_or_blocked_flags():
    with tempfile.TemporaryDirectory()as temporary:
        entry,now=temp_artifact(Path(temporary),8)
        with patch.dict(os.environ,{'TEMP':str(entry.home)}):
            operations.validate(entry)
            entry.blocked='fixture blocked'
            with pytest.raises(ValueError):operations.validate(entry)
            entry.blocked=''
            for key,bad in [('tier',2),('method','recycle'),('category','unknown')]:
                saved=entry.extra[key];entry.extra[key]=bad
                with pytest.raises(ValueError):operations.validate(entry)
                entry.extra[key]=saved

def test_empty_cleanup_is_rejected_and_default_caches_are_actually_included():
    with pytest.raises(ValueError):operations.delete([],{},detector=lambda:set())
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);cache=home/'cache';cache.mkdir();(cache/'x').write_bytes(b'fixture')
        entry=Entry('Claude',home,'cache:cache','cache','project',0,[cache],kind='cache')
        removed=[]
        def recycle(path):
            import shutil
            removed.append(path);shutil.rmtree(path)
        _,good,failed,notes=operations.delete([entry],{entry.key:model.signature(entry)},detector=lambda:set(),recycler=recycle)
        assert good==[entry]and failed==[]and removed==[cache]
        assert not notes

def test_direct_artifact_removal_rechecks_the_expected_tree_before_unlink():
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);path=home/'candidate';path.mkdir();child=path/'changed';child.write_bytes(b'old')
        state=operations._target_state(path,home);child.write_bytes(b'changed')
        with pytest.raises(ValueError):operations._remove_artifact(path,home,state)
        assert child.read_bytes()==b'changed'

def test_direct_artifact_removal_refuses_replacement_after_state_check():
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);path=home/'candidate';path.mkdir();child=path/'changed';child.write_bytes(b'old')
        state=operations._target_state(path,home);original=operations._target_state
        def changed(path,home):
            value=original(path,home);child.write_bytes(b'new user data');return value
        with patch.object(operations,'_target_state',changed),pytest.raises(ValueError):operations._remove_artifact(path,home,state)
        assert child.read_bytes()==b'new user data'

def test_a_stale_record_blocks_the_entire_batch_before_a_healthy_record_is_removed():
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);one=claude(home);two=claude(home)
        expected={e.key:model.signature(e)for e in[one,two]};one.files[0].write_bytes(b'changed')
        removed=[]
        with pytest.raises(ValueError):operations.delete([one,two],expected,detector=lambda:set(),recycler=removed.append)
        assert removed==[] and two.files[0].exists()

def test_fresh_signature_mismatch_at_confirmation_never_reaches_recycler():
    with tempfile.TemporaryDirectory()as temporary:
        entry=claude(Path(temporary));expected=model.signature(entry);count=[0];removed=[]
        def observed(row):
            count[0]+=1
            return expected if count[0]==1 else 'observed-change'
        with patch.object(operations,'signature',observed):
            _,good,failed,_=operations.delete([entry],{entry.key:expected},detector=lambda:set(),recycler=removed.append)
        assert good==[]and len(failed)==1 and removed==[]and entry.files[0].exists()

def test_cache_cleanup_does_not_rewrite_unrelated_history_or_emit_codex_project_notes():
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);cache=home/'cache';cache.mkdir();(cache/'x').write_bytes(b'fixture')
        entry=Entry('Claude',home,'cache:cache','cache','project',0,[cache],kind='cache')
        history=home/'history.jsonl';raw=json.dumps({'sessionId':entry.session,'note':'preserve even malformed history'})+'\n';history.write_text(raw,encoding='utf8')
        def recycle(path):
            import shutil
            shutil.rmtree(path)
        _,good,failed,notes=operations.delete([entry],{entry.key:model.signature(entry)},project_groups={entry.group},detector=lambda:set(),recycler=recycle)
        assert good==[entry]and failed==[]and notes==[]
        assert history.read_text(encoding='utf8')==raw

def test_requested_backup_includes_cache_payloads_by_default():
    import zipfile
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary)/'profile';cache=home/'cache';cache.mkdir(parents=True);(cache/'x').write_bytes(b'cache fixture')
        entry=Entry('Claude',home,'cache:cache','cache','project',0,[cache],kind='cache')
        def recycle(path):
            import shutil
            shutil.rmtree(path)
        archive,good,failed,_=operations.delete([entry],{entry.key:model.signature(entry)},Path(temporary)/'backups',detector=lambda:set(),recycler=recycle,create_backup=True)
        assert good==[entry]and failed==[]and archive is not None
        with zipfile.ZipFile(archive)as z:
            assert any(z.read(name)==b'cache fixture'for name in z.namelist()if not name.endswith('/'))

@pytest.mark.parametrize('extra',['source.txt','chats/notes.txt'])
def test_gemini_project_container_preserves_nonchat_leftovers(extra):
    with tempfile.TemporaryDirectory()as temporary:
        home=Path(temporary);sid=str(uuid.uuid4());folder=home/'tmp'/'fixture';chats=folder/'chats';chats.mkdir(parents=True)
        transcript=chats/f'{sid}.json';transcript.write_text('{}',encoding='utf8');private=folder/extra;private.write_bytes(b'private content')
        entry=Entry('Gemini',home,sid,'fixture','project',0,[transcript],extra={'project_dir':str(folder)})
        removed=[]
        def recycle(path):
            import shutil
            removed.append(path)
            shutil.rmtree(path)if path.is_dir()else path.unlink()
        _,good,failed,notes=operations.delete([entry],{entry.key:model.signature(entry)},project_groups={entry.group},detector=lambda:set(),recycler=recycle)
        assert good==[entry]and failed==[]and removed==[transcript]
        assert private.read_bytes()==b'private content'and notes

def test_concurrently_removed_empty_container_is_a_successful_idempotent_cleanup():
    with tempfile.TemporaryDirectory()as temporary:
        entry=claude(Path(temporary));entry.extra={'project_dir':str(entry.files[0].parent)}
        original=operations.validate_project_folder;checks=[0]
        def validated(row,folder):
            original(row,folder);checks[0]+=1
            if checks[0]==2:folder.rmdir()
        with patch.object(operations,'validate_project_folder',validated):
            _,good,failed,notes=operations.delete([entry],{entry.key:model.signature(entry)},project_groups={entry.group},detector=lambda:set(),recycler=lambda path:path.unlink())
        assert good==[entry]and failed==[]and notes==[]
