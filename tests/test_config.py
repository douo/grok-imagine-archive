from grok_imagine_archive.config import project_root


def test_project_root_is_current_working_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert project_root() == tmp_path
