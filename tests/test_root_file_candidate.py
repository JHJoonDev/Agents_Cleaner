from manager.project_sizes import scan_roots

def test_file_root_is_reported_as_a_string_error_and_other_roots_still_scan(tmp_path):
    file_root = tmp_path / 'root-file'
    file_root.write_bytes(b'synthetic')
    good_root = tmp_path / 'valid-root'
    project = good_root / 'project'
    project.mkdir(parents=True)
    projects, errors = scan_roots([file_root, good_root])
    assert [row.path for row in projects] == [project]
    assert len(errors) == 1 and isinstance(errors[0], str)
    assert str(file_root) in errors[0]
    assert file_root.read_bytes() == b'synthetic'
