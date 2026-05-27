"""
test_cli_version.py — `xskill -v` / `xskill --version` 烟测

确认：
1. 两个 flag 都触发 argparse 的 version action 并以 SystemExit(0) 退出
2. 打印的版本字符串严格等于 ``xskill <__version__>``——即不在 cli.py 里
   硬编版本，单一真源在 setuptools_scm 写出的 ``_version.py``
"""
from __future__ import annotations

import pytest

from xskill import __version__
from xskill.cli import build_parser


@pytest.mark.parametrize("flag", ["-v", "--version"])
def test_version_flag_prints_and_exits(flag, capsys):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([flag])
    # argparse `action="version"` 用 SystemExit(0) 表示成功打印后退出
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    # version 走 stdout（argparse 行为）
    assert captured.out.strip() == f"xskill {__version__}"
