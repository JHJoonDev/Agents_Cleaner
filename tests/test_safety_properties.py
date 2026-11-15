"""Generated filesystem contracts; all files live below the outer QA boundary."""
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hypothesis import given, settings, strategies as st
import pytest
import sandbox_guard as guard
from manager.model import Entry, inside, safe_path, signature
from manager.operations import delete, validate
from manager.project_sizes import scan_roots
from manager.workspaces import fixed_problem, plan_workspaces
from manager.qt_logic import Logic
from manager.qt_sizes import SizeTab

settings.register_profile('ci',max_examples=100,deadline=None,database=None,derandomize=True)
settings.register_profile('qa',max_examples=1000,deadline=None,database=None,derandomize=True)
settings.load_profile(os.environ.get('HYPOTHESIS_PROFILE','ci'))
component=st.text(alphabet='ab_cd-012가나다한글éßΩЖ🙂\u0301',min_size=1,max_size=15).filter(lambda x:x.casefold() not in {'con','prn','aux','nul','com1','lpt1'})
trees=st.lists(st.tuples(st.lists(component,min_size=1,max_size=3),st.binary(max_size=64)),max_size=15)

def snapshot(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}

@given(trees,st.integers(0,65))
def test_generated_project_plans_stay_bounded_readonly_and_idempotent(nodes,limit):
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary)
        project=root/'연구-project'
        project.mkdir()
        for i,(parts,data) in enumerate(nodes):
            path=project/'node_modules'/f'case-{i}'/Path(*parts)/'payload.bin'
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(data)
        # Source/private data must be preserved even if above the size threshold.
        (project/'.env').write_bytes(b'fixture-secret')
        (project/'source.py').write_bytes(b'x'*70)
        before=snapshot(root)
        with patch('manager.artifacts._git_problem',return_value=''):
            first,errors=scan_roots([root],large=limit)
            second,again=scan_roots([root],large=limit)
        assert errors==again==[]
        def plans(projects):
            return [(p.path,p.size,[(c.path,c.size,c.kind,c.fingerprint,c.blocked) for c in p.candidates])for p in projects]
        assert plans(first)==plans(second)
        assert before==snapshot(root)
        for p in first:
            assert inside(p.path,root)
            for candidate in p.candidates:
                assert inside(candidate.path,p.path)
                if candidate.path.name in {'.env','source.py'}:assert candidate.blocked

@given(component,st.binary(max_size=80),st.sampled_from(['Claude','Gemini']))
def test_path_policy_never_accepts_root_sibling_parent_or_traversal(name,data,provider):
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);home=root/'allowed';home.mkdir();outside=root/'allowed-sibling';outside.mkdir()
        item=home/name;item.write_bytes(data)
        assert safe_path(item,home)==item
        for bad in [home,home.parent,outside,outside/name]:
            with pytest.raises(ValueError):safe_path(bad,home)
        forged=Entry(provider,home,'not-a-uuid','label',str(outside),0,[outside/name])
        with pytest.raises(ValueError):validate(forged)
        assert fixed_problem(home.parent,[home])

@given(st.binary(max_size=150),st.booleans(),st.booleans())
def test_readonly_entrypoints_cannot_launch_deletion_or_process_shutdown(data,busy,errors):
    with tempfile.TemporaryDirectory() as temporary:
        home=Path(temporary);path=home/'preserve';path.write_bytes(data)
        entry=Entry('Claude',home,'id','title','project',0,[path])
        forbidden=Mock(side_effect=AssertionError('read-only action escaped'))
        host=SimpleNamespace(allow_delete=False,busy=busy,errors=['fixture']if errors else[],
            entries=[entry],checked={entry.key},workspaces=set(),can_select=lambda e:True,
            open_review=forbidden,launch=forbidden,status_text=Mock(),process_dialog=None)
        before=snapshot(home)
        Logic.review(host)
        Logic.submit_review(host,[entry],set(),[entry],{'workspaces':[]})
        SizeTab.review(host)
        Logic.stop_apps(host,[SimpleNamespace(stoppable=True)])
        forbidden.assert_not_called()
        assert snapshot(home)==before

@given(st.lists(st.binary(max_size=50),min_size=1,max_size=8))
def test_confirmed_fixture_cleanup_is_bounded_and_repeat_is_idempotent(payloads):
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);home=root/'profile';cache=home/'cache';cache.mkdir(parents=True)
        untouched=root/'outside';untouched.write_bytes(b'preserve')
        for i,data in enumerate(payloads):(cache/f'{i}.bin').write_bytes(data)
        entry=Entry('Claude',home,'cache:cache','cache','none',0,[cache],kind='cache')
        expected={entry.key:signature(entry)}
        removed=[]
        def recycle_fixture(path):
            guard.check(path)
            assert inside(path,home)
            import shutil
            shutil.rmtree(path)
            removed.append(path)
        _,good,failed,_=delete([entry],expected,detector=lambda:set(),recycler=recycle_fixture)
        assert good==[entry] and not failed and removed==[cache]
        assert untouched.read_bytes()==b'preserve'
        with pytest.raises((ValueError,OSError)):
            delete([entry],expected,detector=lambda:set(),recycler=recycle_fixture)
        assert removed==[cache] and untouched.read_bytes()==b'preserve'

def test_windows_reparse_metadata_is_refused_without_following_a_junction():
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);home=root/'allowed';home.mkdir();link=home/'junction';link.mkdir()
        original=Path.lstat
        def metadata(path):
            if path==link:
                return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
            return original(path)
        with patch.object(Path,'lstat',metadata),pytest.raises(ValueError):safe_path(link,home)

def test_long_korean_path_remains_inside_allowed_root_and_is_not_selected_as_workspace():
    with tempfile.TemporaryDirectory() as temporary:
        home=Path(temporary);item=home/('한'*140)/'data.txt'
        try:
            item.parent.mkdir();item.write_text('synthetic',encoding='utf8')
        except OSError as exc:
            pytest.skip(f'long-path capability unavailable: {exc.winerror if hasattr(exc,"winerror")else exc.errno}')
        assert safe_path(item,home)==item
        plan=plan_workspaces([home],[Entry('Claude',home,'id','label','project',0)],set())
        assert plan[0]['problem']

def test_real_symlink_is_refused_when_windows_capability_exists():
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);home=root/'allowed';home.mkdir();outside=root/'elsewhere';outside.mkdir();link=home/'link'
        try:link.symlink_to(outside,target_is_directory=True)
        except OSError as exc:
            pytest.skip(f'symlink privilege unavailable: {getattr(exc,"winerror",None)}; reparse metadata covered separately')
        with pytest.raises(ValueError):safe_path(link,home)

@given(st.lists(component,min_size=1,max_size=3),st.integers(0,4),st.sampled_from(['symlink','junction']),st.sampled_from(['outside','cycle','in-root']))
def test_generated_reparse_graphs_never_escape_or_become_unblocked_candidates(parts,index,kind,destination):
    # Windows privilege may forbid creating real links. Emulate both reparse
    # metadata and resolver behavior; exercise outside, cyclic and in-root edges.
    from manager import model
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary);allowed=root/'allowed';project=allowed/'project';cache=project/'node_modules'
        branch=cache.joinpath(*parts);branch.mkdir(parents=True);leaf=branch/'payload';leaf.write_bytes(b'synthetic')
        outside=root/'outside';outside.mkdir();sentinel=outside/'preserve';sentinel.write_bytes(b'private outside allowed root')
        directories=[cache]+[cache.joinpath(*parts[:n])for n in range(1,len(parts)+1)]
        choices=directories+([leaf]if kind=='symlink'else[]);tag=choices[index%len(choices)]
        original_stat=Path.lstat;original_resolve=Path.resolve
        def metadata(path):
            info=original_stat(path)
            if path!=tag:return info
            return SimpleNamespace(st_mode=stat.S_IFLNK if kind=='symlink'else info.st_mode,
                st_file_attributes=getattr(info,'st_file_attributes',0)|0x400,
                st_dev=info.st_dev,st_ino=info.st_ino,st_size=info.st_size,st_mtime_ns=info.st_mtime_ns)
        def resolve(path,*args,**kwargs):
            if path==tag or tag in path.parents:
                if destination=='cycle':raise RuntimeError('synthetic resolver cycle')
                if destination=='outside':return outside/path.relative_to(tag)
            return original_resolve(path,*args,**kwargs)
        with patch.object(Path,'lstat',metadata),patch.object(Path,'resolve',resolve),patch('manager.artifacts._git_problem',return_value=''):
            with pytest.raises(ValueError):model.safe_path(leaf,project)
            plans,_=scan_roots([allowed],large=1)
            repeated,_=scan_roots([allowed],large=1)
            shape=lambda values:[(p.path,p.size,[(c.path,c.size,c.blocked,c.fingerprint)for c in p.candidates])for p in values]
            assert shape(plans)==shape(repeated)
            for planned in plans:
                for candidate in planned.candidates:
                    if not candidate.blocked:
                        assert inside(candidate.path,allowed)
                        assert candidate.path!=tag and tag not in candidate.path.parents
        assert sentinel.read_bytes()==b'private outside allowed root'
