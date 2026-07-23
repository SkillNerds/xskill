"""test_windows_script_encoding.py —— Windows 脚本编码守护

背景（内网装机现场反复踩的坑）：

1. **源文件层**：Windows PowerShell 5.1 读取无 BOM 的 .ps1 按系统 ANSI
   代码页（中文系统=GBK）解析——含中文的脚本注释全乱，多字节错切还可能
   吞掉引号导致 ParserError。所以仓库里所有 .ps1 必须 UTF-8 with BOM。
2. **输出层**：PS 5.1 往管道写输出默认用系统代码页（GBK），而 agent/CI
   按 UTF-8 读，中文提示全变 U+FFFD 替换符——"把输出交给 agent 排查"的
   设计直接失效。所以所有 .ps1 必须显式把输出编码设为 UTF-8。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GENERATED_SKILLOPT_WORKSPACE = (
    REPO_ROOT / "examples" / "kernels" / "skillopt" / "workspace"
)

PS1_FILES = sorted(
    p for p in REPO_ROOT.rglob("*.ps1")
    if (
        ".git" not in p.parts
        and "node_modules" not in p.parts
        # Materialized benchmark checkouts and their virtualenv are ignored,
        # third-party runtime data rather than XSkill's shipped scripts.
        and not p.is_relative_to(GENERATED_SKILLOPT_WORKSPACE)
    )
)

UTF8_BOM = b"\xef\xbb\xbf"


def test_found_ps1_scripts():
    """守护自身有效性：仓库确实有 .ps1，glob 没有悄悄失效。"""
    assert PS1_FILES, "仓库里应存在 .ps1 脚本；若已全部删除请同步删除本测试"


@pytest.mark.parametrize("ps1", PS1_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_ps1_has_utf8_bom(ps1: Path):
    head = ps1.read_bytes()[:3]
    assert head == UTF8_BOM, (
        f"{ps1.name} 缺 UTF-8 BOM：PS 5.1 会按 GBK 解析源文件，"
        f"中文乱码且可能 ParserError。保存为 UTF-8 with BOM。")


@pytest.mark.parametrize("ps1", PS1_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_ps1_sets_utf8_output_encoding(ps1: Path):
    text = ps1.read_text(encoding="utf-8-sig")
    assert "[Console]::OutputEncoding" in text and "UTF8" in text, (
        f"{ps1.name} 未显式设置输出编码：中文 Windows 的 PS 5.1 默认按 GBK "
        f"写管道，UTF-8 消费方（agent/CI）读到的是乱码。在 param() 之后加：\n"
        f"try {{\n"
        f"    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
        f"    $OutputEncoding = [System.Text.Encoding]::UTF8\n"
        f"}} catch {{}}")
