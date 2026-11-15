from unittest.mock import Mock
import pytest
from manager import operations
from manager.model import Entry, signature

SID = '11111111-1111-4111-8111-111111111111'

@pytest.mark.parametrize('relative', ['unlisted/' + SID + '.jsonl', 'projects/p/wrong-name.jsonl'])
def test_claude_validate_rejects_unlisted_top_level_and_wrong_filename(tmp_path, relative):
    home = tmp_path / 'profile'
    target = home / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b'synthetic transcript')
    entry = Entry('Claude', home, SID, 'synthetic', str(tmp_path / 'project'), 0, [target])
    with pytest.raises(ValueError):
        operations.validate(entry)
    assert target.read_bytes() == b'synthetic transcript'

def test_failed_chat_recycle_never_submits_its_workspace_to_recycler(tmp_path):
    home = tmp_path / 'profile'
    target = home / 'projects' / 'p' / (SID + '.jsonl')
    target.parent.mkdir(parents=True)
    target.write_bytes(b'synthetic transcript')
    workspace = tmp_path / 'project'
    workspace.mkdir()
    entry = Entry('Claude', home, SID, 'synthetic', str(workspace), 0, [target])
    def refused(path):
        if path == target:
            raise RuntimeError('synthetic transcript recycle failure')
    recycler = Mock(side_effect=refused)
    _, good, failed, _ = operations.delete(
        [entry], {entry.key: signature(entry)}, detector=lambda: set(), recycler=recycler,
        workspaces=[workspace], all_entries=[entry], create_backup=False,
    )
    recycler.assert_called_once_with(target)
    assert not good and len(failed) == 2
    assert target.exists() and workspace.is_dir()
