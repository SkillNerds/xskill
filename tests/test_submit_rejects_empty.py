import pytest

from xskill.ecosystems._shared import submit_trajectory


def test_submit_rejects_empty_content(tmp_path):
    with pytest.raises(ValueError, match="non-empty"):
        submit_trajectory(content="", format="markdown", traj_dir=tmp_path)


def test_submit_rejects_whitespace_content(tmp_path):
    with pytest.raises(ValueError, match="non-empty"):
        submit_trajectory(content="   \n  \n", format="raw", traj_dir=tmp_path)


def test_submit_rejects_empty_json_object(tmp_path):
    with pytest.raises(ValueError, match="non-empty"):
        submit_trajectory(content="{}", format="json", traj_dir=tmp_path)
