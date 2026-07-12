"""Windows 输出流兜底：pythonw 的 None 流 + 单字节控制台编码。

schtasks / 启动文件夹常驻用 pythonw 运行（免弹窗），其 sys.stdout/stderr
为 None——CLI 任何 print 直接 AttributeError 崩进程（常驻秒死、schtasks
每分钟空转重启）。main() 入口必须换成 devnull 流并统一 UTF-8。
"""
from __future__ import annotations

import sys

import pytest

import xskill.cli as cli


def test_main_survives_pythonw_null_streams(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(sys, "argv", ["xskill", "--version"])

    # argparse 的 --version 打印到 sys.stdout 后 SystemExit(0)；若 main 没
    # 兜底 None 流，这里会是 AttributeError 而非干净退出。
    with pytest.raises(SystemExit) as ei:
        cli.main()
    assert ei.value.code == 0
    assert sys.stdout is not None      # 已被替换为可写流
    assert sys.stderr is not None


def test_main_reconfigures_streams_to_utf8(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    # 模拟 cp1252 控制台：中文写入会炸的流
    out = open(tmp_path / "out.txt", "w", encoding="cp1252")
    err = open(tmp_path / "err.txt", "w", encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(sys, "argv", ["xskill", "--version"])

    with pytest.raises(SystemExit):
        cli.main()
    print("中文输出不应该炸")           # reconfigure 后写中文安全
    sys.stdout.flush()
    assert sys.stdout.encoding.lower().replace("-", "") == "utf8"
