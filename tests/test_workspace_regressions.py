import contextlib
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
import pytest
from manager import workspaces as ws

@contextlib.contextmanager
def policy_tree():
    with tempfile.TemporaryDirectory()as temporary:
        base=Path(temporary);user=base/'user';user.mkdir()
        env={name:str(base/'system'/name.replace('(','').replace(')',''))for name in ['SystemRoot','ProgramFiles','ProgramFiles(x86)','ProgramData','APPDATA','LOCALAPPDATA','TEMP']}
        for value in env.values():Path(value).mkdir(parents=True,exist_ok=True)
        program=base/'program';program.mkdir()
        with patch.object(Path,'home',return_value=user),patch.dict(os.environ,env),patch.object(ws,'PROGRAM_ROOT',program):
            yield base,user,env

def test_relative_existing_parent_references_and_missing_folders_are_explicitly_refused():
    with policy_tree()as(base,user,env):
        project=base/'project';project.mkdir();(base/'child').mkdir()
        previous=Path.cwd()
        try:
            os.chdir(base)
            assert ws.fixed_problem(Path('project'))
            assert ws.workspace_problem(Path('project'),[],set(),[])
            assert ws.fixed_problem(base/'child'/'..'/'project')
            assert '없음' in ws.fixed_problem(base/'missing')
        finally:os.chdir(previous)

def test_drive_roots_are_refused_even_when_the_user_and_system_are_on_another_drive():
    with policy_tree():
        with patch.object(Path,'is_dir',return_value=True),patch.object(Path,'resolve',lambda path:path.absolute()),patch.object(ws,'is_link',return_value=False):
            assert ws.fixed_problem(Path('Q:/'))

def test_user_home_cannot_be_deleted_even_with_profile_and_system_data_elsewhere():
    with policy_tree()as(base,user,env):assert ws.fixed_problem(user,[])

@pytest.mark.parametrize('name',['Documents','Downloads','Desktop','OneDrive - synthetic'])
def test_personal_home_folders_are_protected(name):
    with policy_tree()as(base,user,env):
        path=user/name;path.mkdir()
        assert ws.fixed_problem(path,[])

def test_onedrive_personal_folders_are_protected_without_blocking_unrelated_projects():
    with policy_tree()as(base,user,env):
        cloud=user/'OneDrive-Synthetic';cloud.mkdir()
        personal=cloud/'Documents';personal.mkdir();project=cloud/'MyProject';project.mkdir()
        assert ws.fixed_problem(personal,[])
        assert ws.fixed_problem(project,[])==''
        elsewhere=base/'other'/'OneDrive-Synthetic'/'Documents';elsewhere.mkdir(parents=True)
        assert ws.fixed_problem(elsewhere,[])==''
        local=base/'other'/'Documents';local.mkdir()
        assert ws.fixed_problem(local,[])==''

@pytest.mark.parametrize('var',['APPDATA','LOCALAPPDATA','TEMP','SystemRoot','ProgramFiles','ProgramData'])
def test_system_data_roots_also_protect_their_ancestors(var):
    with policy_tree()as(base,user,env):
        target=base/'target';target.mkdir();child=target/'system-data';child.mkdir()
        with patch.dict(os.environ,{var:str(child)}):assert ws.fixed_problem(target,[])

@pytest.mark.parametrize('var',['SystemRoot','ProgramFiles','ProgramFiles(x86)','ProgramData'])
def test_installed_program_and_system_descendants_are_never_workspaces(var):
    with policy_tree()as(base,user,env):
        target=Path(env[var])/'child';target.mkdir()
        assert ws.fixed_problem(target,[])

def test_program_location_ai_storage_ancestors_and_descendants_are_preserved():
    with policy_tree()as(base,user,env):
        target=base/'target';target.mkdir();program=target/'cleaner';program.mkdir()
        with patch.object(ws,'PROGRAM_ROOT',program):assert ws.fixed_problem(target,[])
        profile=base/'profile';profile.mkdir();nested=profile/'project';nested.mkdir()
        assert ws.fixed_problem(nested,[profile])
        assert ws.fixed_problem(profile.parent/'target',[target/'profile'])

def test_reparse_metadata_and_resolution_mismatch_are_independent_rejections():
    with policy_tree()as(base,user,env):
        target=base/'target';target.mkdir()
        with patch.object(ws,'is_link',side_effect=lambda path:path==target):assert ws.fixed_problem(target,[])
        original=Path.resolve
        def resolved(path):return base/'other'if path==target else original(path)
        with patch.object(Path,'resolve',resolved):assert ws.fixed_problem(target,[])
