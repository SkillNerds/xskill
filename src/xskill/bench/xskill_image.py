"""Launch official xskill noforce/mergeval train images. Do not Chat-rewrite."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

from xskill.bench import proxy
from xskill.bench.dataset import split_root
from xskill.bench.hparams import xskill_official
from xskill.bench.skill_io import hash_skill
from xskill.bench.stdio import safe_write

XSKILL_TRAIN_IMAGES = {
    "officeqa": "localhost:5000/p_user1/algo-xskill-officeqa:hard-canary-noforce-fix-20260909",
    "spreadsheet": "localhost:5000/p_user1/algo-xskill-spreadsheet-full:noforce-mergeval-20260909",
    "alfworld": "localhost:5000/p_user1/algo-xskill-alf-full:noforce-mergeval-fix-20260909",
}

# 官方镜像把上游地址写死在 entrypoint 与 config.yaml 里，请求到不了 LiteLLM。
# 这三个是只改地址来源的副本（算法逐字不动，旧 tag 不覆盖），
# 见 benchmarks/images/xskill_train_litellm/。
XSKILL_TRAIN_IMAGES_LITELLM = {
    "officeqa": "localhost:5000/p_user1/algo-xskill-officeqa:hard-canary-noforce-fix-litellm-20260909",
    "spreadsheet": "localhost:5000/p_user1/algo-xskill-spreadsheet-full:noforce-mergeval-litellm-20260909",
    "alfworld": "localhost:5000/p_user1/algo-xskill-alf-full:noforce-mergeval-fix-litellm-20260909",
}

_AIKEY = Path("/home/admin/.aikey")
_PASS_ENV = (
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
)
# 副本 entrypoint 认这三个地址覆盖变量。
LITELLM_ENV_KEYS = (
    "XSKILL_ANTHROPIC_BASE_URL",
    "XSKILL_LLM_BASE_URL",
    "XSKILL_EMBEDDING_BASE_URL",
)


def train_image(benchmark: str, *, via_litellm: bool = True) -> str:
    override = os.environ.get("XSKILL_BENCH_TRAIN_IMAGE")
    if override:
        return override
    table = XSKILL_TRAIN_IMAGES_LITELLM if via_litellm else XSKILL_TRAIN_IMAGES
    image = table.get(benchmark)
    if not image:
        raise ValueError(f"no official xskill train image for {benchmark}")
    return image


def host_split_root(benchmark: str) -> Path:
    return split_root(benchmark)


def load_aikey() -> dict[str, str]:
    values: dict[str, str] = {}
    if not _AIKEY.is_file():
        return values
    for line in _AIKEY.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw = line.split("=", 1)
        values[name.strip()] = raw.strip().strip("'\"")
    return values


def write_limit_overlay(src: Path, dest: Path, limit: int) -> Path:
    """Copy official split into dest, keep the first N of each split. Does not touch src."""
    if limit <= 0:
        raise ValueError("limit overlay needs limit > 0")
    if dest.resolve() == src.resolve():
        raise RuntimeError("refuse to write overlay onto the official split")
    dest.mkdir(parents=True, exist_ok=True)
    for split in ("train", "val", "test"):
        src_items = src / split / "items.json"
        out_dir = dest / split
        out_dir.mkdir(parents=True, exist_ok=True)
        if not src_items.is_file():
            continue
        items = json.loads(src_items.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            raise ValueError(f"expected list in {src_items}")
        # xskill merges val into train. Keep the first N of each split so
        # --limit N means N train + N val, then N test at eval.
        kept = items[:limit]
        (out_dir / "items.json").write_text(
            json.dumps(kept, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return dest


def effective_hparams(benchmark: str, env: dict[str, str]) -> dict[str, Any]:
    """镜像真正吃到的那几个数。--limit 会把它们改小，provenance 得写真实值。"""
    official = xskill_official(benchmark)
    used = {
        "epochs": int(env.get("XSKILL_EPOCHS") or official["epochs"]),
        "workers": int(env.get("XSKILL_WORKERS") or official["workers"]),
        "canary_settle_s": int(env.get("XSKILL_CANARY_SETTLE") or official["canary_settle_s"]),
        "item_timeout_s": int(env.get("ITEM_TIMEOUT") or official["item_timeout_s"]),
        "max_tool_turns": int(env.get("MAX_TOOL_TURNS") or official["max_tool_turns"]),
        "merge_val_into_train": env.get("XSKILL_MERGE_VAL_INTO_TRAIN", "true") == "true",
    }
    shrunk = {
        name: official[name]
        for name in ("epochs", "workers", "canary_settle_s", "item_timeout_s")
        if int(official[name]) != used[name]
    }
    if shrunk:
        used["official"] = shrunk
        used["source"] = "smoke_shortened"
    else:
        used["source"] = "xskill_official_image_defaults"
    return used


def smoke_env(limit: int, *, n_train_items: int) -> dict[str, str]:
    """Shorter canary loop when --limit is set. Full runs keep image defaults."""
    if limit <= 0:
        return {}
    return {
        "XSKILL_EPOCHS": "1",
        "XSKILL_WORKERS": "1",
        "XSKILL_TRAIN_LIMIT": str(max(int(n_train_items), 1)),
        "FINAL_SETTLE": "20",
        # XSKILL_FLUSH_WAIT 不缩：它是 baby→main 毕业的等待上限，等不够镜像会
        # FATAL 退出而不是少练几轮。用镜像默认 600。
        "XSKILL_CANARY_SETTLE": "20",
        "ITEM_TIMEOUT": "300",
        "XSKILL_CLIENT_QUIET": "5",
        "XSKILL_CLIENT_MIN_CHANGE": "8",
        "XSKILL_INGEST_SETTLE": "8",
        "XSKILL_VAL_ENABLE": "false",
    }


def build_train_env(
    *,
    benchmark: str,
    model: str,
    limit: int,
    merge_val: bool = True,
    n_train_items: int = 0,
    proxy_url: str | None = None,
    proxy_key: str | None = None,
) -> dict[str, str]:
    """训练超参一律用 xskill 官方值，不接命令行的 workers 和超时。

    评测那边跟 SkillOpt 对齐；训练这边是 xskill 自己的算法参数（epoch、
    simulated user workers、canary settle），命令行默认值不许改它。
    """
    official = xskill_official(benchmark)
    env = {
        "OUTPUT_DIR": "/out",
        "SKILL_DIR": "/shared/skill",
        "EVAL_MODEL": model,
        "XSKILL_MERGE_VAL_INTO_TRAIN": "true" if merge_val else "false",
        "XSKILL_EPOCHS": str(int(official["epochs"])),
        "XSKILL_WORKERS": str(int(official["workers"])),
        "ITEM_TIMEOUT": str(int(official["item_timeout_s"])),
        "MAX_TOOL_TURNS": str(int(official["max_tool_turns"])),
        "XSKILL_MAX_TURNS": str(int(official["max_tool_turns"])),
        "FINAL_SETTLE": str(int(official["canary_settle_s"])),
        "XSKILL_CANARY_SETTLE": str(int(official["canary_settle_s"])),
    }
    if proxy_url:
        container_url = proxy.container_base_url(proxy_url)
        # LiteLLM 在根路径上就提供 /v1/messages，不要加 /anthropic 后缀。
        env["XSKILL_ANTHROPIC_BASE_URL"] = container_url
        env["XSKILL_LLM_BASE_URL"] = container_url
        env["XSKILL_EMBEDDING_BASE_URL"] = container_url
        if proxy_key:
            # 镜像里 config.yaml 与 Claude Code 都拿这两个变量当钥匙，
            # 换成代理的 key 就等于全部改道 LiteLLM。
            env["DEEPSEEK_API_KEY"] = proxy_key
            env["DASHSCOPE_API_KEY"] = proxy_key
    if limit > 0:
        if n_train_items > 0:
            train_n = n_train_items
        elif merge_val:
            train_n = limit * 2
        else:
            train_n = limit
        env.update(smoke_env(limit, n_train_items=train_n))
    return env


def build_docker_cmd(
    *,
    image: str,
    name: str,
    env: dict[str, str],
    skill_mount: Path,
    out_mount: Path,
    split_mount: Path | None = None,
) -> list[str]:
    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        name,
        # Bridge, not host: the image binds XSKILL_DAEMON_PORT on 127.0.0.1.
        # Host network collides with a login-machine xskill already on 8791.
        # 桥接下要靠这条别名回连宿主的 LiteLLM。
        *proxy.docker_host_args(),
        "-v",
        f"{skill_mount.resolve()}:/shared/skill",
        "-v",
        f"{out_mount.resolve()}:/out",
    ]
    if split_mount is not None:
        cmd.extend(["-v", f"{split_mount.resolve()}:/app/train_split"])
    for key in _PASS_ENV:
        if key not in env:
            cmd.extend(["-e", key])
    for key, value in env.items():
        cmd.extend(["-e", f"{key}={value}"])
    cmd.append(image)
    return cmd


def wall_timeout_s(*, timeout_s: float, n_items: int, limit: int) -> float:
    items = max(n_items, 1)
    epochs = 1 if limit > 0 else 4
    per = max(float(timeout_s), 60.0)
    extra = 420.0 if limit > 0 else 900.0
    return per * items * epochs + extra


def official_wall_timeout_s(*, benchmark: str, n_items: int, limit: int) -> float:
    official = xskill_official(benchmark)
    per = float(official["item_timeout_s"]) if limit <= 0 else 300.0
    return wall_timeout_s(timeout_s=per, n_items=n_items, limit=limit)


@dataclass
class ImageTrainResult:
    frozen_sha: str
    checkpoints: list[dict[str, Any]]
    log_text: str
    action: str
    image: str
    n_skills: int
    hyperparameters: dict[str, Any]


def harvest_shipped_skills(image_skill: Path, skill_out: Path) -> int:
    shipped = Path(image_skill) / "skills"
    if not shipped.is_dir():
        raise FileNotFoundError(f"image did not write {shipped}")
    skill_out = Path(skill_out)
    if skill_out.exists():
        shutil.rmtree(skill_out)
    shutil.copytree(shipped, skill_out)
    return len(list(skill_out.rglob("SKILL.md")))


def _print(line: str, stream: TextIO | None) -> None:
    safe_write(line, stream)


def _docker(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def run_xskill_image_train(
    *,
    benchmark: str,
    model: str,
    run_id: str,
    uids: list[str],
    skill_out: Path,
    output_dir: Path,
    limit: int = 0,
    proxy_url: str | None = None,
    proxy_key: str | None = None,
    stream: TextIO | None = None,
    docker_fn: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    wait_fn: Callable[[Path, Path, float, str, dict[str, str] | None], str] | None = None,
) -> ImageTrainResult:
    if benchmark not in XSKILL_TRAIN_IMAGES:
        raise RuntimeError(f"xskill image train unsupported for {benchmark}")
    image = train_image(benchmark, via_litellm=bool(proxy_url))
    output_dir = Path(output_dir)
    logs_dir = output_dir / "logs"
    ckpt_dir = output_dir / "checkpoints" / "step_1"
    image_skill = output_dir / "image_skill"
    image_out = output_dir / "image_out"
    logs_dir.mkdir(parents=True, exist_ok=True)
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    image_skill.mkdir(parents=True, exist_ok=True)
    image_out.mkdir(parents=True, exist_ok=True)

    split_mount = None
    if limit > 0:
        overlay = output_dir / "_split_overlay"
        write_limit_overlay(host_split_root(benchmark), overlay, limit)
        split_mount = overlay

    env = build_train_env(
        benchmark=benchmark,
        model=model,
        limit=limit,
        merge_val=True,
        n_train_items=len(uids),
        proxy_url=proxy_url,
        proxy_key=proxy_key,
    )
    name = f"xskill-bench-train-{run_id}".replace(":", "-")[:60]
    cmd = build_docker_cmd(
        image=image,
        name=name,
        env=env,
        skill_mount=image_skill,
        out_mount=image_out,
        split_mount=split_mount,
    )
    keys = load_aikey()
    child_env = dict(os.environ)
    for key in _PASS_ENV:
        if keys.get(key):
            child_env[key] = keys[key]
        if not child_env.get(key):
            raise RuntimeError(f"xskill image train needs {key} in ~/.aikey or the environment")

    log_lines = [
        f"xskill image train {benchmark} image={image} items={len(uids)} limit={limit}",
        f"container={name}",
        "algo=noforce-canary (not Chat rewrite)",
        "hparams=xskill official: "
        + " ".join(f"{key}={env[key]}" for key in sorted(env) if key.startswith("XSKILL_") or key in {"ITEM_TIMEOUT", "MAX_TOOL_TURNS", "FINAL_SETTLE"}),
        "llm=litellm" if proxy_url else "llm=upstream-direct",
    ]
    _print(log_lines[0], stream)

    runner = docker_fn or _docker
    runner(["docker", "rm", "-f", name], env=child_env)
    started = runner(cmd, env=child_env)
    if started.returncode != 0:
        raise RuntimeError((started.stderr or started.stdout or "docker run failed").strip())
    cid = (started.stdout or "").strip()
    log_lines.append(f"container_id={cid[:12] if cid else '?'}")

    deadline = official_wall_timeout_s(benchmark=benchmark, n_items=len(uids), limit=limit)
    waiter = wait_fn or _wait_for_marker
    logs_blob = ""
    marker = "timeout"
    try:
        marker = waiter(image_skill, image_out, deadline, name, child_env)
        logs_proc = runner(["docker", "logs", name], env=child_env, timeout=60)
        if logs_proc.returncode == 0:
            logs_blob = (logs_proc.stdout or "") + (logs_proc.stderr or "")
    finally:
        runner(["docker", "stop", "-t", "15", name], env=child_env)
        runner(["docker", "rm", "-f", name], env=child_env)

    log_lines.append(f"marker={marker}")

    if marker != "DONE":
        err = (image_skill / "ERROR").read_text(encoding="utf-8") if (image_skill / "ERROR").is_file() else marker
        raise RuntimeError(f"xskill image train did not finish: {err}")

    n_skills = harvest_shipped_skills(image_skill, skill_out)
    if n_skills <= 0:
        raise RuntimeError("xskill image train collected zero SKILL.md")
    frozen = hash_skill(skill_dir=skill_out)
    if not frozen:
        raise RuntimeError("failed to hash shipped skill")
    shutil.copytree(skill_out, ckpt_dir / "skill", dirs_exist_ok=True)
    log_lines.append(f"collected {n_skills} SKILL.md")
    log_lines.append(f"skill_sha256={frozen}")
    log_text = "\n".join(log_lines) + "\n"
    log_file = logs_dir / "step_1.log"
    log_file.write_text(log_text, encoding="utf-8")
    if logs_blob:
        (logs_dir / "image.stdout.log").write_text(logs_blob[-200_000:], encoding="utf-8")
    return ImageTrainResult(
        frozen_sha=frozen,
        checkpoints=[
            {
                "step": 1,
                "skill_sha256": frozen,
                "checkpoint_dir": str(ckpt_dir),
                "action": "merged_to_main",
                "log_file": str(log_file),
            }
        ],
        log_text=log_text,
        action="merged_to_main",
        image=image,
        n_skills=n_skills,
        hyperparameters=effective_hparams(benchmark, env),
    )


def _wait_for_marker(
    image_skill: Path,
    image_out: Path,
    deadline_s: float,
    name: str,
    env: dict[str, str] | None,
) -> str:
    del image_out
    started = time.time()
    while time.time() - started < deadline_s:
        if (image_skill / "DONE").is_file():
            return "DONE"
        if (image_skill / "FAILED").is_file():
            return "FAILED"
        if (image_skill / "ERROR").is_file():
            return "ERROR"
        inspect = _docker(
            ["docker", "inspect", "-f", "{{.State.Running}} {{.State.Status}}", name],
            env=env,
        )
        if inspect.returncode == 0:
            parts = (inspect.stdout or "").split()
            running = parts[0] if parts else ""
            status = parts[1] if len(parts) > 1 else ""
            if running == "false":
                if (image_skill / "DONE").is_file():
                    return "DONE"
                return f"exited:{status or 'unknown'}"
        time.sleep(5)
    return "timeout"
