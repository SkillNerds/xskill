"""跑 SkillOpt 官方 scripts/train.py。不做整篇改写冒充官方训练。

官方训练是 ReflACT 六段循环（rollout、reflect、aggregate、select、update、
evaluate），按 epoch 和 batch 走，改写走 patch 模式，每步都要过 selection set 的
gate 才接受。这里只负责：按数据集拼出官方命令行、把请求指到 LiteLLM、跑起来、再
把官方 run 目录里的 history.json 翻译成本框架的 checkpoints。

超参不在这里写死，全部来自 hparams.SKILLOPT_OFFICIAL，也就是 vendor 的 configs。
只有 vendor 明确禁止的组合才在 cfg-options 里覆盖（spreadsheet 的 env.mode）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

from xskill.bench import proxy
from xskill.bench.dataset import (
    claude_bin,
    officeqa_docs_root,
    skillopt_vendor_root,
    split_root,
    spreadsheet_data_root,
)
from xskill.bench.hparams import effective_workers, skillopt_official
from xskill.bench.skill_io import sha256_file
from xskill.bench.stdio import safe_write

# ALFWorld 需要 alfworld 包与 TextWorld 运行栈，本机没有，只能在官方镜像里跑。
SKILLOPT_TRAIN_IMAGES = {
    "alfworld": "localhost:5000/p_user1/algo-skillopt-alf-official-full:exec-20260904",
}
HOST_BENCHMARKS = ("officeqa", "spreadsheet")
ALFWORLD_DATA_IN_IMAGE = "/alfworld_data"
VENDOR_IN_IMAGE = "/app/SkillOpt"
SKILL_INIT_IN_IMAGE = "/app/skill_init.md"


@dataclass
class OfficialTrainResult:
    frozen_sha: str
    checkpoints: list[dict[str, Any]]
    log_text: str
    action: str
    val_score: float | None
    hyperparameters: dict[str, Any]
    runtime: str
    n_steps: int
    returncode: int
    run_dir: Path


def _print(line: str, stream: TextIO | None) -> None:
    safe_write(line, stream)


def cfg_options(benchmark: str, *, docs_dir: Path | None = None) -> list[str]:
    """只放路径与 harness 相关项，训练超参一律留给官方 config。"""
    spec = skillopt_official(benchmark)
    env = spec["env"]
    if benchmark == "officeqa":
        docs = str(docs_dir or officeqa_docs_root())
        return [
            f"env.name={spec['env_name']}",
            f"env.data_dirs={docs}",
            f"env.search_mode={env['search_mode']}",
            "env.use_local_tools=true",
            f"env.max_tool_turns={env['max_tool_turns']}",
        ]
    if benchmark == "spreadsheet":
        # vendor 的适配器禁止 exec target 配 multi，这是唯一允许的覆盖。
        return [f"env.mode={env['mode']}"]
    if benchmark == "alfworld":
        return [
            f"env.name={spec['env_name']}",
            f"env.max_steps={env['max_steps']}",
        ]
    raise ValueError(f"no SkillOpt official train wiring for {benchmark}")


def build_argv(
    *,
    benchmark: str,
    model: str,
    split_dir: Path,
    out_root: Path,
    workers: int,
    vendor_root: Path | None = None,
    claude_path: str | None = None,
    proxy_url: str,
    python_bin: str = "python",
    docs_dir: Path | None = None,
    data_root: Path | None = None,
    train_size: int | None = None,
    skill_init: Path | None = None,
) -> list[str]:
    """官方命令行。optimizer 与 target 的 endpoint 都指 LiteLLM。"""
    spec = skillopt_official(benchmark)
    argv = [
        python_bin,
        "scripts/train.py",
        "--config",
        spec["config"],
        # optimizer 是算法内部的反思 LLM，官方就是 openai_chat；target 是解题
        # harness，合同锁定 claude_code_exec。
        "--optimizer_backend",
        "openai_chat",
        "--target_backend",
        "claude_code_exec",
        "--optimizer_model",
        model,
        "--target_model",
        model,
        "--claude_code_exec_path",
        claude_path or claude_bin(),
        "--claude_code_exec_use_sdk",
        "cli",
        # key 只从环境变量进（build_env 里那两条），不上命令行：命令行任何人 ps 都看得见。
        # vendor 的 azure_openai.py 认 OPTIMIZER_/TARGET_AZURE_OPENAI_API_KEY。
        "--optimizer_azure_openai_endpoint",
        proxy_url,
        "--optimizer_azure_openai_auth_mode",
        "openai_compatible",
        "--target_azure_openai_endpoint",
        proxy_url,
        "--target_azure_openai_auth_mode",
        "openai_compatible",
        "--split_dir",
        str(split_dir),
        "--split_mode",
        "split_dir",
        "--seed",
        str(spec["train"]["seed"]),
        "--workers",
        str(max(int(workers), 1)),
        "--out_root",
        str(out_root),
        "--eval_test",
        "false",
    ]
    if benchmark == "spreadsheet":
        argv.extend(["--data_root", str(data_root or spreadsheet_data_root())])
    if skill_init is not None:
        # 不传就用官方 env.skill_init（vendor 自带的 initial.md），那也是官方口径；
        # 传了就必须真从它起步，否则 run_config 里记的种子哈希是假的。
        argv.extend(["--skill_init", str(skill_init)])
    options = cfg_options(benchmark, docs_dir=docs_dir)
    if train_size is not None:
        options.append(f"train.train_size={int(train_size)}")
    argv.append("--cfg-options")
    argv.extend(options)
    del vendor_root
    return argv


def build_env(
    *,
    benchmark: str,
    proxy_url: str,
    proxy_key: str,
    run_id: str,
    docs_dir: Path | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """claude_code_exec 起的 Claude 子进程继承这些变量，所以做题也走 LiteLLM。"""
    from xskill.bench.usage import claude_custom_headers_value

    env = dict(base_env if base_env is not None else os.environ)
    env.update(
        {
            "ANTHROPIC_BASE_URL": proxy_url,
            "ANTHROPIC_AUTH_TOKEN": proxy_key,
            "ANTHROPIC_API_KEY": proxy_key,
            "CLAUDE_CODE_EXEC_USE_SDK": "cli",
            "IS_SANDBOX": "1",
            # 官方 train.py 的进度只走 stdout；不关缓冲的话一整趟日志都是空的。
            "PYTHONUNBUFFERED": "1",
            "OPTIMIZER_AZURE_OPENAI_ENDPOINT": proxy_url,
            "OPTIMIZER_AZURE_OPENAI_API_KEY": proxy_key,
            "OPTIMIZER_AZURE_OPENAI_AUTH_MODE": "openai_compatible",
            "TARGET_AZURE_OPENAI_ENDPOINT": proxy_url,
            "TARGET_AZURE_OPENAI_API_KEY": proxy_key,
            "TARGET_AZURE_OPENAI_AUTH_MODE": "openai_compatible",
            # 训练阶段只按 run 拆账：这一层拿不到题号。
            "ANTHROPIC_CUSTOM_HEADERS": claude_custom_headers_value(run_id, ""),
        }
    )
    if benchmark == "officeqa":
        env["OFFICEQA_DOCS_DIR"] = str(docs_dir or officeqa_docs_root())
    if benchmark == "alfworld":
        env.setdefault("ALFWORLD_DATA", ALFWORLD_DATA_IN_IMAGE)
        env.setdefault("ALFWORLD_WORKER_START_METHOD", "spawn")
    return env


# docker -e 的值整台机器 ps 都看得见，这几个改走 --env-file。
SECRET_ENV = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPTIMIZER_AZURE_OPENAI_API_KEY",
    "TARGET_AZURE_OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
)


def write_env_file(path: Path, env: dict[str, str]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{name}={env[name]}\n" for name in SECRET_ENV if env.get(name))
    path.write_text(body, encoding="utf-8")
    path.chmod(0o600)
    return path


def build_docker_cmd(
    *,
    benchmark: str,
    image: str,
    name: str,
    argv: list[str],
    env: dict[str, str],
    split_mount: Path,
    out_mount: Path,
    skill_init_mount: Path | None = None,
    env_file: Path | None = None,
) -> list[str]:
    """镜像里跑官方 train.py：覆盖 entrypoint，绕开打榜镜像里缩水的超参。"""
    passthrough = (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_EXEC_USE_SDK",
        "IS_SANDBOX",
        "OPTIMIZER_AZURE_OPENAI_ENDPOINT",
        "OPTIMIZER_AZURE_OPENAI_API_KEY",
        "OPTIMIZER_AZURE_OPENAI_AUTH_MODE",
        "TARGET_AZURE_OPENAI_ENDPOINT",
        "TARGET_AZURE_OPENAI_API_KEY",
        "TARGET_AZURE_OPENAI_AUTH_MODE",
        "ALFWORLD_DATA",
        "ALFWORLD_WORKER_START_METHOD",
        "DEEPSEEK_API_KEY",
        "PYTHONUNBUFFERED",
    )
    cmd = ["docker", "run", "--rm", "--name", name, *proxy.docker_host_args()]
    if env_file is not None:
        cmd.extend(["--env-file", str(Path(env_file).resolve())])
    for key in passthrough:
        if env_file is not None and key in SECRET_ENV:
            continue
        if env.get(key):
            cmd.extend(["-e", f"{key}={env[key]}"])
    if skill_init_mount is not None:
        cmd.extend(["-v", f"{Path(skill_init_mount).resolve()}:{SKILL_INIT_IN_IMAGE}:ro"])
    cmd.extend(
        [
            "-v",
            f"{Path(split_mount).resolve()}:/app/train_split",
            "-v",
            f"{Path(out_mount).resolve()}:/out",
            "--entrypoint",
            "bash",
            image,
            "-lc",
            _image_script(benchmark, argv),
        ]
    )
    return cmd


def _image_script(benchmark: str, argv: list[str]) -> str:
    """容器里的引导脚本：先补官方镜像 entrypoint 做的数据准备，再跑官方 train.py。"""
    quoted = " ".join(_shell_quote(part) for part in argv)
    lines = ["set -uo pipefail", f"cd {VENDOR_IN_IMAGE}"]
    if benchmark == "alfworld":
        lines.extend(
            [
                # 官方 entrypoint 的两步准备：logic 文件落位、gamefile 展成绝对路径。
                'mkdir -p "$ALFWORLD_DATA/logic" 2>/dev/null || true',
                'python - "$ALFWORLD_DATA/logic" <<\'PY\' || true\n'
                "import os, shutil, sys\n"
                "dst = sys.argv[1]\n"
                "import alfworld.info as info\n"
                'src = os.path.join(os.path.dirname(info.__file__), "data")\n'
                'for fn in ("alfred.pddl", "alfred.twl2"):\n'
                "    s = os.path.join(src, fn)\n"
                "    if os.path.isfile(s):\n"
                "        shutil.copy2(s, os.path.join(dst, fn))\n"
                "PY",
                'python - /app/train_split "$ALFWORLD_DATA" <<\'PY\'\n'
                "import json, os, sys\n"
                "split, alf = sys.argv[1], sys.argv[2]\n"
                'for sp in ("train", "val", "test"):\n'
                '    p = os.path.join(split, sp, "items.json")\n'
                "    if not os.path.isfile(p):\n"
                "        continue\n"
                "    items = json.load(open(p))\n"
                "    out = []\n"
                "    for it in items:\n"
                "        r = dict(it)\n"
                '        gf = str(r.get("gamefile") or "")\n'
                "        if gf and not os.path.isabs(gf):\n"
                '            r["gamefile"] = os.path.join(alf, gf)\n'
                "        out.append(r)\n"
                '    json.dump(out, open(p, "w"))\n'
                "PY",
            ]
        )
    lines.append(quoted)
    lines.append("rc=$?")
    lines.append("cp -a /tmp/skillopt_run/. /out/ 2>/dev/null || true")
    lines.append("exit $rc")
    return "\n".join(lines)


def _shell_quote(value: str) -> str:
    if value and all(ch.isalnum() or ch in "-_=./:," for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


# 日志会被贴进对话与工单，钥匙不能原样落盘。
_SECRET_FLAGS = (
    "--optimizer_azure_openai_api_key",
    "--target_azure_openai_api_key",
    "--azure_openai_api_key",
)
# 镜像那条路把 key 塞进 -e 与容器脚本里，只按 flag 遮不住，再按形状兜一层。
_SECRET_SHAPE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def redacted_argv(argv: list[str]) -> list[str]:
    out = [_SECRET_SHAPE.sub("sk-***", part) for part in argv]
    for index, part in enumerate(out[:-1]):
        if part in _SECRET_FLAGS:
            out[index + 1] = "***"
    return out


def history_checkpoints(
    run_dir: Path, *, log_file: Path, out_prefix: Path | None = None
) -> list[dict[str, Any]]:
    """把官方 history.json 翻成本框架的 intermediate_checkpoints。"""
    run_dir = Path(run_dir)
    history_path = run_dir / "history.json"
    if not history_path.is_file():
        return []
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(history, list):
        return []
    out: list[dict[str, Any]] = []
    for record in history:
        if not isinstance(record, dict):
            continue
        step = int(record.get("step") or 0)
        skill_path = run_dir / "skills" / f"skill_v{step:04d}.md"
        if not skill_path.is_file():
            continue
        step_dir = run_dir / "steps" / f"step_{step:04d}"
        shown_dir = step_dir if out_prefix is None else Path(out_prefix) / "steps" / f"step_{step:04d}"
        entry: dict[str, Any] = {
            "step": step,
            "round": int(record.get("epoch") or 0),
            "skill_sha256": sha256_file(skill_path),
            "checkpoint_dir": str(shown_dir),
            "action": str(record.get("action") or "no_change"),
            "log_file": str(log_file),
        }
        # selection_hard 只在候选真过了 gate 的那些步才有；跳过改写的步（例如
        # skip_no_patches）只有 current_score，那也是 selection set 上的分。
        score = record.get("selection_hard")
        if score is None:
            score = record.get("current_score")
        if score is not None:
            entry["val_score"] = float(score)
        out.append(entry)
    return out


def best_val_score(run_dir: Path) -> float | None:
    history_path = Path(run_dir) / "history.json"
    if not history_path.is_file():
        return None
    try:
        history = json.loads(history_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    scores = [
        float(rec["best_score"])
        for rec in history
        if isinstance(rec, dict) and rec.get("best_score") is not None
    ]
    return max(scores) if scores else None


def ship_best_skill(run_dir: Path, skill_out: Path) -> str:
    """官方产物是 best_skill.md，抄成本框架的冻结 skill。"""
    best = Path(run_dir) / "best_skill.md"
    if not best.is_file() or not best.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"官方训练没产出 best_skill.md: {best}")
    skill_out = Path(skill_out)
    skill_out.mkdir(parents=True, exist_ok=True)
    shipped = skill_out / "SKILL.md"
    shipped.write_text(best.read_text(encoding="utf-8"), encoding="utf-8")
    return sha256_file(shipped)


def _stream_process(
    argv: list[str],
    *,
    cwd: Path | None,
    env: dict[str, str],
    log_path: Path,
    stream: TextIO | None,
    timeout_s: float,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + max(timeout_s, 60.0)
    with log_path.open("w", encoding="utf-8") as log:
        log.write(" ".join(redacted_argv(argv)) + "\n\n")
        log.flush()
        popen = subprocess.Popen(
            argv,
            cwd=None if cwd is None else str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert popen.stdout is not None
        for line in popen.stdout:
            log.write(line)
            # 一趟要跑几小时，中途得能 tail 得到进度，不能等进程退出才刷盘。
            log.flush()
            if line.startswith(("  [", "===", "  BASELINE")):
                _print(line.rstrip("\n"), stream)
            if time.time() > deadline:
                popen.kill()
                log.write("\nkilled: wall clock budget exhausted\n")
                break
        popen.wait()
    return int(popen.returncode or 0)


@dataclass
class _Runtime:
    kind: str
    argv: list[str]
    cwd: Path | None
    env: dict[str, str]
    run_dir: Path
    extra: dict[str, Any] = field(default_factory=dict)


def plan_run(
    *,
    benchmark: str,
    model: str,
    run_id: str,
    output_dir: Path,
    workers: int,
    proxy_url: str,
    proxy_key: str,
    split_dir: Path | None = None,
    train_size: int | None = None,
    skill_init: Path | None = None,
) -> _Runtime:
    output_dir = Path(output_dir)
    split = Path(split_dir) if split_dir else split_root(benchmark)
    if benchmark in HOST_BENCHMARKS:
        run_dir = output_dir / "skillopt_run"
        argv = build_argv(
            benchmark=benchmark,
            model=model,
            split_dir=split,
            out_root=run_dir,
            workers=workers,
            proxy_url=proxy_url,
            python_bin=os.environ.get("XSKILL_BENCH_PYTHON", "python3.11"),
            train_size=train_size,
            skill_init=skill_init,
        )
        env = build_env(
            benchmark=benchmark,
            proxy_url=proxy_url,
            proxy_key=proxy_key,
            run_id=run_id,
        )
        vendor = skillopt_vendor_root()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(vendor), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        return _Runtime(kind="host", argv=argv, cwd=vendor, env=env, run_dir=run_dir)

    image = os.environ.get("XSKILL_BENCH_SKILLOPT_TRAIN_IMAGE") or SKILLOPT_TRAIN_IMAGES.get(
        benchmark
    )
    if not image:
        raise RuntimeError(f"SkillOpt 官方训练在 {benchmark} 上既不能本机跑也没有镜像")
    run_dir = output_dir / "skillopt_run"
    run_dir.mkdir(parents=True, exist_ok=True)
    container_url = proxy.container_base_url(proxy_url)
    argv = build_argv(
        benchmark=benchmark,
        model=model,
        split_dir=Path("/app/train_split"),
        out_root=Path("/tmp/skillopt_run"),
        workers=workers,
        proxy_url=container_url,
        claude_path="claude",
        python_bin="python",
        train_size=train_size,
        skill_init=Path(SKILL_INIT_IN_IMAGE) if skill_init else None,
    )
    env = build_env(
        benchmark=benchmark,
        proxy_url=container_url,
        proxy_key=proxy_key,
        run_id=run_id,
        base_env={},
    )
    name = f"xskill-bench-skillopt-{run_id}".replace(":", "-")[:60]
    # 镜像里 split 目录会被就地改写（gamefile 展绝对路径），所以挂副本不挂官方 split。
    split_copy = output_dir / "_split_for_image"
    # 放 output_dir 根下，不放 run_dir：后者会挂进容器的 /out。
    cmd = build_docker_cmd(
        benchmark=benchmark,
        image=image,
        name=name,
        argv=argv,
        env=env,
        split_mount=split_copy,
        out_mount=run_dir,
        skill_init_mount=Path(skill_init) if skill_init else None,
        env_file=write_env_file(output_dir / ".train_secrets.env", env),
    )
    return _Runtime(
        kind="image",
        argv=cmd,
        cwd=None,
        env=dict(os.environ),
        run_dir=run_dir,
        extra={"image": image, "split_src": split, "split_copy": split_copy, "name": name},
    )


def run_official_train(
    *,
    benchmark: str,
    model: str,
    run_id: str,
    skill_out: Path,
    output_dir: Path,
    proxy_url: str,
    proxy_key: str,
    split_dir: Path | None = None,
    train_size: int | None = None,
    skill_init: Path | None = None,
    wall_timeout_s: float = 24 * 3600,
    stream: TextIO | None = None,
    runner: Callable[..., int] | None = None,
) -> OfficialTrainResult:
    output_dir = Path(output_dir)
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "skillopt_train.log"
    official, workers = effective_workers(benchmark)
    plan = plan_run(
        benchmark=benchmark,
        model=model,
        run_id=run_id,
        output_dir=output_dir,
        workers=workers,
        proxy_url=proxy_url,
        proxy_key=proxy_key,
        split_dir=split_dir,
        train_size=train_size,
        skill_init=skill_init,
    )
    spec = skillopt_official(benchmark)
    hyper = {
        "source": spec["config"],
        "runtime": plan.kind,
        "num_epochs": spec["train"]["num_epochs"],
        "train_size": spec["train"]["train_size"] if train_size is None else int(train_size),
        "batch_size": spec["train"]["batch_size"],
        "accumulation": spec["train"]["accumulation"],
        "minibatch_size": spec["train"]["minibatch_size"],
        "merge_batch_size": spec["train"]["merge_batch_size"],
        "skill_update_mode": spec["train"]["skill_update_mode"],
        "lr_scheduler": spec["train"]["lr_scheduler"],
        "learning_rate": spec["train"]["learning_rate"],
        "use_gate": spec["train"]["use_gate"],
        "use_slow_update": spec["train"]["use_slow_update"],
        "use_meta_skill": spec["train"]["use_meta_skill"],
        "seed": spec["train"]["seed"],
        "rollout_workers": workers,
        "rollout_workers_official": official,
        "optimizer_backend": "openai_chat",
        "target_backend": "claude_code_exec",
        "skill_init": "caller_seed" if skill_init else "vendor_default",
    }
    if spec.get("deviations"):
        hyper["deviations"] = spec["deviations"]

    if plan.kind == "image":
        import shutil

        src = Path(plan.extra["split_src"])
        dest = Path(plan.extra["split_copy"])
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        _print(f"skillopt official train {benchmark} image={plan.extra['image']}", stream)
    else:
        _print(f"skillopt official train {benchmark} host vendor={plan.cwd}", stream)
    _print(
        f"epochs={hyper['num_epochs']} batch={hyper['batch_size']} "
        f"minibatch={hyper['minibatch_size']} update={hyper['skill_update_mode']} "
        f"workers={workers}",
        stream,
    )

    raw_wall = os.environ.get("XSKILL_BENCH_SKILLOPT_WALL_S", "").strip()
    if raw_wall:
        wall_timeout_s = max(float(raw_wall), 60.0)
    exec_fn = runner or _stream_process
    try:
        returncode = exec_fn(
            plan.argv,
            cwd=plan.cwd,
            env=plan.env,
            log_path=log_file,
            stream=stream,
            timeout_s=wall_timeout_s,
        )
    finally:
        # 产物目录会被打包带走，run 的 key 不留在里面。
        (output_dir / ".train_secrets.env").unlink(missing_ok=True)
    frozen = ship_best_skill(plan.run_dir, skill_out)
    checkpoints = history_checkpoints(plan.run_dir, log_file=log_file)
    if not checkpoints:
        checkpoints = [
            {
                "step": 1,
                "round": 1,
                "skill_sha256": frozen,
                "checkpoint_dir": str(plan.run_dir),
                "action": "accept",
                "log_file": str(log_file),
            }
        ]
    actions = [str(entry.get("action") or "") for entry in checkpoints]
    # 官方自己就是按子串 accept 统计接受步的（force_accept 也算），这里照它的口径。
    action = next(
        (name for name in reversed(actions) if "accept" in name),
        actions[-1] or "accept",
    )
    return OfficialTrainResult(
        frozen_sha=frozen,
        checkpoints=checkpoints,
        log_text=log_file.read_text(encoding="utf-8")[-40_000:] if log_file.is_file() else "",
        action=action,
        val_score=best_val_score(plan.run_dir),
        hyperparameters=hyper,
        runtime=plan.kind,
        n_steps=len(checkpoints),
        returncode=returncode,
        run_dir=plan.run_dir,
    )
