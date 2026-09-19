#!/usr/bin/env python3
"""ALFWorld 单局多轮 rollout —— 驱动 claude CLI 多轮会话解一局 ALFWorld（TextWorld ReAct）。

本文件是 bench 自己的副本，评测时挂进 `alfworld-eval` 镜像，覆盖镜像里写死
Skill 工具的那份。SkillOpt 必须走 Read+Bash 并读工作区 SKILL.md；xskill 才用 Skill 工具。

目的（why）
==========
镜像 SpreadsheetBench 版 multi_turn_rollout.py 的 claude CLI 多轮框架，但把任务域从
"写 solution.py→执行→对比答案"换成 ALFWorld 的"每步出 <action>→env 走一步→喂回观测"。

  turn0  claude 读 task + 初始观测（含可用动作）→ 按 --skill-mode 用技能 → 出 <think>..<action>..
  step   保留完整回复给 EnvManager 解析；另取 <action> 仅用于日志 → 得到新观测/reward/done
  续会话 claude 用 `--resume <session_id>` 续上同一会话，喂回 env 反馈
  循环   直到 env done（won 则成功） 或 到 max_turns/max_steps

claude CLI 的多轮会话会被它自己写到 `$CLAUDE_CONFIG_DIR/projects/*.jsonl`，
xskill daemon/connect client 监听该目录即可把整段多轮对话入库、蒸馏成技能。

自包含约束
==========
- 只 import SkillOpt 的 alfworld env 入口（build_alfworld_env，rollout 母本已确认可复用）。
  其余（claude CLI cmd 构造 + env 隔离 + session_id 解析）本文件本地实现。
- fail-loud 但不崩循环：claude 没输出 / 没 <action> / session_id 解析失败 / env 异常——
  都记录到 result，不静默吞错、也不让整脚本 crash。
- 与 SpreadsheetBench 版一致：turn0 用 --output-format json 拿 session_id，turn>0 --resume。

签名（被 entrypoint run_item_team 调用）
=====================================
  python alfworld_rollout.py --item-id <id> --data-root <split_root> --chome <home/.claude>
    --claude-path <claude> --model <model> --max-turns <N> --max-steps <M>
    --alfworld-data-root <ALFWORLD_DATA> --workspace <ws> --out <result.json> --timeout <s>

--data-root 指向含 train/val/test/items.json 的 split 根（与 SpreadsheetBench 的 --data-root
约定平行，但这里是 alfworld split 而非 dataset.json）。item-id 形如 "train:0003"。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


# ════════════════════════════════════════════════════════════════════════════
# 1) 数据定位：从 split 根读 items.json，按 id 找 gamefile，展开为绝对路径
# ════════════════════════════════════════════════════════════════════════════

def load_items(data_root: str) -> list[dict]:
    """读 <data-root>/{train,val,test}/items.json 合并成一个 list。fail-loud。"""
    items: list[dict] = []
    found = False
    for sp in ("train", "val", "test"):
        p = os.path.join(data_root, sp, "items.json")
        if os.path.isfile(p):
            found = True
            with open(p, encoding="utf-8") as f:
                items.extend(json.load(f))
    if not found:
        raise FileNotFoundError(
            f"no items.json under {data_root}/(train|val|test) — split root not found"
        )
    return items


def find_item(items: list[dict], item_id: str) -> dict:
    want = str(item_id)
    for it in items:
        if str(it.get("id")) == want:
            return it
    raise KeyError(f"item id={item_id!r} not found in split items.json")


def resolve_gamefile(item: dict, alfworld_data_root: str) -> str:
    """把 item 的相对 gamefile 展开为 $ALFWORLD_DATA 下绝对路径。fail-loud。"""
    gf = str(item.get("gamefile") or "")
    if not gf:
        raise KeyError(f"item {item.get('id')!r} has no gamefile")
    if not os.path.isabs(gf):
        gf = os.path.join(alfworld_data_root, gf)
    if not os.path.isfile(gf):
        raise FileNotFoundError(
            f"gamefile not found: {gf} (ALFWORLD_DATA={alfworld_data_root} not unpacked?)"
        )
    return gf


# ════════════════════════════════════════════════════════════════════════════
# 2) prompt 构造
# ════════════════════════════════════════════════════════════════════════════

# ReAct 格式硬约束：projection 要求回复同时含 <think>..</think> 与 <action>..</action>，
# 且不含中文（否则该步动作被判 invalid）。每轮 prompt 都重申一次格式与可用动作。
_FORMAT_RULES = (
    "You are an expert agent operating in the ALFRED Embodied (ALFWorld TextWorld) "
    "environment. Solve the household task step by step.\n\n"
    "STRICT OUTPUT FORMAT (every reply MUST follow this exactly, or the step is wasted):\n"
    "  - Reply with ONE reasoning block then ONE action, in English only (no Chinese characters).\n"
    "  - Wrap reasoning in <think>...</think> and the single chosen action in <action>...</action>.\n"
    "  - The action MUST be one of the admissible commands shown in the observation, copied verbatim.\n"
    "  - Output exactly one <action> per reply — issue only the next single step, not a plan.\n\n"
    "Example reply:\n"
    "<think>The task is to put a mug on the desk. I should first look around to find a mug.</think>"
    "<action>go to desk 1</action>\n"
)


def build_turn0_prompt(task_text: str, obs_text: str, *, skill_mode: str = "native") -> str:
    """turn0：格式约束 + 技能用法 + 初始观测（含可用动作）。

    native：xskill，走 Skill 工具。exec：SkillOpt，只读工作区里的 SKILL.md。
    """
    del task_text
    if skill_mode == "exec":
        skill_line = (
            "Read `SKILL.md` in the workspace if it is present, then follow its strategy. "
            "You have Read and Bash only (no Skill, Write, or Edit)."
        )
    else:
        skill_line = (
            "First, use the Skill tool to find and invoke any available skill whose "
            "description matches this ALFWorld household task, then follow its strategy."
        )
    return (
        f"{_FORMAT_RULES}\n"
        "# Task setup\n"
        f"{skill_line}\n\n"
        f"# Initial observation\n{obs_text}\n\n"
        "Now produce your <think> and the next single <action> (one admissible command)."
    )


OUTCOME_MARKER = "[XSKILL_ENV_OUTCOME_JSON]"


def build_step_prompt(
    obs_text: str,
    step_idx: int,
    last_action: str,
    *,
    reward: float | None = None,
    done: bool | None = None,
    won: bool | None = None,
) -> str:
    """后续 turn：把环境反馈及其结构化结果一起喂回模型。

    ``obs_text`` 只描述动作级反馈，例如“成功移动了 pan”，不能代表整个目标完成。
    reward/done/won 是环境给出的权威状态，必须显式传给模型，防止模型仅凭自然语言
    自行宣布 task complete。
    """
    outcome = {
        "reward": reward,
        "done": None if done is None else bool(done),
        "won": None if won is None else bool(won),
    }
    return (
        f"# Environment feedback after your action '{last_action}' (step {step_idx})\n"
        f"{obs_text}\n\n"
        "# Authoritative environment result for the previous action\n"
        f"{json.dumps(outcome, ensure_ascii=False)}\n"
        "These fields are authoritative. Do not claim that the task is complete unless "
        "won=true. If done=false and won=false, continue solving the task using one "
        "admissible action.\n\n"
        "Continue. Output your <think> and the next single <action> (one admissible command). "
        "English only; pick an action verbatim from the admissible commands above."
    )


def annotate_session_outcome(
    chome: str,
    session_id: str,
    outcome: dict,
) -> tuple[bool, str]:
    """把最终环境结果写进现有 Claude Code 会话末条 assistant 文本。

    XSkill team client 只上传 ``.claude/projects/**/*.jsonl``，不会上传独立的
    ``mtres_*.json``。因此在会话结束且不再 resume 后，以 wrapper annotation
    形式把结构化 outcome 追加到最后一条 assistant 文本，使最终 success/won/
    fail_reason 与原轨迹进入同一个 AtomTask。使用原子替换，避免 collector 读到
    半写文件。
    """
    if not session_id:
        return False, "missing session_id"

    project_root = Path(chome) / "projects"
    matches = list(project_root.glob(f"**/{session_id}.jsonl"))
    if not matches:
        return False, f"session jsonl not found under {project_root}"

    session_path = matches[0]
    try:
        records = [
            json.loads(line)
            for line in session_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"cannot read session jsonl: {exc}"

    annotation = (
        "\n\n"
        f"{OUTCOME_MARKER} "
        f"{json.dumps(outcome, ensure_ascii=False, sort_keys=True)}\n"
        "[This is an authoritative wrapper annotation, not a model claim.]"
    )
    annotated = False
    for record in reversed(records):
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in reversed(content):
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                if OUTCOME_MARKER not in block["text"]:
                    block["text"] += annotation
                annotated = True
                break
        if annotated:
            break

    if not annotated:
        return False, "no assistant text block found"

    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=session_path.parent,
            prefix=f".{session_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            for record in records:
                tmp.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                tmp.write("\n")
        os.replace(tmp_name, session_path)
    except OSError as exc:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False, f"cannot annotate session jsonl: {exc}"
    return True, str(session_path)


# ════════════════════════════════════════════════════════════════════════════
# 3) claude CLI 调用 —— 复制自 SpreadsheetBench multi_turn_rollout.call_claude
# ════════════════════════════════════════════════════════════════════════════

# xskill native 必须含 Skill，否则 Claude Code 会把 skill_listing 剥掉。
# SkillOpt exec 只能是 Read,Bash，技能是工作区里的 SKILL.md。
_DEFAULT_TOOLS = "Read,Bash,Skill"


def call_claude(
    *,
    claude_path: str,
    work_dir: str,
    chome: str,
    model: str,
    prompt: str,
    timeout: int,
    output_format: str,
    resume_session_id: str | None = None,
    tools: str = _DEFAULT_TOOLS,
) -> tuple[str, str, int]:
    """调一次 claude CLI。返回 (stdout, raw含stderr, returncode)。fail-loud 不吞错。"""
    cmd = [
        claude_path,
        "-p",
        "--output-format", output_format,
        "--permission-mode", "bypassPermissions",
        "--add-dir", work_dir,
        "--tools", tools,
        "--allowedTools", tools,
        "--setting-sources", "user,project",
    ]
    if model:
        cmd += ["--model", model]
    if resume_session_id:
        cmd += ["--resume", resume_session_id]
    cmd += ["--", prompt]

    run_env = dict(os.environ)
    run_env["CLAUDE_CONFIG_DIR"] = chome

    try:
        proc = subprocess.run(
            cmd, cwd=work_dir, capture_output=True, text=True,
            timeout=timeout, env=run_env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        raw = stdout
        if stderr:
            raw = f"{raw}\n[stderr]\n{stderr}" if raw else stderr
        return "", (raw or f"timeout after {timeout}s"), 124

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    raw = stdout
    if stderr:
        raw = f"{raw}\n[stderr]\n{stderr}" if raw else stderr
    return stdout, raw, proc.returncode


def parse_session_id(stdout: str) -> str:
    """从 `--output-format json` 的 stdout 解析 session_id。解析不到返回空串。"""
    s = (stdout or "").strip()
    if not s:
        return ""
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            sid = obj.get("session_id") or obj.get("sessionId") or ""
            if sid:
                return str(sid)
    except (json.JSONDecodeError, ValueError):
        pass
    for line in s.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            sid = obj.get("session_id") or obj.get("sessionId") or ""
            if sid:
                return str(sid)
    return ""


def parse_result_text(stdout: str, output_format: str) -> str:
    """从 claude 回复里取出可解析 <action> 的正文。

    --output-format json：取 obj['result']（claude -p json 把最终文本放这里）。
    --output-format text：stdout 即正文。
    解析不到 result 字段就退回整段 stdout（projection 仍能从中 find <action>）。
    """
    s = (stdout or "")
    if output_format != "json":
        return s
    try:
        obj = json.loads(s.strip())
        if isinstance(obj, dict):
            r = obj.get("result")
            if isinstance(r, str) and r:
                return r
    except (json.JSONDecodeError, ValueError):
        pass
    return s


def extract_action(text: str) -> str | None:
    """从 claude 回复解析 <action>...</action>（对照 alfworld_projection 的取法）。"""
    m = re.search(r"<action>(.*?)</action>", text, re.DOTALL)
    return m.group(1).strip() if m else None


_LOOK_FALLBACK_RESPONSE = (
    "<think>The model response did not contain a valid action tag, so use the safe "
    "fallback action.</think><action>look</action>"
)


def prepare_env_step_response(reply: str) -> tuple[str, str, bool]:
    """返回 ``(日志动作, EnvManager 输入, 是否使用兜底)``。

    ``AlfWorldEnvironmentManager.step`` 自己会调用 ``alfworld_projection``，因此它的
    输入契约是包含 ``<think>``/``<action>`` 的完整模型回复，而不是已经提取出的纯
    动作。提前提取后再传给 EnvManager 会让 projection 看不到标签，并错误触发
    ``[-30:]`` 兜底，破坏所有超过 30 字符的合法动作。

    纯动作仍返回给调用方，用于训练结果日志和下一轮 prompt。只有回复缺少 action
    标签时，才构造一个格式完整的 ``look`` 回复，保持原有安全兜底语义。
    """
    model_response = reply or ""
    action = extract_action(model_response)
    if action is None:
        return "look", _LOOK_FALLBACK_RESPONSE, True
    return action, model_response, False


# ════════════════════════════════════════════════════════════════════════════
# 4) 主流程：单局多轮 ReAct rollout
# ════════════════════════════════════════════════════════════════════════════

def _obs_task_desc(obs, infos, idx: int = 0) -> tuple[str, str, str]:
    """从 reset 后的 obs/infos 取 (text_obs_含动作, anchor_raw, task_description)。"""
    text_obs = obs["text"][idx]
    anchor = obs["anchor"][idx] if "anchor" in obs else text_obs
    task_desc = ""
    marker = "Your task is to: "
    pos = anchor.find(marker)
    if pos != -1:
        task_desc = anchor[pos + len(marker):].strip().splitlines()[0].strip()
    gamefile = ""
    if isinstance(infos[idx], dict):
        gamefile = infos[idx].get("extra.gamefile", "") or infos[idx].get("gamefile", "")
    return text_obs, task_desc, gamefile


def run_alfworld_episode(args) -> dict:
    """对单个 ALFWorld game 跑多轮 ReAct rollout，返回 result dict。"""
    result = {
        "item_id": str(args.item_id),
        "success": False,
        "won": False,
        "turns_used": 0,
        "steps_used": 0,
        "session_id": "",
        "per_turn": [],
        "fail_reason": "",
        "task_type": "",
        "gamefile": "",
        "task_description": "",
    }

    # ── setup：定位 item / gamefile / 建 env ───────────────────────────────
    items = load_items(args.data_root)
    item = find_item(items, args.item_id)
    result["task_type"] = item.get("task_type", "")
    gamefile = resolve_gamefile(item, args.alfworld_data_root)
    result["gamefile"] = gamefile

    os.makedirs(args.workspace, exist_ok=True)

    # 延迟 import（重依赖 textworld/alfworld）；放这里让 setup 报错也能写进 result。
    from skillopt.envs.alfworld.rollout import build_alfworld_env

    # env_num=1：单局。specific_gamefiles 锁定到这一题。is_train 无影响（gamefile 已指定）。
    env_manager = build_alfworld_env(
        env_num=1, seed=42, is_train=False, specific_gamefiles=[gamefile],
    )

    obs, infos = env_manager.reset({})
    obs_text, task_desc, gf2 = _obs_task_desc(obs, infos, 0)
    result["task_description"] = task_desc
    if gf2:
        result["gamefile"] = gf2

    session_id = ""
    done = False
    won = False
    step_idx = 0
    last_action = ""
    last_reward: float | None = None
    last_done: bool | None = None
    last_won: bool | None = None

    # ── 多轮 ReAct 循环：每 turn 一个 claude 调用 = 一个 env step ────────────
    for turn in range(args.max_turns):
        if step_idx >= args.max_steps:
            if not result["fail_reason"]:
                result["fail_reason"] = f"max_steps {args.max_steps} reached"
            break

        per = {"turn": turn, "step": step_idx, "claude_ok": False,
               "action": None, "reward": None, "done": False, "note": ""}

        # a) 组装本轮 prompt
        if turn == 0:
            prompt = build_turn0_prompt(
                task_desc or "(see observation)",
                obs_text,
                skill_mode=args.skill_mode,
            )
        else:
            prompt = build_step_prompt(
                obs_text,
                step_idx,
                last_action,
                reward=last_reward,
                done=last_done,
                won=last_won,
            )

        # b) 调 claude。turn0 用 json 拿 session_id；turn>0 用 --resume 续会话。
        if turn == 0:
            stdout, raw, rc = call_claude(
                claude_path=args.claude_path, work_dir=args.workspace,
                chome=args.chome, model=args.model, prompt=prompt,
                timeout=args.timeout, output_format="json",
                tools=args.tools,
            )
            session_id = parse_session_id(stdout)
            result["session_id"] = session_id
            reply = parse_result_text(stdout, "json")
            if not session_id:
                per["note"] = "session_id parse failed (cannot --resume in later turns)"
        else:
            if not session_id:
                per["note"] = "no session_id; cannot resume — aborting"
                result["per_turn"].append(per)
                if not result["fail_reason"]:
                    result["fail_reason"] = "no-session-id-to-resume"
                break
            stdout, raw, rc = call_claude(
                claude_path=args.claude_path, work_dir=args.workspace,
                chome=args.chome, model=args.model, prompt=prompt,
                timeout=args.timeout, output_format="text",
                resume_session_id=session_id,
                tools=args.tools,
            )
            reply = parse_result_text(stdout, "text")

        per["returncode"] = rc
        per["claude_ok"] = bool((reply or "").strip()) and rc == 0

        # c) 纯 action 仅用于日志和下一轮 prompt；EnvManager 必须收到含标签的完整回复，
        #    由它自己的 projection 做唯一一次环境动作解析。解析不到 → 用格式完整的
        #    'look' 回复兜底（对照官方 rollout 的 "missing action tag -> look" 行为）。
        action, env_step_response, used_fallback = prepare_env_step_response(reply or "")
        if used_fallback:
            per["note"] = (per["note"] + "; " if per["note"] else "") + \
                f"no <action> parsed (rc={rc}); fallback 'look'"
        per["action"] = action
        last_action = action

        # d) env 走一步
        try:
            obs, rewards, dones, infos = env_manager.step([env_step_response])
        except Exception as e:  # noqa: BLE001
            per["note"] = (per["note"] + "; " if per["note"] else "") + f"env.step error: {e}"
            result["per_turn"].append(per)
            if not result["fail_reason"]:
                result["fail_reason"] = f"env-step-error: {e}"
            break

        step_idx += 1
        obs_text = obs["text"][0]
        reward = float(rewards[0])
        done = bool(dones[0])
        step_won = (
            bool(infos[0].get("won", False))
            if isinstance(infos[0], dict)
            else False
        )
        per["reward"] = reward
        per["done"] = done
        per["won"] = step_won
        result["per_turn"].append(per)
        result["turns_used"] = turn + 1
        result["steps_used"] = step_idx
        last_reward = reward
        last_done = done
        last_won = step_won

        if done:
            won = step_won
            result["won"] = won
            result["success"] = won
            if not won and not result["fail_reason"]:
                result["fail_reason"] = "episode ended without completing task"
            break

    if not done and not result["fail_reason"]:
        result["fail_reason"] = f"not done after {result['turns_used']} turns / {step_idx} steps"

    result["done"] = done
    result["last_reward"] = last_reward

    # 关 env（best-effort，避免 textworld 子进程泄漏）
    try:
        if hasattr(env_manager, "envs") and hasattr(env_manager.envs, "close"):
            env_manager.envs.close()
    except Exception:  # noqa: BLE001
        pass

    outcome = {
        "item_id": result["item_id"],
        "task_description": result.get("task_description", ""),
        "reward": last_reward,
        "done": done,
        "won": result["won"],
        "success": result["success"],
        "turns_used": result["turns_used"],
        "steps_used": result["steps_used"],
        "fail_reason": result["fail_reason"],
    }
    annotation_ok, annotation_detail = annotate_session_outcome(
        args.chome, session_id, outcome
    )
    result["outcome_annotation_written"] = annotation_ok
    if not annotation_ok:
        result["outcome_annotation_error"] = annotation_detail

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="单局 ALFWorld 多轮 ReAct rollout（驱动 claude CLI）")
    parser.add_argument("--item-id", required=True, help="split items.json 里的题目 id（如 train:0003）")
    parser.add_argument("--data-root", required=True, help="含 train/val/test/items.json 的 split 根")
    parser.add_argument("--alfworld-data-root", default=os.environ.get("ALFWORLD_DATA", "/alfworld_data"),
                        help="$ALFWORLD_DATA：含 json_2.1.1/ 的游戏数据根")
    parser.add_argument("--chome", required=True, help="CLAUDE_CONFIG_DIR（隔离的 claude config 目录）")
    parser.add_argument("--claude-path", default="claude", help="claude CLI 可执行文件路径")
    parser.add_argument("--model", default="deepseek-v4-flash", help="模型名")
    parser.add_argument("--max-turns", type=int, default=30, help="最大 claude 轮数（= 最大 env step 数上限）")
    parser.add_argument("--max-steps", type=int, default=50, help="最大 env step 数")
    parser.add_argument("--workspace", default=None, help="工作目录（默认 /tmp/alf_<item-id>）")
    parser.add_argument("--out", default=None, help="result json 落盘路径（可选）")
    parser.add_argument("--timeout", type=int, default=300, help="每轮 claude 调用的超时秒数")
    parser.add_argument(
        "--tools",
        default=_DEFAULT_TOOLS,
        help="claude --tools/--allowedTools，SkillOpt exec 必须是 Read,Bash",
    )
    parser.add_argument(
        "--skill-mode",
        choices=("native", "exec"),
        default="native",
        help="native=Skill 工具；exec=读工作区 SKILL.md",
    )
    args = parser.parse_args()
    if args.skill_mode == "exec" and "Skill" in {part.strip() for part in args.tools.split(",")}:
        raise SystemExit("skill-mode=exec cannot include the Skill tool")

    if not args.workspace:
        args.workspace = f"/tmp/alf_{str(args.item_id).replace(':', '_')}"

    try:
        result = run_alfworld_episode(args)
    except Exception as e:  # noqa: BLE001
        result = {
            "item_id": str(args.item_id),
            "success": False, "won": False, "turns_used": 0, "steps_used": 0,
            "session_id": "", "per_turn": [],
            "fail_reason": f"setup-error: {type(e).__name__}: {e}",
            "error": traceback.format_exc(),
        }

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    print(
        f"[alf-rollout] id={result['item_id']} "
        f"success={result['success']} won={result.get('won')} "
        f"turns={result['turns_used']} steps={result.get('steps_used')} "
        f"session={result.get('session_id') or '-'} "
        f"reason={result.get('fail_reason') or 'ok'}"
    )
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
