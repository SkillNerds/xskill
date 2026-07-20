"""tests/test_e2e_xskill_serve_auto.py — 真正的 E2E (AtomTask 流水线 + 灰度全链路)

从用户视角出发的 E2E，只用 subprocess + HTTP + fake LLM 服务器，**0 行
``import xskill.*``**。

验证两件事：

1. **自动 detect + bridge + index**：``xskill serve`` 起来后扫到
   ``~/.claude/projects/``，自动 register ``~/.xskill/cc_sessions/``，
   ``claude -p`` 写的 JSONL 被 daemon 桥成 traj_*.md → watcher 拆 AtomTask
   入索引。``GET /api/v1/registry/dirs`` 和 ``xskill registry list`` 反映
   状态。

2. **灰度翻牌全链路**（这是大头）：bootstrap 一个 skill X 含 main v1 +
   staging v2 → 起 daemon → 跑 16+ 次 claude -p → 每次 ingester 见到新
   session 就翻牌（main ↔ staging 交替装到 ~/.claude/skills/） → 每条
   traj 被打上 side header → watcher 跑 TaskAgent 拆 atom → cluster
   归入已有 skill → ux 打分员对每个 atom 评分 → AtomCanary.append (主键=atom_id)
   → check_and_decide 判 staging 胜出 → 自动 promote 替换 main → 下次
   claude -p 启动时 system prompt 看到 v2 内容。

mock 数据按功能块整理在文件顶部：SKILL_V1_BODY / SKILL_V2_BODY /
SESSION_PROMPTS / FAKE_LLM_SCORE_PROFILE / _atom_split_build / _cluster_build 等。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
import yaml

# fake server 是测试套基础设施 (不属于 xskill.* 内部 API)。
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))
from _fake_llm_server import (  # noqa: E402
    FakeLLMServer,
    Responder,
    make_anthropic_text_response,
    make_anthropic_tool_use_response,
    make_openai_chat_response,
    make_openai_tool_call_response,
)

CLAUDE_BIN = shutil.which("claude")
XSKILL_BIN = shutil.which("xskill")


# ════════════════════════════════════════════════════════════════════
# === MOCK DATA BLOCK ===
# 按"功能/角色"分组的 mock 数据，下面任何地方都从这里取常量。改测试
# 的预期数值时只动这一段。
# ════════════════════════════════════════════════════════════════════

# ── A. Skill 双版本 bootstrap 内容 ──────────────────────────────────
# v1 = main 分支当前内容；v2 = staging 候选内容。两者描述里都明文带
# "v1" / "v2" 标识，方便：
#   1) fake LLM ux 评分员从 traj prompt 里反推用的是哪版（实际 ux_score
#      prompt 自己就明文携带了 side= 字段，无需文本反推，这只是双保险）
#   2) 最后一次 claude -p 时通过 system prompt 里 v2 标识断言新版生效

SKILL_NAME = "list-py-files"

SKILL_V1_BODY = """---
name: list-py-files
description: >
  [v1] 列出工作目录下的 Python 源文件。典型触发："列 .py"、"看有哪些
  python 文件"。仅需 Bash 工具访问。
compatibility: >
  任何具备 Bash 的环境；只读操作。
metadata:
  version: 1
  created: "2026-05-11"
  last_updated: "2026-05-11"
  source_trajs: ["traj_seed_main_v1"]
  frozen: false
  use_count: 0
---

# 列出 Python 文件 (v1)

## 直接列出

1. 运行 `ls *.py` 查看当前目录的 .py 文件。

## 验证

2. 输出应为换行分隔的文件名列表。
"""

SKILL_V2_BODY = """---
name: list-py-files
description: >
  [v2] 列出工作目录及子目录下的 Python 源文件。典型触发："列出 python
  文件"、"找出所有 .py 文件包括子目录"。优先用 find 递归遍历，比 ls
  对嵌套结构更可靠。仅需 Bash 工具访问。
compatibility: >
  任何具备 Bash 的环境；只读操作；POSIX find 兼容。
metadata:
  version: 2
  created: "2026-05-11"
  last_updated: "2026-05-11"
  source_trajs: ["traj_seed_main_v1", "traj_seed_staging_v2_a", "traj_seed_staging_v2_b"]
  frozen: false
  use_count: 0
---

# 列出 Python 文件 (v2 — 递归友好)

## 决定遍历范围

1. 默认只看当前目录：`ls *.py`。
2. 需要子目录：`find . -name '*.py' -type f`。

   > ⚠️ 见 traj_seed_staging_v2_a：直接用 `ls *.py` 漏掉嵌套包，必须用 find。

## 验证

3. 输出非空且每行是合法路径；exit code 0。
"""


# ── B. Claude session 提示词清单 ───────────────────────────────────
# 分两类：含关键词 "list-py-files" 的 prompt 让 fake claude 调 Skill
# tool（消耗灰度配额）；不含的让它直接回 text（透明放过）。
# poll_interval=2s + session 间 sleep ≥ 2.6s 保证 ingester 在两条
# session 之间能跑完一次完整的 poll + 翻牌。

# 触发 Skill tool 调用的 prompt 关键标记（fake server 用它分流）
SKILL_TRIGGER_KEYWORD = "list-py-files"

# 13 个真正使用 skill 的 session——每个 prompt 显式 reference skill 名字
WITH_SKILL_PROMPTS = [
    "请用 list-py-files 帮我列出工作目录的 .py 文件",     # 1
    "调用 list-py-files：看下当前 python 源码",          # 2
    "list-py-files 帮忙列 .py",                          # 3
    "用 list-py-files 列出 python 文件",                 # 4
    "调 list-py-files 看 .py 清单",                       # 5
    "请 list-py-files 找出所有 python 文件",             # 6
    "list-py-files 跑一下",                              # 7
    "用 skill list-py-files 列 .py",                     # 8
    "list-py-files 帮我看哪些 .py 在这",                  # 9
    "调 list-py-files 列 python 源",                     # 10
    "list-py-files 列出 py 文件",                        # 11
    "请 list-py-files 工作目录的 python",                # 12
    "list-py-files 查 .py",                              # 13
]

# 3 个不使用 skill 的 session——纯文本对话，fake claude 直接回 text
# 用于验证 daemon 跳过这些 session，不消耗灰度配额、不打 header
WITHOUT_SKILL_PROMPTS = [
    "今天天气怎么样？",
    "讲个笑话",
    "1+1 等于几",
]

# promote 之后的最终验证 session（也用 skill 触发，看新版进 system prompt）
FINAL_VERIFY_PROMPT = "list-py-files 帮忙验证一下新版本"

# fake server 收到"使用 skill"请求时让模型回 tool_use(Skill, {skill: ...})；
# CC 收到后会在 tool_result 里把 SKILL.md body 塞回来 + "Launching skill"
# 标记。我们 fake 收到 tool_result 后回 final text 收尾。
FAKE_SKILL_FINAL_REPLY = "已通过 skill 完成 .py 列表查询。"
FAKE_PLAIN_REPLY = "这是个纯文本回复。"


# ── C. ux 评分曲线 ─────────────────────────────────────────────────
# fake LLM 评分员看到 "来自 main 分支" 给低分，"来自 staging 分支" 给
# 高分。这是 staging 胜出的根本驱动力。两个差值要够大（>>1）才能在
# canary controller 的 t-test / 平均分差里站稳。
FAKE_LLM_SCORE_PROFILE = {
    "main":    {"score": 4, "reasons": "[fake] v1 步骤粗糙，用户多次澄清"},
    "staging": {"score": 9, "reasons": "[fake] v2 一次到位，用户满意"},
}


# ── D. AtomTask 拆分假数据 ────────────────────────────────────────
# TaskAgent 使用 ``submit_atom`` 工具协议，不解析正文中的 XML。fake server
# 首轮扫描真实 ``[line:N]`` 地图并返回 tool_calls；Agno 执行完这些工具后会
# 带 role=tool 的消息再次请求，第二轮返回普通文本结束 agent loop。
def _atom_split_build(b: dict) -> dict:
    messages = b.get("messages") or []
    if any(
        isinstance(message, dict) and message.get("role") == "tool"
        for message in messages
    ):
        return make_openai_chat_response(
            "所有 atom 已通过 submit_atom 提交。",
            model=b.get("model", "fake"),
        )
    marks = re.findall(r"\[line:(\d+)\]", _msg_user_text(b))
    if not marks:
        raise AssertionError("atom-split responder: prompt 里没有 [line:] 标记")
    calls = [
        (
            "submit_atom",
            {
                "start_line": int(mark),
                "intent": "列出当前目录下的 .py 文件",
                "summary": "用户请求列 Python 文件；agent 调 Bash 输出文件列表。",
                "tags": ["file_ops", "bash"],
                "used_skills": ["list-py-files"],
                "ux_score": 7,
            },
        )
        for mark in marks
    ]
    return make_openai_tool_call_response(
        calls,
        model=b.get("model", "fake"),
    )


def test_atom_split_responder_uses_submit_atom_tool_protocol():
    first = _atom_split_build({
        "model": "fake",
        "messages": [
            {"role": "system", "content": "你是 AtomTask 拆分员"},
            {
                "role": "user",
                "content": "[line:7] first\n[line:21] second",
            },
        ],
    })

    choice = first["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    calls = choice["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == [
        "submit_atom",
        "submit_atom",
    ]
    arguments = [
        json.loads(call["function"]["arguments"])
        for call in calls
    ]
    assert [item["start_line"] for item in arguments] == [7, 21]

    final = _atom_split_build({
        "model": "fake",
        "messages": [
            {"role": "system", "content": "你是 AtomTask 拆分员"},
            {"role": "user", "content": "[line:7] first"},
            {
                "role": "tool",
                "tool_call_id": "call_fake_1",
                "content": "ok: 已记录 atom #1",
            },
        ],
    })
    assert final["choices"][0]["finish_reason"] == "stop"
    assert "submit_atom" in final["choices"][0]["message"]["content"]


# ── E. cluster agent 应答：把 atom 低权重归到已有 skill ──────────
# watcher 只会在所有 atom 都被 cluster 消费后触发 UX 评分。fake 必须遵守
# add_tasks_to_skill 工具协议；weightscore=1 且总 atom 数低于 jam_threshold，
# 不会改写本 E2E 预置的 main/staging 内容。
FAKE_CLUSTER_RESPONSE = "所有 atom 已低权重归入现有 skill。"


def _cluster_build(body: dict) -> dict:
    messages = body.get("messages") or []
    if body.get("tools"):
        batch_tools = [
            tool_payload["function"]
            for tool_payload in body["tools"]
            if (
                tool_payload.get("type") == "function"
                and tool_payload.get("function", {}).get("name")
                == "add_tasks_to_skill"
            )
        ]
        if len(batch_tools) != 1:
            raise AssertionError(
                "cluster responder: add_tasks_to_skill schema missing",
            )
        task_items = (
            batch_tools[0]["parameters"]["properties"]["tasks"]["items"]
        )
        if set(task_items.get("properties", {})) != {
            "atom_id",
            "weightscore",
            "note",
        }:
            raise AssertionError(
                "cluster responder: batch item schema fields invalid",
            )
        if set(task_items.get("required", [])) != {
            "atom_id",
            "weightscore",
        }:
            raise AssertionError(
                "cluster responder: batch item schema required invalid",
            )
    if any(
        isinstance(message, dict) and message.get("role") == "tool"
        for message in messages
    ):
        return make_openai_chat_response(
            FAKE_CLUSTER_RESPONSE,
            model=body.get("model", "fake"),
        )
    atom_ids = list(dict.fromkeys(
        re.findall(r"atom_id:\s*(\S+)", _msg_user_text(body))
    ))
    if not atom_ids:
        raise AssertionError("cluster responder: prompt 里没有 atom_id")
    return make_openai_tool_call_response(
        [
            (
                "add_tasks_to_skill",
                {
                    "skill_name": SKILL_NAME,
                    "tasks": [
                        {
                            "atom_id": atom_id,
                            "weightscore": 1,
                        }
                        for atom_id in atom_ids
                    ],
                },
            ),
        ],
        model=body.get("model", "fake"),
    )


def test_cluster_responder_consumes_each_atom_via_tool_calls():
    first = _cluster_build({
        "model": "fake",
        "messages": [
            {"role": "system", "content": "TaskClusterAgent"},
            {
                "role": "user",
                "content": "atom_id: atom-a\natom_id: atom-b",
            },
        ],
    })

    calls = first["choices"][0]["message"]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == [
        "add_tasks_to_skill",
    ]
    arguments = json.loads(calls[0]["function"]["arguments"])
    assert [
        item["atom_id"]
        for item in arguments["tasks"]
    ] == [
        "atom-a",
        "atom-b",
    ]
    assert all(
        item["weightscore"] == 1
        for item in arguments["tasks"]
    )

    final = _cluster_build({
        "messages": [
            {"role": "system", "content": "TaskClusterAgent"},
            {
                "role": "tool",
                "tool_call_id": "call_fake_1",
                "content": "ok",
            },
        ],
    })
    assert final["choices"][0]["finish_reason"] == "stop"


# ── F. canary 行为期望 ─────────────────────────────────────────────
# 跟 sandbox 里的 config.yaml 里 canary.min_samples 对齐：两边各到 5
# 条样本就能 decide。15 条 session 中应当大致一半 main 一半 staging，
# 任一边 ≥5 就触发判定。
CANARY_MIN_SAMPLES = 5


# ════════════════════════════════════════════════════════════════════
# === END MOCK DATA BLOCK ===
# ════════════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _poll(predicate, *, timeout: float = 30.0, interval: float = 0.5,
          desc: str = "predicate") -> None:
    """Spin until ``predicate()`` returns truthy or ``timeout`` expires."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(interval)
    msg = f"timeout waiting for {desc} after {timeout}s"
    if last_err is not None:
        msg += f"; last exception: {last_err}"
    raise AssertionError(msg)


def _run_git(args: list[str], cwd: Path) -> tuple[int, str, str]:
    """轻量 git 执行——用于 bootstrap skill 仓库时 init/commit/branch。"""
    r = subprocess.run(["git"] + args, cwd=str(cwd),
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _bootstrap_canary_skill(skill_dir: Path) -> None:
    """造一个含 main v1 + staging v2 的真实 skill 仓库。

    顺序：
      1. init git in skill_dir/list-py-files
      2. 装 v1 内容 → commit 到 main
      3. 装 v2 内容 → commit 到 main（HEAD = v2, parent = v1）
      4. 把 HEAD (v2 commit) 挪到 staging 分支；main 回到 v1
      5. materialize_staging：把 staging 的 SKILL.md 物化到
         skill_dir/.canary/list-py-files/SKILL.md，让 install_to_claude_code
         (side='staging') 能直接读
    """
    skill_subdir = skill_dir / SKILL_NAME
    skill_subdir.mkdir(parents=True)
    _run_git(["init"], cwd=skill_subdir)
    _run_git(["checkout", "-b", "main"], cwd=skill_subdir)
    _run_git(["config", "user.email", "test@xskill"], cwd=skill_subdir)
    _run_git(["config", "user.name", "test"], cwd=skill_subdir)

    skill_md = skill_subdir / "SKILL.md"
    skill_md.write_text(SKILL_V1_BODY, encoding="utf-8")
    _run_git(["add", "-A"], cwd=skill_subdir)
    _run_git(["commit", "-m", "v1 baseline"], cwd=skill_subdir)
    code, v1_sha, _ = _run_git(["rev-parse", "HEAD"], cwd=skill_subdir)
    assert code == 0 and v1_sha, "v1 commit failed"

    skill_md.write_text(SKILL_V2_BODY, encoding="utf-8")
    _run_git(["add", "-A"], cwd=skill_subdir)
    _run_git(["commit", "-m", "v2 candidate"], cwd=skill_subdir)

    # 创建 staging 分支指向 v2 (当前 HEAD)，然后 main 回退到 v1
    _run_git(["branch", "staging"], cwd=skill_subdir)
    _run_git(["reset", "--hard", v1_sha], cwd=skill_subdir)
    # 校验：现在 main 内容是 v1，staging 分支存在且指向 v2
    assert skill_md.read_text(encoding="utf-8") == SKILL_V1_BODY
    code, staging_sha, _ = _run_git(["rev-parse", "staging"], cwd=skill_subdir)
    assert code == 0 and staging_sha != v1_sha

    # 物化 staging 到 .canary/ 方便 install_to_claude_code(side='staging') 读
    canary_dir = skill_dir / ".canary" / SKILL_NAME
    canary_dir.mkdir(parents=True)
    (canary_dir / "SKILL.md").write_text(SKILL_V2_BODY, encoding="utf-8")


# ════════════════════════════════════════════════════════════════════
# Fake server response programming
# ════════════════════════════════════════════════════════════════════

def _msg_user_text(body: dict) -> str:
    """OpenAI-style chat completion: 拼接所有 user message 的 text。"""
    parts: list[str] = []
    for m in body.get("messages", []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict):
                    parts.append(p.get("text", ""))
    return "\n".join(parts)


def _msg_system_text(body: dict) -> str:
    for m in body.get("messages", []):
        if m.get("role") != "system":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def _has_tool(body: dict, tool_name: str) -> bool:
    return any(t.get("name") == tool_name for t in body.get("tools", []))


def _has_tool_result(body: dict) -> bool:
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "tool_result":
                    return True
    return False


def _anthropic_user_text(body: dict) -> str:
    """从 Anthropic /v1/messages body 抽**真实用户 prompt**（不含 CC 注入
    的 system-reminder）。

    CC 把 user message 拆成多个 text part：前面几个 part 是 ``<system-
    reminder>...``（含 skill listing 等），最后一个 part 才是用户真正
    敲进去的 prompt。我们只取最后一个 part，避免 skill listing 里出现的
    skill 名误触发 keyword 匹配。

    跨多条 user 消息时（多轮对话），取最后一条 user 消息的最后一个 text。
    """
    last_text = ""
    for m in body.get("messages", []):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            last_text = c
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    last_text = p.get("text", "")
    return last_text


_UX_SIDE_RE = re.compile(r"来自\s*(main|staging)\s*分支")


def _program_fake_server(fake: FakeLLMServer) -> None:
    """把所有需要的 responder 一次性程序化进去。

    Anthropic /v1/messages 端：
      - "用了 skill"的 session（user prompt 含 list-py-files 关键词）→
        模型回 tool_use(Skill, {skill: list-py-files})；CC 内部
        Launching skill → tool_result 把 SKILL.md body 塞回来 → fake 回
        final text 收尾。
      - "没用 skill"的 session（普通问题）→ 直接 text reply，CC 写 JSONL
        但**没有** tool_use=Skill 事件，ingester 跳过翻牌。
      - 预处理请求（tools=[]）→ default text。
    OpenAI /chat/completions 端：
      - ux_score 区分 main/staging 给档分（驱动 staging 胜出）
      - TaskAgent atom 拆分（返回 submit_atom tool_calls）
      - TaskClusterAgent 用 add_tasks_to_skill 批量消费全部 atom
    Embeddings：默认 8-dim 向量足够（不写 responder fake server 自带）。
    """
    fake.reset()

    # ── Anthropic：CC 的 Skill tool 流程 ────────────────────────
    # 主请求且 user prompt 含 keyword + 还没收 tool_result → 调 Skill
    fake.add_responder("/v1/messages", Responder(
        name="claude-skill-invoke",
        match=lambda b: (
            _has_tool(b, "Skill")
            and not _has_tool_result(b)
            and SKILL_TRIGGER_KEYWORD in _anthropic_user_text(b)
        ),
        build=lambda b: make_anthropic_tool_use_response(
            tool_name="Skill",
            tool_input={"skill": SKILL_NAME},
            model=b.get("model", "claude-fake"),
        ),
    ))
    # 收到 Skill tool_result → final text
    fake.add_responder("/v1/messages", Responder(
        name="claude-skill-finalize",
        match=lambda b: _has_tool_result(b),
        build=lambda b: make_anthropic_text_response(
            FAKE_SKILL_FINAL_REPLY, model=b.get("model", "claude-fake"),
        ),
    ))
    # 主请求 + 不含 keyword + 没收 tool_result → 不调 Skill，直接 text
    fake.add_responder("/v1/messages", Responder(
        name="claude-plain-text",
        match=lambda b: (
            _has_tool(b, "Skill")
            and not _has_tool_result(b)
            and SKILL_TRIGGER_KEYWORD not in _anthropic_user_text(b)
        ),
        build=lambda b: make_anthropic_text_response(
            FAKE_PLAIN_REPLY, model=b.get("model", "claude-fake"),
        ),
    ))

    # ── OpenAI：ux 评分员（atom 粒度）─────────────────────────
    # v2 SYSTEM_PROMPT_ATOM 标志："你是用户体验评审员" + 严格分档表。
    # 区分主请求 user content 里的 "side=main" / "side=staging" 给档分。
    def _ux_match(body: dict) -> bool:
        sys_t = _msg_system_text(body)
        return "用户体验评审员" in sys_t and "严格分档表" in sys_t

    def _ux_build(body: dict) -> dict:
        user_text = _msg_user_text(body)
        if "side=staging" in user_text:
            side = "staging"
        elif "side=main" in user_text:
            side = "main"
        else:
            m = _UX_SIDE_RE.search(user_text)
            side = m.group(1) if m else "main"
        profile = FAKE_LLM_SCORE_PROFILE.get(side, FAKE_LLM_SCORE_PROFILE["main"])
        payload = json.dumps(profile, ensure_ascii=False)
        return make_openai_chat_response(payload, model=body.get("model", "fake"))

    fake.add_responder("/chat/completions", Responder(
        name="ux-score", match=_ux_match, build=_ux_build,
    ))

    # ── OpenAI：TaskAgent atom 拆分 ─────────────────────────────
    # SYSTEM_PROMPT 标志："你是 AtomTask 拆分员"。返回 submit_atom 调用。
    fake.add_responder("/chat/completions", Responder(
        name="atom-split",
        match=lambda b: "AtomTask 拆分员" in _msg_system_text(b),
        build=_atom_split_build,
    ))

    # ── OpenAI：TaskClusterAgent 低权重批量归入现有 skill ─────
    fake.add_responder("/chat/completions", Responder(
        name="cluster-consume",
        match=lambda b: "TaskClusterAgent" in _msg_system_text(b),
        build=_cluster_build,
    ))

    # ── OpenAI：SkillEditAgent noop（理论不该被触发；保险垫底） ──
    fake.add_responder("/chat/completions", Responder(
        name="edit-noop",
        match=lambda b: "SkillEditAgent" in _msg_system_text(b),
        build=lambda b: make_openai_chat_response(
            "candidates 不足，本次跳过编辑。", model=b.get("model", "fake"),
        ),
    ))

    # ── default fallback (chat completion) ────────────────────
    # 落不到上面任一 responder 的请求（例如 skill index search
    # 里的辅助 LLM 调用）拿一个 benign 回复，避免阻塞。


# ════════════════════════════════════════════════════════════════════
# Sandbox + daemon fixtures
# ════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def fake_server():
    srv = FakeLLMServer()
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def sandbox(tmp_path, fake_server):
    """Sandbox HOME + xskill config.yaml 指向 fake server + 工作目录。"""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps({"theme": "auto"}), encoding="utf-8",
    )
    (home / ".claude" / "projects").mkdir()
    (home / ".claude" / "skills").mkdir()

    xhome = home / ".xskill"
    xhome.mkdir()
    skill_dir = xhome / "skill"
    skill_dir.mkdir()

    config_yaml = {
        "skill_dir": str(skill_dir),
        "llm": {
            "base_url": fake_server.base_url,
            "model": "fake-llm",
            "api_key": "fake-key",
        },
        "embedding": {
            "base_url": fake_server.base_url,
            "model": "fake-embed",
            "api_key": "fake-key",
            "dim": 0,
        },
        "canary": {
            "enabled": True, "probability": 0.5,
            "min_samples": CANARY_MIN_SAMPLES, "max_days_hold": 14,
            # 本 E2E 的 source_model 可被模型分桶识别，判定会走 scoped
            # ``total_samples`` 而不是 legacy ``min_samples``。两者钉成同一
            # 小阈值，确保有限的 13 条 used-skill session 能完成裁决。
            "total_samples": CANARY_MIN_SAMPLES,
        },
        "watcher": {"poll_interval": 2},
        # 入库完成屏障（settle barrier，src/xskill 的 ingest 路径，默认 120s）
        # 会把"刚写完即扫描"的 session 全挡在窗口外 → daemon 子进程读本
        # config.yaml 拿到默认值，E2E 等不到桥接而超时。评测场景里 session
        # 由脚本一次性写完即定稿，关掉屏障（=0）即"出现即入库"。
        # 注意：conftest 的 _isolate_ingest_config 只 monkeypatch 进程内
        # ingest_config，管不到 ``xskill serve`` 子进程；子进程只认这份
        # config.yaml，故必须在此显式写 0。
        "ingest": {"settle_seconds": 0},
    }
    (xhome / "config.yaml").write_text(
        yaml.safe_dump(config_yaml, allow_unicode=True), encoding="utf-8",
    )

    workdir = home / "workdir"
    workdir.mkdir()
    (workdir / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (workdir / "utils.py").write_text("def x(): pass\n", encoding="utf-8")

    class Box:
        pass

    box = Box()
    box.home = home
    box.xhome = xhome
    box.skill_dir = skill_dir
    box.workdir = workdir
    box.cc_projects = home / ".claude" / "projects"
    box.cc_sessions = xhome / "cc_sessions"
    box.cc_installed_skill = home / ".claude" / "skills" / SKILL_NAME / "SKILL.md"
    box.fake = fake_server
    return box


def _child_env_for_sandbox(home: Path) -> dict:
    """子进程 env：HOME 指 sandbox 但 PYTHONUSERBASE 保留真实位置，让
    ``xskill`` CLI 能 import 自身。详见之前 commit 注释。"""
    import site as _site
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONUSERBASE"] = _site.USER_BASE
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(k, None)
    return env


@pytest.fixture
def xskill_daemon(sandbox):
    """Spawn the real ``xskill serve`` subprocess. Yields its base URL."""
    port = _free_port()
    env = _child_env_for_sandbox(sandbox.home)
    proc = subprocess.Popen(
        [XSKILL_BIN, "serve", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"

    def _ready() -> bool:
        try:
            r = requests.get(f"{base_url}/api/v1/registry/dirs", timeout=1.5)
            if r.status_code != 200:
                return False
            dirs = r.json().get("dirs", [])
            return any(d.get("ecosystem") == "claude_code" for d in dirs)
        except requests.RequestException:
            return False

    try:
        try:
            _poll(_ready, timeout=20.0,
                  desc="xskill serve startup + cc_sessions registered")
        except AssertionError:
            proc.terminate()
            try:
                _, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate(timeout=5)
            raise AssertionError(
                f"xskill serve did not become ready; subprocess stderr:\n"
                f"{stderr.decode('utf-8', errors='replace')[-2500:]}"
            )
        yield base_url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()


# ════════════════════════════════════════════════════════════════════
# Claude -p driver
# ════════════════════════════════════════════════════════════════════

def _run_claude_once(*, sandbox, fake_base_url: str, prompt: str,
                     timeout: int = 45) -> None:
    """Drive a single ``claude -p`` against the fake server."""
    env = os.environ.copy()
    env["HOME"] = str(sandbox.home)
    env["ANTHROPIC_BASE_URL"] = fake_base_url
    env["ANTHROPIC_AUTH_TOKEN"] = "fake-token"
    for k in ("ANTHROPIC_API_KEY",):
        env.pop(k, None)
    r = subprocess.run(
        [
            CLAUDE_BIN, "--model", "claude-sonnet-4-6",
            "--permission-mode", "bypassPermissions",
            "--allowed-tools", "Bash",
            "-p", prompt,
        ],
        cwd=str(sandbox.workdir),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise AssertionError(
            f"claude -p failed (code={r.returncode})\n"
            f"stdout: {r.stdout[-800:]}\nstderr: {r.stderr[-800:]}"
        )


# ════════════════════════════════════════════════════════════════════
# === TEST 1: auto-detect + bridge ===
# ════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(XSKILL_BIN is None, reason="xskill CLI not installed")
@pytest.mark.skipif(CLAUDE_BIN is None, reason="claude CLI not installed")
def test_serve_auto_detects_and_indexes_claude_sessions(sandbox, xskill_daemon):
    """轻量 E2E：daemon 起来后能自动注册 + 桥接 + 入索引。"""
    base_url = xskill_daemon
    fake = sandbox.fake
    _program_fake_server(fake)

    # ── 1. registry list 输出含 claude_code ecosystem 行 ─────────
    out = subprocess.run(
        [XSKILL_BIN, "registry", "list"],
        env=_child_env_for_sandbox(sandbox.home),
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0, f"xskill registry list failed: {out.stderr}"
    cc_line = next(
        (line for line in out.stdout.splitlines() if "claude_code" in line),
        None,
    )
    assert cc_line is not None, f"no claude_code line in:\n{out.stdout}"
    parts = cc_line.split("\t")
    assert len(parts) >= 6
    assert parts[1] == "claude_code"
    assert str(sandbox.cc_sessions.resolve()) == parts[-1]
    initial_traj_count = int(parts[2])

    # ── 2. 跑一次 claude -p 让 CC 写 JSONL ───────────────────────
    _run_claude_once(sandbox=sandbox, fake_base_url=fake.base_url,
                     prompt=WITH_SKILL_PROMPTS[0])
    assert list(sandbox.cc_projects.glob("*/*.jsonl")), "CC did not write JSONL"

    # ── 3. daemon 自动桥成 traj_*.md ─────────────────────────────
    _poll(lambda: any(sandbox.cc_sessions.glob("traj_*.md")),
          timeout=20.0, desc="bridged into cc_sessions")
    bridged = sorted(sandbox.cc_sessions.glob("traj_*.md"))
    md_text = bridged[0].read_text(encoding="utf-8")
    assert "Claude Code Session Trajectory" in md_text
    # 这个 lightweight E2E 不 bootstrap skill_dir，所以 CC 端会报
    # "Unknown skill: list-py-files"——只要 daemon 还在桥就行，bridge
    # 的具体 tool 类型不重要（"Tool Call:" 任何 tool 都算）。
    assert "Tool Call:" in md_text or "User" in md_text
    json_meta = json.loads(bridged[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert json_meta.get("session_id")

    # ── 4. HTTP 反映 traj_count 涨 ───────────────────────────────
    def _count_increased() -> bool:
        r = requests.get(f"{base_url}/api/v1/registry/dirs", timeout=3)
        r.raise_for_status()
        for d in r.json().get("dirs", []):
            if d.get("ecosystem") == "claude_code":
                return int(d.get("traj_count", 0)) > initial_traj_count
        return False
    _poll(_count_increased, timeout=20.0, desc="traj_count update")

    cc_dir = next(
        d for d in requests.get(f"{base_url}/api/v1/registry/dirs", timeout=3).json()["dirs"]
        if d["ecosystem"] == "claude_code"
    )
    assert cc_dir["path"] == str(sandbox.cc_sessions.resolve())
    assert cc_dir["traj_count"] >= 1
    assert cc_dir["label"] == "claude_code sessions"


# ════════════════════════════════════════════════════════════════════
# === TEST 2: 灰度全链路 (长测试) ===
# ════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(XSKILL_BIN is None, reason="xskill CLI not installed")
@pytest.mark.skipif(CLAUDE_BIN is None, reason="claude CLI not installed")
def test_canary_flip_promote_and_install_new_version(sandbox, xskill_daemon):
    """灰度全链路 E2E：

    bootstrap → daemon 启动 install main →
    跑多轮 claude -p（每轮 ingester 翻 main↔staging）→
    每条 traj 被打 side header → watcher 跑 LLM ux 评分员（main 低分、
    staging 高分）→ check_and_decide 自动 promote staging 为新 main →
    daemon 重新 install 把 v2 同步进 ~/.claude/skills →
    最终一次 claude -p 验证 CC 在 system prompt 里看到的是 v2 描述。
    """
    fake = sandbox.fake
    _program_fake_server(fake)

    # ── A. bootstrap：本测试 specific 操作 ───────────────────────
    # 注意：xskill_daemon fixture 先于此 setup 调用——意味着 daemon 启动
    # 时 skill_dir 是空的，没装 main 到 ~/.claude/skills。这里 bootstrap
    # 完之后我们需要重启 daemon 让它重新扫 skill_dir + install main。
    # 重启太重，改写为：bootstrap + 手动把 main 装到 CC dir + record 一
    # 条 history（模拟 daemon 启动 install）。daemon 后续翻牌跟着走。
    _bootstrap_canary_skill(sandbox.skill_dir)
    # 手动等效 daemon "startup install_all_to_claude_code(main)"：
    cc_skill_dir = sandbox.home / ".claude" / "skills" / SKILL_NAME
    cc_skill_dir.mkdir(parents=True, exist_ok=True)
    (cc_skill_dir / "SKILL.md").write_text(SKILL_V1_BODY, encoding="utf-8")
    # 起始 install_history 记一条 main——daemon 的 CCSessionIngester 靠这条
    # 反查 session 启动时的 side。路径必须是 daemon 实际读写的
    # XSKILL_HOME/install_history.jsonl（server.py: XSKILL_HOME /
    # "install_history.jsonl"），即 sandbox.xhome 下，不是 skill_dir 下。
    import time as _time
    history_path = sandbox.xhome / "install_history.jsonl"
    history_path.write_text(
        json.dumps({"t": _time.time() - 1, "skill": SKILL_NAME,
                    "side": "main", "sha": "", "target": "claude_code"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # ── B. 混合驱动 with/without skill session ─────────────────
    # 交错跑 with/without 让 daemon 见到的 session 流是混合的：
    #   user_skill1, no_skill1, user_skill2, user_skill3, no_skill2, ...
    # 这样验证两条：(1) 只有 with_skill 触发翻牌、(2) without 透明跳过。
    SESSION_GAP_SEC = 2.6  # > watcher poll_interval=2，留 0.6 余量
    interleaved: list[tuple[str, bool]] = []  # (prompt, expected_used)
    n_iter = iter(WITHOUT_SKILL_PROMPTS)
    # 简单交错：每 4 个 with 插 1 个 without
    for i, p in enumerate(WITH_SKILL_PROMPTS):
        interleaved.append((p, True))
        if i in (3, 7, 11):  # 3 个 without 插点
            try:
                interleaved.append((next(n_iter), False))
            except StopIteration:
                pass

    for prompt, _expect_used in interleaved:
        _run_claude_once(sandbox=sandbox, fake_base_url=fake.base_url,
                         prompt=prompt, timeout=30)
        time.sleep(SESSION_GAP_SEC)

    expected_n_total = len(interleaved)
    expected_n_used = sum(1 for _, u in interleaved if u)
    expected_n_unused = expected_n_total - expected_n_used

    # ── C. 等所有 traj 桥过来 ────────────────────────────────────
    def _all_bridged() -> bool:
        return len(list(sandbox.cc_sessions.glob("traj_*.md"))) >= expected_n_total
    _poll(_all_bridged, timeout=40.0,
          desc=f"all {expected_n_total} sessions bridged to cc_sessions")

    # ── D. 验证 header 分布：used_skill session 有 header，
    #       without 没 header ─────────────────────────────────────
    bridged = sorted(sandbox.cc_sessions.glob("traj_*.md"))
    with_header = 0
    no_header = 0
    side_counts = {"main": 0, "staging": 0}
    for traj in bridged:
        text = traj.read_text(encoding="utf-8")
        m = re.search(r"<!--\s*xskill:skill=(\S+)\s+side=(\S+)\s+sha=(\S*)\s*-->", text)
        if m:
            with_header += 1
            side_counts[m.group(2)] = side_counts.get(m.group(2), 0) + 1
        else:
            no_header += 1

    # 用 daemon 状态来精确断言：read session_assignments.jsonl
    assignments_path = sandbox.skill_dir / "session_assignments.jsonl"
    assignments_records = []
    if assignments_path.is_file():
        for line in assignments_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                assignments_records.append(json.loads(line))

    used_count = sum(1 for r in assignments_records if r.get("used_skill"))
    unused_count = sum(1 for r in assignments_records if not r.get("used_skill"))

    assert used_count == expected_n_used, (
        f"used_skill session count mismatch: assignments={used_count} "
        f"expected={expected_n_used}\nrecords:\n"
        + "\n".join(repr(r) for r in assignments_records)
    )
    assert unused_count == expected_n_unused, (
        f"unused session count mismatch: {unused_count} vs expected {expected_n_unused}"
    )

    # 同时核对：with_header 数 == used_count（即 daemon 给所有用了 skill
    # 的 session 都打了 header，没用 skill 的全跳过）
    assert with_header == used_count, (
        f"header count != used_skill count: header={with_header} "
        f"used={used_count}; side_counts={side_counts}"
    )
    assert no_header == unused_count, (
        f"no_header count != unused count: no_header={no_header} "
        f"unused={unused_count}"
    )

    # 每边样本数 ≥ CANARY_MIN_SAMPLES（canary 才会 decide）
    assert side_counts["main"] >= CANARY_MIN_SAMPLES, side_counts
    assert side_counts["staging"] >= CANARY_MIN_SAMPLES, side_counts

    # session_assignments 表里每条 used_skill session 都有合法 side 字段
    for r in assignments_records:
        assert r.get("side") in ("main", "staging"), r
        assert isinstance(r.get("sid"), str) and r["sid"], r
        assert isinstance(r.get("t"), (int, float)), r

    # ── E. 等 ux 评分累积 + check_and_decide 触发 promote ───────
    skill_md_path = sandbox.skill_dir / SKILL_NAME / "SKILL.md"

    def _promoted() -> bool:
        try:
            cur = skill_md_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        return "v2" in cur

    _poll(_promoted, timeout=180.0,
          desc="canary controller promote staging→main on skill_dir side")

    # ── F. promote 后再跑一次 claude -p 让 ingester 把新 main 装到
    #       ~/.claude/skills/。最后这条 session 也是 with-skill。──────
    _run_claude_once(sandbox=sandbox, fake_base_url=fake.base_url,
                     prompt=FINAL_VERIFY_PROMPT, timeout=30)
    time.sleep(SESSION_GAP_SEC + 1)

    def _cc_skill_is_v2() -> bool:
        try:
            return "v2" in sandbox.cc_installed_skill.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
    _poll(_cc_skill_is_v2, timeout=30.0,
          desc="~/.claude/skills/list-py-files/SKILL.md updated to v2")

    final_text = sandbox.cc_installed_skill.read_text(encoding="utf-8")
    assert "v2" in final_text
    assert "递归友好" in final_text  # v2 标志短语

    # ── G. 验证 fake server 的最新 claude 调用看到了 v2 description ──
    # CC 启动时把 ~/.claude/skills/<name>/SKILL.md description 注入
    # "following skills are available" 段落。promote 之后这一段必含 [v2]。
    anth_reqs = fake.requests_for("/v1/messages")
    found_v2_in_system = False
    for r in reversed(anth_reqs[-10:]):  # 最后一轮的请求数量
        blob = json.dumps(r["body"], ensure_ascii=False)
        if "[v2]" in blob or "递归友好" in blob:
            found_v2_in_system = True
            break
    assert found_v2_in_system, (
        "did not see v2 skill content in the most recent claude -p call; "
        "CC may not have picked up the updated install."
    )
