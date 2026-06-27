#!/usr/bin/env python3
"""离线 description 选优冒烟 —— 全程 mock，零网络、零真 API。

给定一个目标 skill + 若干诱饵 skill 的 fixtures（缺省自动生成演示 fixtures），
跑一次**完整** description 触发选优链路并打印逐 case 触发结果：

  fixtures(诱饵=真 git main 分支) → .skill_index.pkl 现场重建(故意先删)
  → case 生成(mock LLM) → 真 agno Agent 探针(模型层换确定性脚本)
  → train/test 选优 → 写回 frontmatter → registry 落库 + 逐 case 落盘

这是后续评测场景的冒烟入口：任何人改了 trigger_probe / description_opt /
agno_factory，跑这个脚本 30 秒内能看出链路断没断。

用法：
    python3 scripts/probe_offline_smoke.py                  # 临时目录演示 fixtures
    python3 scripts/probe_offline_smoke.py --workdir /tmp/probe_smoke
    python3 scripts/probe_offline_smoke.py --skill-dir /path/to/skill-root/my-skill
        # 用现成 skill 目录（其父目录视作 skill_root，兄弟目录视作诱饵）

mock 模型的决策规则（确定性）：把 query 与每个 use_* 工具的 description 做
词袋重叠打分，重叠词 ≥2 选最高分工具，否则不调任何工具——模拟"代理按描述
挑 skill"，同 query 同结果，可复现。
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import numpy as np  # noqa: E402

# ─────────────────────────────────────────────────────────────────
# 演示 fixtures：目标 skill + 诱饵（诱饵走真 git baby→main，main_sha 非空）
# ─────────────────────────────────────────────────────────────────

_TARGET = ("log-analyzer",
           "does stuff with files")          # 故意写烂，留给选优空间
_DECOYS = [
    ("react-form-builder",
     "Build React components and frontend forms with validation"),
    ("postgres-schema-designer",
     "Design postgres database schemas, tables and migrations"),
    ("aws-deployer",
     "Deploy applications to aws cloud infrastructure with terraform"),
]

_CASES = [
    {"query": "parse the error log at /var/log/app.log", "should_trigger": True, "topic": "logs"},
    {"query": "find the error trace in my stack output", "should_trigger": True, "topic": "logs"},
    {"query": "summarize what went wrong in this log file", "should_trigger": True, "topic": "logs"},
    {"query": "grep the log for OOM kills", "should_trigger": True, "topic": "logs"},
    {"query": "analyze error log lines and report root cause", "should_trigger": True, "topic": "logs"},
    {"query": "write a react component for a login form", "should_trigger": False, "topic": "frontend"},
    {"query": "set up a postgres database schema", "should_trigger": False, "topic": "db"},
    {"query": "deploy my app to aws", "should_trigger": False, "topic": "infra"},
    {"query": "explain how merge sort works", "should_trigger": False, "topic": "algo"},
    {"query": "rename a variable across the project", "should_trigger": False, "topic": "refactor"},
]

_IMPROVED = ("Use this skill to analyze and parse log files, error logs and "
             "error traces, find root causes and summarize failures.")


class ScriptedLLM:
    """case 生成 / improve / shorten 的 mock（绝不出网）。"""

    def chat(self, prompt: str, _system: str = "") -> str:
        if "generating evaluation queries" in prompt:
            return json.dumps(_CASES)
        if "write a new and improved description" in prompt:
            return f"<new_description>{_IMPROVED}</new_description>"
        return ""


class HashEmbed:
    """确定性词袋 embedding：同文本同向量，离线可复现。"""

    DIM = 64

    def _vec(self, text: str):
        v = np.zeros(self.DIM, dtype=np.float32)
        for w in re.findall(r"[a-z]+", (text or "").lower()):
            v[hash(w) % self.DIM] += 1.0
        return v

    def encode(self, text: str):
        return self._vec(text)

    def encode_batch(self, texts):
        return np.stack([self._vec(t) for t in texts])


# ─────────────────────────────────────────────────────────────────
# mock 模型：真 agno Agent，模型层换确定性词袋决策
# ─────────────────────────────────────────────────────────────────

_STOP = {"the", "a", "an", "my", "in", "to", "for", "with", "at", "this",
         "and", "of", "use", "skill", "when", "use_"}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if w not in _STOP and len(w) > 2}


def _latest_user_query(messages) -> str:
    for m in reversed(messages or []):
        if getattr(m, "role", "") == "user":
            return getattr(m, "content", "") or ""
    return ""


def _best_tool(query: str, tools) -> str | None:
    best_name, best_score = None, 1   # 需要重叠 ≥2 才触发
    qw = _words(query)
    for t in tools or []:
        fn = (t or {}).get("function", {}) if isinstance(t, dict) else {}
        name = fn.get("name", "")
        if not name.startswith("use_"):
            continue
        score = len(qw & _words(fn.get("description", "")))
        if score > best_score:
            best_name, best_score = name, score
    return best_name


def _set_assistant_response(assistant_message, *, content=None, tool_calls=None) -> None:
    if assistant_message is None:
        return
    assistant_message.role = "assistant"
    assistant_message.content = content
    if tool_calls is not None:
        assistant_message.tool_calls = tool_calls


def _scripted_invoke_factory():
    """造 model.invoke 替身：从 messages 抠出 user query，与 tools 里各
    use_* 工具的 description 做词袋重叠，重叠 ≥2 调最高分工具。"""
    from agno.models.response import ModelResponse

    def invoke(messages=None, assistant_message=None, tools=None, **kw):  # noqa: ARG001
        best_name = _best_tool(_latest_user_query(messages), tools)
        if best_name:
            tc = [{"id": "call_1", "type": "function",
                   "function": {"name": best_name,
                                "arguments": json.dumps({"reason": "fits"})}}]
            _set_assistant_response(assistant_message, tool_calls=tc)
            return ModelResponse(role="assistant", content=None, tool_calls=tc)
        _set_assistant_response(assistant_message, content="no skill fits")
        return ModelResponse(role="assistant", content="no skill fits")

    return invoke


def make_mock_agno_factory():
    """真 make_default_factory 构造 Agent（验证注入链路），仅在最低层把
    model.invoke 换掉——agno 工具注册/执行/StopAgentRun 全是真的。"""
    from xskill.agents.agno_factory import make_default_factory
    cfg = {"llm": {"base_url": "http://127.0.0.1:9/v1", "model": "offline-stub",
                   "api_key": "sk-offline", "max_context": 200000}}
    real = make_default_factory(cfg)

    def factory(*, instructions, tools, **kwargs):
        agent = real(instructions=instructions, tools=tools, **kwargs)
        agent.model.invoke = _scripted_invoke_factory()
        return agent

    return factory


# ─────────────────────────────────────────────────────────────────
# fixtures
# ─────────────────────────────────────────────────────────────────

def _write_skill(root: Path, name: str, desc: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n"
        f"metadata:\n  version: 1\n---\n\n# {name}\n\nbody\n",
        encoding="utf-8",
    )
    return d


def build_fixtures(skill_root: Path) -> Path:
    """目标 skill（plain 目录）+ 诱饵（真 git，graduate 到 main）。"""
    from xskill.skill.git import init_skill_repo_on_baby, commit_baby_to_main_branch
    target = _write_skill(skill_root, *_TARGET)
    for name, desc in _DECOYS:
        d = skill_root / name
        init_skill_repo_on_baby(str(d), name=name, description=desc)
        _write_skill(skill_root, name, desc)
        ok = commit_baby_to_main_branch(str(d), f"v1: fixture decoy {name}")
        assert ok, f"诱饵 {name} graduate 失败"
    return target


# ─────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────

# 全脚本墙钟看门狗：要求 <60s 跑完。任何组件意外阻塞（网络/SDK/agno 内部）
# 都不许吊死冒烟——超时直接打印 FAIL + 强制退出（守护线程，正常跑完零影响）。
_WATCHDOG_SECONDS = 55.0


def _arm_watchdog() -> None:
    import os
    import threading
    import faulthandler

    def _kill():
        print(f"\nRESULT: FAIL — 冒烟超过墙钟上限 {_WATCHDOG_SECONDS:.0f}s"
              "（疑似挂死，dump 各线程栈如下）", flush=True)
        faulthandler.dump_traceback()
        os._exit(2)

    t = threading.Timer(_WATCHDOG_SECONDS, _kill)
    t.daemon = True
    t.start()


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workdir", type=Path, default=None,
                    help="工作目录（缺省临时目录）；registry.db 也落这里，绝不碰 ~/.xskill")
    ap.add_argument("--skill-dir", type=Path, default=None,
                    help="现成 skill 目录（父目录视作 skill_root）；缺省自动生成演示 fixtures")
    return ap.parse_args()


def _prepare_run(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s %(message)s")
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="probe_smoke_"))
    workdir.mkdir(parents=True, exist_ok=True)

    # registry 隔离：record_trigger_eval 落 workdir，不污染真 ~/.xskill
    import xskill.pipeline.registry as REG
    REG.get_registry_db_path = lambda: workdir / "registry.db"

    if args.skill_dir:
        target = args.skill_dir.resolve()
        skill_root = target.parent
    else:
        skill_root = workdir / "skill"
        target = build_fixtures(skill_root)
    print(f"skill_root = {skill_root}\ntarget     = {target.name}")
    return workdir, skill_root, target


def _remove_skill_index(skill_root: Path) -> None:
    # 故意删掉索引：验证"index 缺失 → 现场从 main 重建"这条必现路径
    idx = skill_root / ".skill_index.pkl"
    if idx.is_file():
        idx.unlink()
        print("已删除 .skill_index.pkl（模拟 rebuild --force 后状态）")


def _run_optimization(target: Path, skill_root: Path) -> dict:
    from xskill.skill.description_opt import optimize_description
    return optimize_description(
        target,
        llm=ScriptedLLM(),
        config={"skill_opt": {"runs_per_case": 1, "max_iters": 1, "seed": 42}},
        agno_agent_factory=make_mock_agno_factory(),
        embed_client=HashEmbed(),
        skill_root=skill_root,
    )


def _print_case_results(exp_dir: Path) -> None:
    # ── 逐 case 触发结果 ───────────────────────────────────────
    print("\n══ 逐 case 触发结果（per-case 落盘 → 原样回放）══")
    for f in sorted(exp_dir.glob("*/*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        mark = "PASS" if rec["passed"] else "FAIL"
        print(f"[{f.parent.name}/{f.stem}] {mark} "
              f"should={rec['should_trigger']} did={rec['did_trigger']} "
              f"triggered={rec['triggered_skill']} "
              f"catalog={rec['catalog']}\n    query: {rec['query']}")


def _print_optimization_result(out: dict) -> None:
    print("\n══ 选优结果 ══")
    print(f"best_description: {out['best_description']}")
    print(f"chosen_reason:    {out['chosen_reason']}")
    print(f"catalog_size:     {out['catalog_size']} "
          f"(no_competition={out['no_competition']})")
    for c in out["candidates"]:
        print(f"  iter {c['iter']}: train={c['train_score']} "
              f"test={c['test_score']} desc={c['description'][:70]!r}")


def _print_registry_rows(target: Path, *, demo_fixtures: bool) -> list:
    import xskill.pipeline.registry as REG

    rows = REG.trigger_eval_for_skill(_TARGET[0] if demo_fixtures else target.name)
    print(f"\nregistry.skill_trigger_eval rows: {len(rows)}")
    for r in rows:
        print(f"  {r}")
    return rows


def main() -> int:
    import time as _time
    t_start = _time.monotonic()
    _arm_watchdog()
    args = _parse_args()
    _workdir, skill_root, target = _prepare_run(args)
    _remove_skill_index(skill_root)

    out = _run_optimization(target, skill_root)
    exp_dir = Path(out["exp_dir"])
    _print_case_results(exp_dir)
    _print_optimization_result(out)
    rows = _print_registry_rows(target, demo_fixtures=not args.skill_dir)

    # 冒烟判定：有竞争 catalog、落库 1 条、逐 case 文件非空
    ok = (out["catalog_size"] > 0 and len(rows) == 1
          and any(exp_dir.glob("*/*.json")))
    elapsed = _time.monotonic() - t_start
    print(f"\nSMOKE {'OK' if ok else 'FAILED'}")
    print(f"RESULT: {'PASS' if ok else 'FAIL'} "
          f"(elapsed {elapsed:.1f}s, exit code {0 if ok else 1})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
