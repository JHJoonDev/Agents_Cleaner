import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from manager import project_sizes as sizes, artifacts, runtime, workspaces

def item(base):
    project=base/'project';cache=project/'node_modules';cache.mkdir(parents=True);(cache/'payload').write_bytes(b'fixture')
    with patch.object(artifacts,'_git_problem',return_value=''):
        scanned=sizes.scan_project(project)
    return scanned,scanned.candidates[0]

@pytest.mark.parametrize('reason',['candidate-blocked','reparse','protected'])
def test_recycle_does_not_trust_a_cached_plan_over_current_path_protection(reason):
    with tempfile.TemporaryDirectory()as temporary:
        project,candidate=item(Path(temporary));recycler=Mock()
        if reason=='candidate-blocked':candidate.blocked='manual interlock'
        clean=sizes.Project(project.path,candidates=[sizes.Candidate(candidate.path,candidate.size,candidate.kind,candidate.fingerprint)])
        with patch.object(sizes,'scan_project',return_value=clean),patch.object(sizes,'is_link',return_value=reason=='reparse'),patch.object(sizes,'protected',return_value=reason=='protected'),patch.object(runtime,'active_providers',return_value=set()),patch.object(workspaces,'fixed_problem',return_value=''),patch.object(artifacts,'live_cwds',return_value=[]),patch.object(artifacts,'process_owners',return_value=[]),patch('manager.recycle.recycle',recycler):
            with pytest.raises(ValueError):sizes.recycle_candidate(candidate,project)
        recycler.assert_not_called()

@pytest.mark.parametrize('apps',[None,[],{'Unknown'},''])
def test_unknown_process_state_is_rejected_before_recycle(apps):
    with tempfile.TemporaryDirectory()as temporary:
        project,candidate=item(Path(temporary));recycler=Mock()
        with patch.object(runtime,'active_providers',return_value=apps),patch.object(workspaces,'fixed_problem',return_value=''),patch('manager.recycle.recycle',recycler):
            with pytest.raises(RuntimeError):sizes.recycle_candidate(candidate,project)
        recycler.assert_not_called()

def test_scan_deduplicates_roots_and_returns_candidates_largest_first():
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);project=base/'project';project.mkdir()
        (project/'large.bin').write_bytes(b'1234');(project/'small.bin').write_bytes(b'12')
        with patch.object(artifacts,'_git_problem',return_value=''):
            result,errors=sizes.scan_roots([base,base],large=1)
        assert errors==[]and len(result)==1
        assert [c.size for c in result[0].candidates]==[4,2]

def test_root_cache_is_not_its_own_candidate_and_nested_excluded_trees_are_not_counted():
    with tempfile.TemporaryDirectory()as temporary:
        root=Path(temporary)/'node_modules';root.mkdir();(root/'small.bin').write_bytes(b'123')
        excluded=root/'ToxAuto-private';excluded.mkdir();(excluded/'secret.bin').write_bytes(b'private')
        with patch.object(artifacts,'_git_problem',return_value=''):
            scanned=sizes.scan_project(root,large=10)
        assert scanned.size==3 and scanned.candidates==[]
        count,_,reason=sizes.measure(excluded,[])
        assert count==0 and reason

def test_excluded_scan_root_and_reparse_ancestor_are_not_traversed():
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);excluded=base/'ToxAuto-private';(excluded/'project').mkdir(parents=True)
        (excluded/'project'/'payload.bin').write_bytes(b'private')
        result,errors=sizes.scan_roots([excluded],large=1);assert not result and errors
        normal=base/'normal';(normal/'project').mkdir(parents=True)
        with patch.object(sizes,'is_link',side_effect=lambda path:path==normal):
            result,errors=sizes.scan_roots([normal]);assert not result and errors

def test_reparse_candidate_is_never_counted_or_descended_and_unsupported_nodes_are_ignored():
    with tempfile.TemporaryDirectory()as temporary:
        project,candidate=item(Path(temporary));original=Path.lstat
        def metadata(path):
            value=original(path)
            if path==candidate.path:return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400)
            return value
        with patch.object(Path,'lstat',metadata):
            count,_,reason=sizes.measure(candidate.path,[]);assert count==0 and reason
            scanned=sizes.scan_project(project.path);assert scanned.size==0 and not scanned.candidates
        unsupported=project.path/'socket.bin';unsupported.write_bytes(b'payload')
        def non_file(path):
            value=original(path)
            if path==unsupported:
                return SimpleNamespace(st_mode=stat.S_IFSOCK,st_file_attributes=0,st_ino=value.st_ino,st_mtime_ns=value.st_mtime_ns,st_size=value.st_size)
            return value
        with patch.object(Path,'lstat',non_file),patch.object(artifacts,'_git_problem',return_value=''):
            count,_,_=sizes.measure(unsupported,[]);assert count==0
            scanned=sizes.scan_project(project.path,large=1)
            assert not any(c.path==unsupported for c in scanned.candidates)

def test_private_data_does_not_trigger_external_git_inspection():
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);path=base/'output'/'data.db';path.parent.mkdir();path.write_bytes(b'private')
        with patch.object(artifacts,'_git_problem',side_effect=AssertionError('protected data must not be inspected')):
            result=sizes.scan_project(base,large=1)
        assert result.candidates and all(c.blocked for c in result.candidates)
