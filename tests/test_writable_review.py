"""Both policy directions without creating windows or starting processes."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from manager.model import Entry
from manager.qt_logic import Logic
from manager import qt_sizes, app_control

def test_writable_review_opens_selected_records_and_stop_requires_explicit_confirmation():
    entry=Entry('Claude',Path('synthetic'),'id','title','project',0)
    opened=Mock(return_value='opened')
    host=SimpleNamespace(busy=False,allow_delete=True,errors=[],entries=[entry],checked={entry.key},
        can_select=lambda e:True,workspaces=set(),open_review=opened,process_dialog=None,status_text=Mock())
    assert Logic.review(host)=='opened'
    opened.assert_called_once_with([entry],[])
    group=SimpleNamespace(stoppable=True);launched=[]
    host.launch=lambda work,event:launched.append((work(),event))
    with patch('manager.qt_logic.ask',return_value=True),patch.object(app_control,'stop_summary',return_value='synthetic'),patch.object(app_control,'request_close',return_value=([],[]))as close:
        Logic.stop_apps(host,[group])
    close.assert_called_once_with([group]);assert launched==[(([],[]),'stop')]
    with patch('manager.qt_logic.ask',return_value=False),patch.object(app_control,'stop_summary',return_value='synthetic'),patch.object(app_control,'request_close')as close:
        Logic.stop_apps(host,[group])
    close.assert_not_called();assert len(launched)==1

def test_writable_size_review_offers_confirmation_and_cancel_never_recycles():
    candidate=SimpleNamespace(path=Path('synthetic/selected.bin'));project=SimpleNamespace(path=Path('synthetic'))
    tree=Mock();tree.topLevelItemCount.return_value=1
    parent=tree.topLevelItem.return_value;parent.childCount.return_value=1
    child=parent.child.return_value;child.checkState.return_value=qt_sizes.Qt.CheckState.Checked
    child.data.return_value=(project,candidate)
    host=SimpleNamespace(allow_delete=True,tree=tree)
    dialog=Mock();dialog.exec.return_value=0
    dialogs=Mock(return_value=dialog);dialogs.DialogCode=SimpleNamespace(Accepted=1)
    buttons=Mock();buttons.StandardButton=SimpleNamespace(Ok=1,Cancel=2)
    with patch.object(qt_sizes,'QDialog',dialogs),patch.object(qt_sizes,'QVBoxLayout'),patch.object(qt_sizes,'QLabel'),patch.object(qt_sizes,'QCheckBox'),patch.object(qt_sizes,'QDialogButtonBox',buttons),patch.object(qt_sizes,'recycle_candidate')as recycle:
        qt_sizes.SizeTab.review(host)
    dialogs.assert_called_once_with(host);dialog.exec.assert_called_once();recycle.assert_not_called()
