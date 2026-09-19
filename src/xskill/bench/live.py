"""Live item runner: real Claude + official scorers. Not the fake executor."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from xskill.bench.constants import SKILLOPT_TOOLS, XSKILL_TOOLS
from xskill.bench.dataset import (
    alfworld_eval_image,
    alfworld_rollout_py,
    claude_bin,
    load_officeqa_item,
    load_spreadsheet_item,
    officeqa_docs_root,
    officeqa_gold,
    officeqa_split_root,
    skillopt_vendor_root,
    spreadsheet_data_root,
    spreadsheet_rollout_py,
)
from xskill.bench import proxy
from xskill.bench.executor import ItemOutcome
from xskill.bench.hparams import shared_eval
from xskill.bench.scorer import score_item
from xskill.bench.usage import claude_custom_headers_value, write_usage_sidecar

_ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.IGNORECASE | re.DOTALL)
_MAX_TASK_MD = 120_000


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
# 镜像 rollout 的 --timeout 是每轮 Claude，不是单题墙上时钟。单题预算在 docker 外层。
ALFWORLD_CLAUDE_TURN_TIMEOUT_S = 240


def _ensure_skillopt_on_path() -> None:
    root = skillopt_vendor_root()
    if root.is_dir() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


def _proxy_url_and_key(
    explicit_url: str | None, explicit_key: str | None = None
) -> tuple[str, str]:
    """做题走代理。key 优先用这趟的 run 虚拟 key，其次管理钥匙。"""
    url = proxy.proxy_base_url(explicit_url)
    key = explicit_key or proxy.master_key() or os.environ.get("ANTHROPIC_AUTH_TOKEN") or ""
    return url, key


def _tools_csv(algo: str, tools: list[str] | None) -> str:
    requested = list(tools) if tools else list(SKILLOPT_TOOLS if algo == "skillopt" else XSKILL_TOOLS)
    if algo == "skillopt" and "Skill" in requested:
        raise RuntimeError("skillopt live tools must not include Skill")
    if {"Write", "Edit"}.intersection(requested):
        raise RuntimeError(f"live tools must not include Write/Edit: {requested}")
    locked = list(SKILLOPT_TOOLS if algo == "skillopt" else XSKILL_TOOLS)
    return ",".join(locked)


def alfworld_skill_mode(algo: str) -> str:
    return "exec" if algo == "skillopt" else "native"


def prepare_alfworld_workspace(algo: str, work: Path, skill_file: Path | None) -> Path | None:
    """SkillOpt 把训练出来的技能当成工作区文档；xskill 走 chome 里的 Skill 包。"""
    if algo != "skillopt" or skill_file is None:
        return None
    dest = Path(work) / "SKILL.md"
    shutil.copy2(skill_file, dest)
    return dest


def build_alfworld_docker_cmd(
    *,
    uid: str,
    work: Path,
    chome: Path,
    model: str,
    timeout_s: float,
    max_tool_turns: int,
    max_steps: int,
    tools: str,
    skill_mode: str,
    litellm_url: str,
    api_key: str,
    run_id: str,
    rollout_py: Path | None = None,
    image: str | None = None,
) -> list[str]:
    script = Path(rollout_py or alfworld_rollout_py()).resolve()
    turn_timeout = int(min(float(timeout_s), ALFWORLD_CLAUDE_TURN_TIMEOUT_S))
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "-e",
        f"ANTHROPIC_BASE_URL={litellm_url}",
        "-e",
        f"ANTHROPIC_AUTH_TOKEN={api_key}",
        "-e",
        f"ANTHROPIC_API_KEY={api_key}",
        "-e",
        f"ANTHROPIC_CUSTOM_HEADERS={claude_custom_headers_value(run_id, uid)}",
        "-e",
        "CLAUDE_CONFIG_DIR=/tmp/chome",
        "-v",
        f"{chome}:/tmp/chome",
        "-v",
        f"{work}:/tmp/ws",
        "-v",
        f"{script}:/app/alfworld_rollout.py:ro",
        "--entrypoint",
        "python",
        image or alfworld_eval_image(),
        "/app/alfworld_rollout.py",
        "--item-id",
        uid,
        "--data-root",
        "/bench/data/alfworld_path_split",
        "--alfworld-data-root",
        "/alfworld_data",
        "--chome",
        "/tmp/chome",
        "--workspace",
        "/tmp/ws",
        "--claude-path",
        "claude",
        "--model",
        model,
        "--out",
        "/tmp/ws/alf_result.json",
        "--timeout",
        str(turn_timeout),
        "--max-turns",
        str(int(max_tool_turns)),
        # 单局最大 env step 抄 SkillOpt 官方 configs/alfworld/default.yaml。
        "--max-steps",
        str(int(max_steps)),
        "--tools",
        tools,
        "--skill-mode",
        skill_mode,
    ]


def _extract_answer(text: str) -> str:
    matches = _ANSWER_RE.findall(text or "")
    return matches[-1].strip() if matches else ""


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = None
    if isinstance(loaded, list):
        return [str(item).strip() for item in loaded if str(item).strip()]
    return [text]


def _parse_claude_json(stdout: str) -> tuple[str, dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        return "", {}
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text, {}
    if not isinstance(obj, dict):
        return text, {}
    result = obj.get("result")
    return (result if isinstance(result, str) else text), obj


def _usage_blob(obj: dict[str, Any]) -> dict[str, Any]:
    usage = obj.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return {
        "id": obj.get("session_id") or obj.get("sessionId") or obj.get("request_id"),
        "model": obj.get("model"),
        "usage": {
            "input_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
            "cache_read_input_tokens": (
                usage.get("cache_read_input_tokens")
                or usage.get("cache_read_tokens")
                or usage.get("cache_read")
            ),
        },
    }


def _write_settings(chome: Path) -> None:
    chome.mkdir(parents=True, exist_ok=True)
    (chome / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {
                    "defaultMode": "bypassPermissions",
                    "allow": ["Read(*)", "Bash(*)", "Skill(*)"],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _install_xskill_skills(chome: Path, skill_dir: Path) -> None:
    dest = chome / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(skill_dir)
    packages: list[Path] = []
    if (src / "SKILL.md").is_file():
        packages.append(src)
    else:
        packages.extend(
            child for child in sorted(src.iterdir()) if child.is_dir() and (child / "SKILL.md").is_file()
        )
    if not packages:
        raise FileNotFoundError(f"no SKILL.md under {skill_dir}")
    for pkg in packages:
        target = dest / pkg.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(pkg, target)


def _claude_env(*, chome: Path, run_id: str, uid: str, proxy_url: str, api_key: str) -> dict[str, str]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(chome)
    env["ANTHROPIC_BASE_URL"] = proxy_url
    if api_key:
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
        env["ANTHROPIC_API_KEY"] = api_key
    env["ANTHROPIC_CUSTOM_HEADERS"] = claude_custom_headers_value(run_id, uid)
    env["ANTHROPIC_MODEL"] = env.get("ANTHROPIC_MODEL") or ""
    return env


def _run_claude(
    *,
    claude_path: str,
    work_dir: Path,
    add_dirs: list[Path],
    chome: Path,
    model: str,
    prompt: str,
    timeout_s: float,
    tools: str,
    env: dict[str, str],
    resume_session_id: str | None = None,
) -> tuple[str, str, int, dict[str, Any]]:
    cmd = [
        claude_path,
        "-p",
        "--output-format",
        "json",
        "--permission-mode",
        "bypassPermissions",
        "--add-dir",
        str(work_dir),
    ]
    for folder in add_dirs:
        if folder.resolve() != work_dir.resolve():
            cmd.extend(["--add-dir", str(folder)])
    cmd.extend(
        [
            "--tools",
            tools,
            "--allowedTools",
            tools,
            "--setting-sources",
            "user,project",
        ]
    )
    if model:
        cmd.extend(["--model", model])
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
    cmd.extend(["--", prompt])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        raw = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
        return stdout, raw or f"timeout after {timeout_s}s", 124, {}
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    raw = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
    text, obj = _parse_claude_json(stdout)
    return text, raw, proc.returncode, obj


def _load_ss_mod():
    path = spreadsheet_rollout_py()
    spec = importlib.util.spec_from_file_location("_xskill_bench_ss_rollout", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load spreadsheet rollout from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveExecutor:
    """Host Claude for OfficeQA/Spreadsheet; ALFWorld eval image for games."""

    def __init__(
        self,
        *,
        benchmark: str,
        algo: str = "xskill",
        model: str = "deepseek-v4-flash",
        run_id: str = "",
        timeout_s: float = 600,
        max_tool_turns: int = 24,
        skill_dir: Path | None = None,
        skill_file: Path | None = None,
        logs_dir: Path | None = None,
        tools: list[str] | None = None,
        litellm_url: str | None = None,
        litellm_key: str | None = None,
        workspace_root: Path | None = None,
        gold: dict[str, Any] | None = None,
        **_ignored: Any,
    ) -> None:
        self.benchmark = benchmark
        self.algo = algo
        self.model = model
        self.run_id = run_id
        self.timeout_s = float(timeout_s)
        self.max_tool_turns = int(max_tool_turns)
        self.skill_dir = Path(skill_dir) if skill_dir else None
        self.skill_file = Path(skill_file) if skill_file else None
        self.logs_dir = Path(logs_dir) if logs_dir else None
        self.tools = _tools_csv(algo, tools)
        self.litellm_url, self.api_key = _proxy_url_and_key(litellm_url, litellm_key)
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.gold = gold or {}
        self.claude_path = claude_bin()

    def run_item(self, uid: str, index: int) -> ItemOutcome:
        del index
        started = time.perf_counter()
        work = (self.workspace_root or Path("/tmp/xskill-bench-live")) / uid.replace(":", "_")
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        chome = work / "chome"
        _write_settings(chome)
        if self.algo == "xskill" and self.skill_dir:
            _install_xskill_skills(chome, self.skill_dir)
        try:
            if self.benchmark == "officeqa":
                outcome = self._run_officeqa(uid, work, chome)
            elif self.benchmark == "spreadsheet":
                outcome = self._run_spreadsheet(uid, work, chome)
            elif self.benchmark == "alfworld":
                outcome = self._run_alfworld(uid, work, chome)
            else:
                raise RuntimeError(f"live executor has no path for {self.benchmark}")
        except subprocess.TimeoutExpired:
            outcome = ItemOutcome(
                uid=uid,
                pred_answer="",
                gold_answer=self.gold.get(uid),
                status="timeout",
                is_correct=False,
                latency_ms=0.0,
                summary_text="timeout",
            )
        outcome.latency_ms = (time.perf_counter() - started) * 1000.0
        return outcome

    def _env(self, chome: Path, uid: str) -> dict[str, str]:
        return _claude_env(
            chome=chome,
            run_id=self.run_id,
            uid=uid,
            proxy_url=self.litellm_url,
            api_key=self.api_key,
        )

    def _record_usage(self, uid: str, obj: dict[str, Any]) -> None:
        if self.logs_dir is None or not obj:
            return
        write_usage_sidecar(
            self.logs_dir,
            uid,
            _usage_blob(obj),
            run_id=self.run_id,
            model=self.model,
        )

    def _dump(self, uid: str, name: str, text: str) -> None:
        if self.logs_dir is None:
            return
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / f"{uid}.{name}").write_text(_as_text(text), encoding="utf-8")

    def _run_officeqa(self, uid: str, work: Path, chome: Path) -> ItemOutcome:
        _ensure_skillopt_on_path()
        from skillopt.envs.officeqa.tool_runtime import (  # type: ignore
            build_oracle_parsed_pages_context,
            resolve_candidate_files,
            resolve_docs_roots,
        )

        item = load_officeqa_item(uid)
        gold = self.gold.get(uid, officeqa_gold(item))
        docs_roots = resolve_docs_roots(str(officeqa_docs_root()))
        source_files = _as_list(item.get("source_files"))
        source_docs = _as_list(item.get("source_docs"))
        candidates = resolve_candidate_files(source_files, docs_roots)
        oracle = build_oracle_parsed_pages_context(
            source_files,
            source_docs,
            docs_roots,
            evidence_note="Treat it as primary document evidence and combine it with local document searches.",
        )
        task_md = _officeqa_task_md(item, docs_roots, candidates, oracle, algo=self.algo)
        (work / "task.md").write_text(task_md[:_MAX_TASK_MD], encoding="utf-8")
        if self.algo == "skillopt" and self.skill_file:
            shutil.copy2(self.skill_file, work / "SKILL.md")
        if self.algo == "skillopt":
            prompt = (
                "Read `SKILL.md` if present, then read `task.md`. "
                f"Search the local corpus by absolute path. About {self.max_tool_turns} read/search steps. "
                "End with one short answer in `<answer>ANSWER</answer>`."
            )
        else:
            prompt = (
                "Read `task.md`, use the Skill tool first for any matching OfficeQA skill, "
                "then search the local corpus by absolute path. "
                f"About {self.max_tool_turns} read/search steps. "
                "End with one short answer in `<answer>ANSWER</answer>`."
            )
        text, raw, rc, obj = _run_claude(
            claude_path=self.claude_path,
            work_dir=work,
            add_dirs=[Path(root) for root in docs_roots],
            chome=chome,
            model=self.model,
            prompt=prompt,
            timeout_s=self.timeout_s,
            tools=self.tools,
            env=self._env(chome, uid),
        )
        self._dump(uid, "raw.txt", raw)
        self._record_usage(uid, obj)
        if rc == 124:
            return ItemOutcome(uid, "", gold, "timeout", False, 0.0, "timeout")
        pred = _extract_answer(text)
        status, correct = score_item(self.benchmark, pred=pred, gold=gold)
        return ItemOutcome(uid, pred, gold, status, correct, 0.0, text[:400])

    def _run_spreadsheet(self, uid: str, work: Path, chome: Path) -> ItemOutcome:
        ss = _load_ss_mod()
        item = load_spreadsheet_item(uid)
        gold = True
        data_root = spreadsheet_data_root()
        rel = item.get("spreadsheet_path") or f"spreadsheet/{uid}"
        task_dir = Path(rel) if os.path.isabs(str(rel)) else data_root / rel
        input_src, golden_path = ss.find_input_and_golden(str(task_dir))
        input_path, output_path = ss.prepare_workspace(str(work), input_src)
        answer_position = item.get("answer_position") or ""
        answer_sheet = item.get("answer_sheet") or ""
        if answer_position and answer_sheet and "!" not in str(answer_position):
            answer_eval = f"{answer_sheet}!{answer_position}"
        else:
            answer_eval = answer_position
        if self.algo == "skillopt" and self.skill_file:
            shutil.copy2(self.skill_file, work / "SKILL.md")
        prompt = ss.build_turn0_prompt(
            item.get("instruction") or "",
            input_path,
            item.get("instruction_type") or "",
            answer_eval,
        )
        if self.algo == "skillopt":
            prompt = (
                "Read `SKILL.md` if present. You have Read and Bash only (no Write/Edit). "
                "Write `solution.py` with a Bash heredoc.\n\n" + prompt
            )
        else:
            prompt = (
                "Use the Skill tool first if a spreadsheet skill is installed. "
                "You have Read, Bash, and Skill only (no Write/Edit). "
                "Write `solution.py` with a Bash heredoc.\n\n" + prompt
            )
        text, raw, rc, obj = _run_claude(
            claude_path=self.claude_path,
            work_dir=work,
            add_dirs=[],
            chome=chome,
            model=self.model,
            prompt=prompt,
            timeout_s=self.timeout_s,
            tools=self.tools,
            env=self._env(chome, uid),
        )
        self._dump(uid, "raw.txt", raw)
        self._record_usage(uid, obj)
        if rc == 124:
            return ItemOutcome(uid, False, gold, "timeout", False, 0.0, "timeout")
        solution = work / "solution.py"
        if not solution.is_file():
            return ItemOutcome(uid, False, gold, "fail", False, 0.0, "no solution.py")
        exec_ok, exec_err = ss._exec_run_solution(str(work), int(self.timeout_s))
        if not exec_ok:
            return ItemOutcome(uid, False, gold, "fail", False, 0.0, exec_err[:400])
        graded = ss.evaluate_output(output_path, golden_path, answer_eval)
        passed = bool(graded.get("ok"))
        status, correct = score_item(self.benchmark, pred=passed, gold=gold)
        return ItemOutcome(uid, passed, gold, status, correct, 0.0, json.dumps(graded, default=str)[:400])

    def _run_alfworld(self, uid: str, work: Path, chome: Path) -> ItemOutcome:
        if self.algo == "xskill" and self.skill_dir:
            _install_xskill_skills(chome, self.skill_dir)
        prepare_alfworld_workspace(self.algo, work, self.skill_file)
        out_json = work / "alf_result.json"
        spec = shared_eval(self.benchmark)
        cmd = build_alfworld_docker_cmd(
            uid=uid,
            work=work,
            chome=chome,
            model=self.model,
            timeout_s=self.timeout_s,
            max_tool_turns=self.max_tool_turns,
            max_steps=int(spec.max_steps or self.max_tool_turns),
            tools=self.tools,
            skill_mode=alfworld_skill_mode(self.algo),
            litellm_url=self.litellm_url,
            api_key=self.api_key,
            run_id=self.run_id,
        )
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s + 60)
        except subprocess.TimeoutExpired as exc:
            self._dump(
                uid,
                "raw.txt",
                _as_text(exc.stdout) + "\n" + _as_text(exc.stderr) + "\ntimeout",
            )
            return ItemOutcome(uid, False, True, "timeout", False, 0.0, "timeout")
        raw = _as_text(proc.stdout)
        if proc.stderr:
            raw += "\n[stderr]\n" + _as_text(proc.stderr)
        self._dump(uid, "raw.txt", raw)
        self._harvest_proxy_usage(uid)
        if not out_json.is_file():
            return ItemOutcome(uid, False, True, "fail", False, 0.0, raw[-400:])
        result = json.loads(out_json.read_text(encoding="utf-8"))
        won = bool(result.get("won") or result.get("success"))
        status, correct = score_item(self.benchmark, pred=won, gold=True)
        if result.get("fail_reason") == "timeout" or "timeout" in str(result.get("fail_reason") or ""):
            status, correct = "timeout", False
        return ItemOutcome(uid, won, True, status, correct, 0.0, json.dumps(result, default=str)[:400])

    def _harvest_proxy_usage(self, uid: str) -> None:
        if self.logs_dir is None:
            return
        try:
            from xskill.bench.usage import fetch_spend_logs, spend_row_matches, usage_from_spend_rows

            # 读账本要管理钥匙，不是做题用的那把。
            rows = [
                row
                for row in fetch_spend_logs(
                    base_url=self.litellm_url, api_key=proxy.master_key()
                )
                if spend_row_matches(row, self.run_id, uid)
            ]
        except Exception:
            return
        if not rows:
            return
        usage = usage_from_spend_rows(rows, model=self.model)
        write_usage_sidecar(
            self.logs_dir,
            uid,
            {
                "id": (usage.get("request_ids") or [None])[0],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens"),
                    "completion_tokens": usage.get("output_tokens"),
                    "prompt_cache_hit_tokens": usage.get("cache_read_tokens"),
                },
            },
            run_id=self.run_id,
            model=self.model,
        )


def _officeqa_task_md(
    item: dict[str, Any],
    docs_roots: list[str],
    candidate_files: list[str],
    oracle_context: str,
    *,
    algo: str,
) -> str:
    source_files = _as_list(item.get("source_files"))
    source_docs = _as_list(item.get("source_docs"))
    abs_candidates = [os.path.realpath(path) for path in candidate_files[:30]]
    abs_roots = [os.path.realpath(root) for root in docs_roots]
    skill_line = (
        "Read `SKILL.md` first if it is in the workspace."
        if algo == "skillopt"
        else "Use the Skill tool first if any relevant skill is installed."
    )
    parts = [
        "# OfficeQA Task",
        "Answer the question using the local OfficeQA document corpus.",
        "Read and search files by their absolute paths.",
        "Do not assume a workspace-relative `docs/` or `docs/root_*` tree.",
        skill_line,
        "Return the final answer at the end as `<answer>ANSWER</answer>`.",
        "",
        "## Question",
        str(item.get("question") or ""),
    ]
    if abs_roots:
        parts.extend(["", "## Document Corpus", *[f"- {root}" for root in abs_roots]])
    if source_files:
        parts.extend(["", "## Source Files", *[f"- {value}" for value in source_files]])
    if source_docs:
        parts.extend(["", "## Source Hints", *[f"- {value}" for value in source_docs]])
    if abs_candidates:
        parts.extend(["", "## Candidate Files", *[f"- {value}" for value in abs_candidates]])
    if oracle_context.strip():
        parts.extend(["", "## Oracle Parsed Pages", oracle_context.strip()])
    parts.extend(
        [
            "",
            "## Answer Rules",
            "- Search local files by absolute path before answering.",
            "- Check units, dates, signs, fiscal years, row labels, and column labels.",
            "- Put only the final short answer inside the last `<answer>...</answer>` tag.",
        ]
    )
    return "\n".join(parts)
