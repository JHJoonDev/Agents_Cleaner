import importlib.util
from pathlib import Path
import pytest
import sandbox_guard as guard

spec=importlib.util.spec_from_file_location('qa_mutations',guard.ROOT/'qa/mutation_check.py')
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)

@pytest.mark.parametrize('name',['../scripts/main.py','scripts/main.py','C:/outside.json','../source.json','x:y.json','sub/report.json','results.py'])
def test_report_output_cannot_target_sources_or_escape_its_directory(name):
    with pytest.raises(ValueError):runner.report_path(name)

def test_report_writer_replaces_only_a_dedicated_fixture_json(monkeypatch,tmp_path):
    monkeypatch.setattr(runner,'REPORT_DIR',tmp_path/'reports')
    runner.write_report('result.json',{'status':'first'})
    runner.write_report('result.json',{'status':'second'})
    assert (tmp_path/'reports/result.json').read_text(encoding='utf8').find('second')>=0
    assert list((tmp_path/'reports').iterdir())==[tmp_path/'reports/result.json']

def test_stale_cache_is_rejected_before_starting_any_test_process(monkeypatch,tmp_path):
    monkeypatch.setattr(runner,'REPORT_DIR',tmp_path/'reports')
    runner.write_report('stale.json',{'fingerprint':'old','results':[]})
    monkeypatch.setattr(runner,'catalog',lambda:[])
    monkeypatch.setattr(runner,'fingerprint',lambda items:('new',{}))
    monkeypatch.setattr('sys.argv',['mutation_check.py','--resume','--output','stale.json'])
    with pytest.raises(ValueError,match='stale mutation cache'):runner.main()
