from __future__ import annotations

import os
import stat

from xskill.skill import skill


def test_delete_skill_retries_readonly_git_object(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills"
    git_object = skill_dir / "demo" / ".git" / "objects" / "aa" / "object"
    git_object.parent.mkdir(parents=True)
    git_object.write_bytes(b"git object")
    git_object.chmod(stat.S_IREAD)
    monkeypatch.setattr(skill, "commit_changes", lambda *_args: True)

    original_unlink = os.unlink
    attempts = 0

    def windows_unlink(path, *args, **kwargs):
        nonlocal attempts
        if os.fspath(path).endswith("object"):
            attempts += 1
            mode = os.stat(path, dir_fd=kwargs.get("dir_fd")).st_mode
            if not mode & stat.S_IWRITE:
                raise PermissionError("read-only Git object")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", windows_unlink)

    assert skill.delete_skill(skill_dir, "demo")
    assert attempts == 2
    assert not (skill_dir / "demo").exists()
