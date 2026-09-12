"""真训练与真评测的请求必须落到 LiteLLM：环境变量、镜像补丁、容器连通性、路由。"""
from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path

import pytest

from xskill.bench import proxy
from xskill.bench.skillopt_train import build_env as skillopt_build_env
from xskill.bench.xskill_image import (
    XSKILL_TRAIN_IMAGES,
    XSKILL_TRAIN_IMAGES_LITELLM,
    build_docker_cmd,
    build_train_env,
    train_image,
)

ROOT = Path(__file__).resolve().parents[1]
PROXY = "http://127.0.0.1:4000"
CONTAINER_PROXY = "http://host.docker.internal:4000"
DEEPSEEK_ANTHROPIC = "https://api.deepseek.com/anthropic"
DEEPSEEK_OPENAI = "https://api.deepseek.com"
DASHSCOPE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
BENCHMARKS = ("officeqa", "spreadsheet", "alfworld")


def _image_or_skip(image: str) -> str:
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        pytest.skip("docker not installed")
    if proc.returncode != 0:
        pytest.skip(f"image missing: {image}")
    return image


def _in_image(image: str, script: str, env: dict[str, str] | None = None) -> str:
    cmd = ["docker", "run", "--rm", *proxy.docker_host_args()]
    for key, value in (env or {}).items():
        cmd.extend(["-e", f"{key}={value}"])
    cmd.extend(["--entrypoint", "bash", image, "-lc", script])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


# 容器里按镜像自己的 entrypoint 代码算出生效端点：先 eval 那行 export，
# 再 eval 渲染 config.yaml 的那段 sed。不复述我们的猜测，跑它的代码。
_EFFECTIVE = r"""
set -u
: "${DEEPSEEK_API_KEY:=probe-key}"
: "${DASHSCOPE_API_KEY:=probe-key}"
export DEEPSEEK_API_KEY DASHSCOPE_API_KEY
eval "$(grep -m1 'export ANTHROPIC_BASE_URL=' /app/entrypoint_train.sh | sed 's/^ *//')"
echo "anthropic=$ANTHROPIC_BASE_URL"
export XSKILL_HOME=/tmp/probe_home
mkdir -p "$XSKILL_HOME"
eval "$(sed -n '/^sed -e "s#__XSKILL_HOME__#/,/config\.yaml"$/p' /app/entrypoint_train.sh)"
grep base_url "$XSKILL_HOME/config.yaml" | awk '{print "base_url="$2}'
"""


def _effective_endpoints(image: str, env: dict[str, str] | None = None) -> dict[str, object]:
    out = _in_image(image, _EFFECTIVE, env)
    anthropic = ""
    base_urls: list[str] = []
    for line in out.splitlines():
        if line.startswith("anthropic="):
            anthropic = line.split("=", 1)[1].strip()
        elif line.startswith("base_url="):
            base_urls.append(line.split("=", 1)[1].strip())
    return {"anthropic": anthropic, "base_urls": base_urls}


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_patched_image_sends_every_endpoint_to_litellm(benchmark):
    image = _image_or_skip(XSKILL_TRAIN_IMAGES_LITELLM[benchmark])
    got = _effective_endpoints(
        image,
        {
            "XSKILL_ANTHROPIC_BASE_URL": CONTAINER_PROXY,
            "XSKILL_LLM_BASE_URL": CONTAINER_PROXY,
            "XSKILL_EMBEDDING_BASE_URL": CONTAINER_PROXY,
        },
    )
    assert got["anthropic"] == CONTAINER_PROXY
    # llm.base_url（蒸馏与 SkillEdit）与 embedding.base_url（atom 入库检索）两条都要改道。
    assert got["base_urls"] == [CONTAINER_PROXY, CONTAINER_PROXY]


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_patched_image_without_override_is_identical_to_official(benchmark):
    """不设覆盖变量时行为与官方镜像逐字相同，补丁不改算法。"""
    image = _image_or_skip(XSKILL_TRAIN_IMAGES_LITELLM[benchmark])
    got = _effective_endpoints(image)
    assert got["anthropic"] == DEEPSEEK_ANTHROPIC
    assert got["base_urls"] == [DEEPSEEK_OPENAI, DASHSCOPE]


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_official_image_untouched_and_copy_uses_new_tag(benchmark):
    official = XSKILL_TRAIN_IMAGES[benchmark]
    copy = XSKILL_TRAIN_IMAGES_LITELLM[benchmark]
    assert official != copy
    _image_or_skip(official)
    # 官方镜像里没有任何覆盖变量，说明旧 tag 没被就地改写。
    out = _in_image(official, "grep -c XSKILL_ANTHROPIC_BASE_URL /app/entrypoint_train.sh || true")
    assert out.strip() == "0"


def test_container_can_reach_host_litellm():
    if not proxy.alive():
        pytest.skip("LiteLLM proxy down")
    image = _image_or_skip(XSKILL_TRAIN_IMAGES_LITELLM["officeqa"])
    out = _in_image(image, f"curl -s -m 8 {CONTAINER_PROXY}/health/liveliness || echo UNREACHABLE")
    assert "alive" in out, f"容器连不上宿主代理: {out!r}"


def test_proxy_serves_both_routes_the_two_algos_need():
    if not proxy.alive() or not proxy.master_key():
        pytest.skip("LiteLLM proxy down or no master key")
    names = set(proxy.model_names())
    # 做题与反思走 chat，xskill 的 atom 检索走 embedding，缺一条就有请求绕开代理。
    assert "deepseek-v4-flash" in names
    assert "text-embedding-v4" in names
    req = urllib.request.Request(
        proxy.proxy_base_url() + "/v1/embeddings",
        data=json.dumps({"model": "text-embedding-v4", "input": "ping"}).encode(),
        headers={
            "Authorization": f"Bearer {proxy.master_key()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    assert len(doc["data"][0]["embedding"]) > 0
    assert doc["usage"]["prompt_tokens"] > 0


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_xskill_train_env_points_everything_at_the_proxy(benchmark):
    env = build_train_env(
        benchmark=benchmark,
        model="deepseek-v4-flash",
        limit=0,
        proxy_url=PROXY,
        proxy_key="sk-run-key",
    )
    for name in (
        "XSKILL_ANTHROPIC_BASE_URL",
        "XSKILL_LLM_BASE_URL",
        "XSKILL_EMBEDDING_BASE_URL",
    ):
        assert env[name] == CONTAINER_PROXY, name
    # 镜像拿 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY 当钥匙，这里换成代理的 key。
    assert env["DEEPSEEK_API_KEY"] == "sk-run-key"
    assert env["DASHSCOPE_API_KEY"] == "sk-run-key"
    assert train_image(benchmark, via_litellm=True) == XSKILL_TRAIN_IMAGES_LITELLM[benchmark]
    assert train_image(benchmark, via_litellm=False) == XSKILL_TRAIN_IMAGES[benchmark]


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_xskill_docker_cmd_carries_proxy_and_no_upstream_keys(benchmark, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-upstream-deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-upstream-dashscope")
    env = build_train_env(
        benchmark=benchmark,
        model="deepseek-v4-flash",
        limit=0,
        proxy_url=PROXY,
        proxy_key="sk-run-key",
    )
    cmd = build_docker_cmd(
        image=train_image(benchmark, via_litellm=True),
        name="route-test",
        env=env,
        skill_mount=Path("/tmp/route-skill"),
        out_mount=Path("/tmp/route-out"),
    )
    joined = " ".join(cmd)
    assert "host.docker.internal:host-gateway" in joined
    assert CONTAINER_PROXY in joined
    assert "sk-upstream-deepseek" not in joined
    assert "sk-upstream-dashscope" not in joined


@pytest.mark.parametrize("benchmark", BENCHMARKS)
def test_skillopt_train_env_points_optimizer_and_target_at_proxy(benchmark):
    env = skillopt_build_env(
        benchmark=benchmark,
        proxy_url=PROXY,
        proxy_key="sk-run-key",
        run_id="route-run",
        base_env={},
    )
    # 反思 LLM 与做题 Claude 都走代理，两条都不许直连上游。
    assert env["OPTIMIZER_AZURE_OPENAI_ENDPOINT"] == PROXY
    assert env["TARGET_AZURE_OPENAI_ENDPOINT"] == PROXY
    assert env["ANTHROPIC_BASE_URL"] == PROXY
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-run-key"
    assert env["OPTIMIZER_AZURE_OPENAI_AUTH_MODE"] == "openai_compatible"
    assert "api.deepseek.com" not in json.dumps(env)


@pytest.mark.parametrize("benchmark", ["officeqa", "spreadsheet"])
def test_skillopt_host_train_keeps_the_key_off_the_command_line(benchmark, tmp_path):
    """本机那条路的 key 只走 env：命令行是全机可见的。"""
    from xskill.bench.skillopt_train import plan_run

    plan = plan_run(
        benchmark=benchmark,
        model="deepseek-v4-flash",
        run_id="route-host",
        output_dir=tmp_path,
        workers=2,
        proxy_url=PROXY,
        proxy_key="sk-run-key",
    )
    assert plan.kind == "host"
    assert "sk-run-key" not in " ".join(plan.argv)
    assert plan.env["OPTIMIZER_AZURE_OPENAI_API_KEY"] == "sk-run-key"
    assert plan.env["TARGET_AZURE_OPENAI_API_KEY"] == "sk-run-key"


def test_skillopt_alfworld_image_bootstrap_and_trainer_are_real(tmp_path):
    """ALFWorld 只能在镜像里训：验数据准备真跑通、官方 train.py 真能起。"""
    import shutil

    from xskill.bench.skillopt_train import SKILLOPT_TRAIN_IMAGES, plan_run

    _image_or_skip(SKILLOPT_TRAIN_IMAGES["alfworld"])
    plan = plan_run(
        benchmark="alfworld",
        model="deepseek-v4-flash",
        run_id="alf-bootstrap",
        output_dir=tmp_path,
        workers=2,
        proxy_url=PROXY,
        proxy_key="sk-run-key",
    )
    src = Path(plan.extra["split_src"])
    dest = Path(plan.extra["split_copy"])
    if not (src / "train" / "items.json").is_file():
        pytest.skip(f"alfworld split missing: {src}")
    shutil.copytree(src, dest)
    before = json.loads((dest / "train" / "items.json").read_text(encoding="utf-8"))[0]
    assert not str(before.get("gamefile") or "").startswith("/")

    argv = list(plan.argv)
    # 换成 --help：只验容器脚本与官方入口，不真开训。
    argv[-1] = argv[-1].replace("scripts/train.py", "scripts/train.py --help", 1)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900, check=False)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "SkillOpt unified training entry point" in proc.stdout
    # 官方 entrypoint 那步 gamefile 展绝对路径必须真做了，否则开训就找不到局面文件。
    after = json.loads((dest / "train" / "items.json").read_text(encoding="utf-8"))[0]
    assert str(after["gamefile"]).startswith("/alfworld_data/")
    # 官方 split 不许被就地改写。
    still = json.loads((src / "train" / "items.json").read_text(encoding="utf-8"))[0]
    assert not str(still.get("gamefile") or "").startswith("/")


def test_official_train_log_does_not_leak_keys():
    from xskill.bench.skillopt_train import redacted_argv

    argv = [
        "python",
        "scripts/train.py",
        "--target_azure_openai_api_key",
        "sk-realkey1234567890",
        "-e",
        "OPTIMIZER_AZURE_OPENAI_API_KEY=sk-anotherkey1234",
        "bash",
        "-lc",
        "cd /app/SkillOpt\npython scripts/train.py --optimizer_azure_openai_api_key sk-inscript12345",
    ]
    joined = " ".join(redacted_argv(argv))
    assert "sk-realkey1234567890" not in joined
    assert "sk-anotherkey1234" not in joined
    assert "sk-inscript12345" not in joined
    # 遮住钥匙但命令本身还要能读，不然日志没用。
    assert "scripts/train.py" in joined
    assert "--target_azure_openai_api_key ***" in joined


def test_train_usage_sidecar_records_run_level_totals(tmp_path):
    """训练阶段逐题分不开，只按 run 记；provenance schema 不许加字段，写旁路文件。"""
    from xskill.bench.pipeline import _train_usage_lines

    lines = _train_usage_lines(
        output_dir=tmp_path,
        run_id="usage-run",
        model="deepseek-v4-flash",
        litellm_url=None,
        run_key=None,
    )
    doc = json.loads((tmp_path / "train_usage.json").read_text(encoding="utf-8"))
    assert doc["run_id"] == "usage-run"
    assert doc["granularity"] == "run"
    assert doc["usage"]["source"] in {"litellm", "missing"}
    assert lines and "Train usage" in lines[0]
    if doc["usage"]["source"] == "litellm":
        # 一趟几千笔，request_id 只留抽样，笔数单独记，不然旁路文件全是 id。
        assert len(doc["usage"].get("request_ids") or []) <= 20
        assert doc["usage"]["n_requests"] >= len(doc["usage"].get("request_ids") or [])


def test_run_key_alias_is_unique_per_attempt():
    """run_id 每趟都一样（train-officeqa-xskill），alias 撞名代理会拒，整趟就没法归账。"""
    from xskill.bench import proxy

    first = proxy.run_key_alias("train-officeqa-xskill")
    assert first != proxy.run_key_alias("train-officeqa-xskill", nonce="20260905-090000")
    assert "train-officeqa-xskill" in first

    if not proxy.master_key():
        pytest.skip("no master key")
    run_id = "alias-reuse-probe"
    keys = [proxy.try_create_run_key(run_id) for _ in range(2)]
    assert all(k is not None for k in keys), "同一个 run_id 连建两次都得成功"
    assert keys[0].token != keys[1].token


def test_vendor_claude_subprocess_inherits_our_proxy_env():
    """vendor 起 Claude 时不清 env，所以我们设的代理地址与拆账头能传下去。"""
    from xskill.bench.dataset import skillopt_vendor_root

    path = skillopt_vendor_root() / "skillopt/model/codex_harness.py"
    if not path.is_file():
        pytest.skip("vendor harness missing")
    text = path.read_text(encoding="utf-8")
    # run_env 只有两种取值：None（继承父进程）或 dict(os.environ) 的副本。
    assert "run_env: dict[str, str] | None = None" in text
    assert "run_env = dict(os.environ)" in text
    assert "env=run_env," in text
    assert "env={}" not in text


def test_no_direct_upstream_endpoint_left_in_bench_code():
    """框架代码里不许再出现直连上游的地址（镜像补丁脚本除外，它就是在换掉这些）。"""
    bench = ROOT / "src/xskill/bench"
    offenders = []
    for path in bench.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "api.deepseek.com" in stripped or "dashscope.aliyuncs.com" in stripped:
                offenders.append(f"{path.name}:{lineno}:{stripped}")
    assert offenders == [], offenders
