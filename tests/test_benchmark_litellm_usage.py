"""第八步：LiteLLM 与本地兜底电表、文档口径、隔离、可选实打冒烟。"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from xskill.bench.cli import dispatch_bench
from xskill.bench.usage import (
    UsageCollector,
    claude_custom_headers_value,
    collect_item_usage,
    contract_usage,
    last_complete_usage_response,
    missing_usage,
    proxy_alive,
    spend_row_matches,
    tagging_headers,
    usage_from_spend_rows,
    write_usage_sidecar,
)
from xskill.cli import build_parser
from xskill.usage import cost_usd, extract_usage, load_price_table

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = ROOT / "benchmarks" / "BENCHMARK_FRAMEWORK_PROPOSAL.md"
UNTOUCHED = {
    "scripts/bench/README.md": "2f3fb4030e0a3eb32be655dd5f994f04db47a9b0041300942a885f9383b8387b",
    "scripts/bench/evaluate.py": "7fa0916005ec820cbeedc1ba6d7c8b5211c56990f8b14c4d3db1840feb14600d",
    "scripts/bench/run_baseline.py": "3f28cac87db835ccbc13202b5fc962fa5fd5e530f0907a89f265fbf4e2b730ba",
    "scripts/bench/officeqa/import_leaderboard_eval.py": "d3a3794daaf084ef5f89b315ddfad35f37ddbd5ecd4e462297be437028699836",
    "scripts/bench/spreadsheet/import_leaderboard_eval.py": "9402c9460d25749b40f33a1a908059354efa6e53e5115c12e6f1a1ee04052c27",
    "scripts/bench/alfworld/import_leaderboard_eval.py": "d8042ff67dca1cc8ad9a5565b3ea40b3207327c06cd941f8b8e5c36d1944474f",
    "scripts/bench/contract_import.py": "9991c0985a757d4e16481f158169d8eac94e613de3f809907e37812616d7bec1",
    "benchmarks/validate.py": "77fa03d47e62621ac9fa9f01b6410d18645142109383c5d8c40cac97c1155cb4",
    "benchmarks/schemas/run_config.schema.json": "38ff0c0dc8d9fece7483d4f9c21144ffa90bf4112bbf1c00434f451f4b3d027a",
    "benchmarks/schemas/summary.schema.json": "fc5a94373598edb973a3ba960847bdcdaa7f9a7541a5b750629aec61b3acf949",
    "benchmarks/schemas/result_record.schema.json": "53baf5a314cbf89023ca108438923977c9387b0b190d88cc4c778a3801b4fb78",
    "benchmarks/schemas/train_provenance.schema.json": "bd94571bad4b409b2150d498c66cc65b273ad9826259e0e45fbf87f9dec12f60",
}

RUN_ID = "eval-officeqa-usage-fixture"
UID = "UID0003"
MODEL = "deepseek-v4-flash"
USAGE_BLOB = {
    "id": "chatcmpl-req-step8",
    "model": MODEL,
    "usage": {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 0,
    },
}


def _sha(rel: str) -> str:
    return hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()


def _skill_dir(tmp_path: Path) -> Path:
    pkg = tmp_path / "skills" / "officeqa-expert"
    pkg.mkdir(parents=True)
    (pkg / "SKILL.md").write_text("---\nname: officeqa-expert\n---\nLook up the number.\n", encoding="utf-8")
    return tmp_path / "skills"


def _same_spend_row() -> dict:
    return {
        "request_id": USAGE_BLOB["id"],
        "model": MODEL,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "request_tags": [
            f"run:{RUN_ID}",
            f"item:{UID}",
            f"X-Run-Id: {RUN_ID}",
            f"x-item-id: {UID}",
        ],
    }


def _expected_cost() -> float:
    extracted = extract_usage(USAGE_BLOB)
    price, _ = load_price_table().resolve(MODEL)
    return float(cost_usd(extracted, price) or 0.0)


def test_proposal_litellm_is_optional_not_required():
    text = PROPOSAL.read_text(encoding="utf-8")
    assert "不是复现精度的必装依赖" in text
    assert "X-Run-Id" in text and "X-Item-Id" in text
    assert "local_fallback" in text
    assert "usage_missing_n" in text
    assert "不能假装花了 0 美元" in text
    assert "import litellm" in text
    assert "每个超参数是干什么的" not in text
    assert "实际跑哪个文件" not in text
    assert "50" in text and "172" in text
    assert "超时计入分母" in text or "超时计入分母，不计入分子" in text


def test_readme_and_untouched_hashes():
    readme = (ROOT / "benchmarks/README.md").read_text(encoding="utf-8")
    assert "litellm_usage.py" in readme
    assert "local_fallback" in readme
    # 超参口径：单一来源、SkillOpt 走官方 train.py、xskill 训练不接命令行默认值。
    assert "hparams.py" in readme
    assert "scripts/train.py" in readme
    assert "minibatch 8" in readme and "patch 改写" in readme
    assert "不接命令行默认值" in readme
    assert "XSKILL_BENCH_MAX_WORKERS" in readme
    # 偏离必须写明，不许悄悄改。
    assert "eval_test" in readme and "env.mode" in readme
    # LiteLLM 口径：两条路由、镜像副本、两条记账通路。
    assert "text-embedding-v4" in readme
    assert "XSKILL_ANTHROPIC_BASE_URL" in readme
    assert "host.docker.internal:host-gateway" in readme
    assert "虚拟 key" in readme
    assert "旧 tag 不覆盖" in readme
    # 冒烟趟与训练用量的口径也得写明，不然读者会把 --limit 当官方成绩。
    assert "smoke_shortened" in readme
    assert "train_usage.json" in readme
    assert "--env-file" in readme
    # 旧的「Chat 改写冒充官方训练」口径必须已经删掉。
    assert "SkillOpt 训练仍用 Chat 改写" not in readme
    splitter = (ROOT / "scripts/bench/README.md").read_text(encoding="utf-8")
    assert "轨迹拆分" in splitter
    assert "officeqa" not in splitter.lower()
    for rel, digest in UNTOUCHED.items():
        assert _sha(rel) == digest, rel


def test_no_litellm_import_in_bench_or_src():
    import xskill.bench.usage  # noqa: F401

    assert "litellm" not in sys.modules
    banned = []
    roots = [ROOT / "src/xskill", ROOT / "scripts/bench/litellm_usage.py", ROOT / "src/xskill/bench"]
    files = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if stripped.startswith("import litellm") or stripped.startswith("from litellm"):
                banned.append(f"{path}:{stripped}")
    assert banned == []


def test_same_blob_litellm_and_fallback_match(tmp_path):
    spend = [_same_spend_row()]
    litellm_usage = collect_item_usage(
        run_id=RUN_ID,
        uid=UID,
        model=MODEL,
        spend_logs=spend,
        mode="litellm",
    )
    logs = tmp_path / "logs"
    write_usage_sidecar(logs, UID, USAGE_BLOB, run_id=RUN_ID, model=MODEL)
    fallback = collect_item_usage(
        run_id=RUN_ID,
        uid=UID,
        model=MODEL,
        logs_dir=logs,
        mode="local_fallback",
    )
    assert litellm_usage["source"] == "litellm"
    assert fallback["source"] == "local_fallback"
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "total_tokens"):
        assert litellm_usage[key] == fallback[key] == USAGE_BLOB["usage"][
            {
                "input_tokens": "prompt_tokens",
                "output_tokens": "completion_tokens",
                "cache_read_tokens": "prompt_cache_hit_tokens",
                "total_tokens": "total_tokens",
            }[key]
        ]
    assert litellm_usage["request_ids"] == fallback["request_ids"] == [USAGE_BLOB["id"]]
    expected = _expected_cost()
    assert abs(litellm_usage["cost_usd"] - expected) < 1e-12
    assert abs(fallback["cost_usd"] - expected) < 1e-12
    missing = collect_item_usage(run_id=RUN_ID, uid=UID, model=MODEL, mode="auto")
    assert missing == missing_usage()


def test_stream_uses_last_usage_block_not_char_count():
    chunks = [
        {"choices": [{"delta": {"content": "hello world"}}]},
        {"id": "chatcmpl-req-step8", "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
    ]
    last = last_complete_usage_response(chunks)
    usage = contract_usage(source="local_fallback", resp=last, model=MODEL)
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["total_tokens"] != len("hello world")


def test_spend_row_match_requires_both_ids():
    row = _same_spend_row()
    assert spend_row_matches(row, RUN_ID, UID)
    assert not spend_row_matches(row, RUN_ID, "UID9999")
    assert not spend_row_matches({"request_tags": [f"run:{RUN_ID}"]}, RUN_ID, UID)


def test_fake_eval_with_spend_fixture_and_sidecar(tmp_path):
    skill = _skill_dir(tmp_path)
    spend_path = tmp_path / "spend.json"
    spend_path.write_text(json.dumps([_same_spend_row()]), encoding="utf-8")
    out = tmp_path / "eval-litellm"
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(out),
            "--limit",
            "1",
            "--run-id",
            RUN_ID,
            "--usage-mode",
            "litellm",
            "--spend-logs",
            str(spend_path),
        ]
    )
    assert dispatch_bench(args) == 0
    row = json.loads((out / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["uid"] == UID
    assert row["usage"]["source"] == "litellm"
    assert row["usage"]["request_ids"] == [USAGE_BLOB["id"]]
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["usage_totals"]["usage_missing_n"] == 0
    assert summary["n_total"] == 1

    out2 = tmp_path / "eval-fallback"
    logs = out2 / "logs"
    logs.mkdir(parents=True)
    write_usage_sidecar(logs, UID, USAGE_BLOB, run_id=RUN_ID, model=MODEL)
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(out2),
            "--limit",
            "1",
            "--run-id",
            RUN_ID,
            "--usage-mode",
            "local_fallback",
        ]
    )
    assert dispatch_bench(args) == 0
    row = json.loads((out2 / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["usage"]["source"] == "local_fallback"
    assert row["status"] in {"pass", "fail", "timeout"}
    assert row["usage"]["input_tokens"] == 100


def test_usage_script_does_not_change_pass_fail(tmp_path):
    skill = _skill_dir(tmp_path)
    out = tmp_path / "plain"
    args = build_parser().parse_args(
        [
            "bench",
            "eval",
            "--benchmark",
            "officeqa",
            "--algo",
            "xskill",
            "--harness",
            "native_cc",
            "--skill-dir",
            str(skill),
            "--output-dir",
            str(out),
            "--limit",
            "3",
        ]
    )
    assert dispatch_bench(args) == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["n_pass"] == 1
    assert summary["status_counts"]["timeout"] == 1
    assert summary["usage_totals"]["usage_missing_n"] == 3


def test_tagging_headers_and_claude_env():
    headers = tagging_headers("run-a", "item-b")
    assert headers["X-Run-Id"] == "run-a"
    assert headers["X-Item-Id"] == "item-b"
    assert "run:run-a" in headers["x-litellm-tags"]
    text = claude_custom_headers_value("run-a", "item-b")
    assert "X-Run-Id: run-a" in text
    assert "X-Item-Id: item-b" in text


def test_litellm_usage_cli_collect_from_fixture(tmp_path):
    spend = tmp_path / "spend.json"
    spend.write_text(json.dumps([_same_spend_row()]), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/bench/litellm_usage.py"),
            "collect",
            "--run-id",
            RUN_ID,
            "--item-id",
            UID,
            "--spend-logs",
            str(spend),
            "--mode",
            "litellm",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    doc = json.loads(proc.stdout)
    assert doc["source"] == "litellm"
    assert doc["request_ids"] == [USAGE_BLOB["id"]]


class _HeaderHandler(BaseHTTPRequestHandler):
    captured: list[dict[str, str]] = []

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length)
        _HeaderHandler.captured.append({k: v for k, v in self.headers.items()})
        body = json.dumps(
            {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        _HeaderHandler.captured.append({k: v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def _serve_header_capture() -> tuple[HTTPServer, int]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    _HeaderHandler.captured = []
    server = HTTPServer(("127.0.0.1", port), _HeaderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _headers_contain_run_item(captured: list[dict[str, str]], run_id: str, item_id: str) -> bool:
    for hdr in captured:
        lowered = {k.lower(): v for k, v in hdr.items()}
        if lowered.get("x-run-id") == run_id and lowered.get("x-item-id") == item_id:
            return True
        tags = lowered.get("x-litellm-tags", "")
        if f"run:{run_id}" in tags and f"item:{item_id}" in tags:
            return True
    return False


def test_host_claude_custom_headers_smoke():
    claude = Path("/home/admin/.nvm/versions/node/v24.14.1/bin/claude")
    if not claude.is_file():
        pytest.skip("host claude missing")
    server, port = _serve_header_capture()
    run_id = "hdr-host-run"
    item_id = "hdr-host-item"
    env = {
        **os.environ,
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
        "ANTHROPIC_AUTH_TOKEN": "dummy",
        "ANTHROPIC_API_KEY": "dummy",
        "ANTHROPIC_CUSTOM_HEADERS": claude_custom_headers_value(run_id, item_id),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    try:
        subprocess.run(
            [str(claude), "-p", "只回 ok", "--output-format", "json", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        pass
    finally:
        server.shutdown()
    assert _HeaderHandler.captured, "host Claude sent no HTTP request to the capture server"
    assert _headers_contain_run_item(_HeaderHandler.captured, run_id, item_id)


@pytest.mark.parametrize(
    "image",
    [
        "localhost:5000/l_creator/officeqa-eval:skillopt-exec-20260904",
        "localhost:5000/l_creator/alfworld-eval:native-cc-20260904",
    ],
)
def test_image_claude_custom_headers_smoke(image):
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("docker not installed")
    if proc.returncode != 0:
        pytest.skip(f"image missing: {image}")
    server, port = _serve_header_capture()
    run_id = "hdr-img-run"
    item_id = "hdr-img-item"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "host",
                "-e",
                f"ANTHROPIC_BASE_URL=http://127.0.0.1:{port}",
                "-e",
                "ANTHROPIC_AUTH_TOKEN=dummy",
                "-e",
                "ANTHROPIC_API_KEY=dummy",
                "-e",
                f"ANTHROPIC_CUSTOM_HEADERS={claude_custom_headers_value(run_id, item_id)}",
                "-e",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
                "--entrypoint",
                "claude",
                image,
                "-p",
                "只回 ok",
                "--output-format",
                "json",
                "--max-turns",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=40,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pass
    finally:
        server.shutdown()
    assert _HeaderHandler.captured, f"{image} Claude sent no HTTP request"
    assert _headers_contain_run_item(_HeaderHandler.captured, run_id, item_id), (
        f"{image} did not forward X-Run-Id / X-Item-Id; captured={_HeaderHandler.captured!r}"
    )


def _load_proxy_env() -> dict[str, str]:
    path = Path("/home/admin/litellm-proxy/.env")
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'").strip('"')
    return out


def test_live_litellm_proxy_probe():
    if not proxy_alive():
        pytest.skip("LiteLLM proxy down")
    env = _load_proxy_env()
    if not env.get("LITELLM_MASTER_KEY"):
        pytest.skip("no master key")
    run_id = "probe-live-litellm"
    item_id = "probe-live-item"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/bench/litellm_usage.py"),
            "probe",
            "--via",
            "proxy",
            "--run-id",
            run_id,
            "--item-id",
            item_id,
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "LITELLM_MASTER_KEY": env["LITELLM_MASTER_KEY"],
        },
    )
    if proc.returncode == 2 and "chat failed" in proc.stderr:
        # 上游限流或抖动不是本仓的问题，别记成失败。
        pytest.skip(f"upstream unavailable: {proc.stderr.strip()}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip().startswith("{")]
    assert lines[0]["http"] == 200
    assert lines[0]["has_usage"] is True
    collected = lines[-1]["collected"]
    assert collected["source"] == "litellm"
    assert collected["input_tokens"] > 0
    assert lines[-1]["tagged_rows"] >= 1


HOST_CLAUDE = Path("/home/admin/.nvm/versions/node/v24.14.1/bin/claude")


def _live_proxy_or_skip() -> str:
    from xskill.bench import proxy

    if not proxy.alive():
        pytest.skip("LiteLLM proxy down")
    if not proxy.master_key():
        pytest.skip("no master key")
    return proxy.proxy_base_url()


def test_live_claude_through_real_proxy_bills_back_as_litellm():
    """Claude Code 走真代理，账再收回来必须是 source=litellm，不是本地兜底。"""
    from xskill.bench import proxy

    base = _live_proxy_or_skip()
    if not HOST_CLAUDE.is_file():
        pytest.skip("host claude missing")
    run_id = f"live-cc-{os.getpid()}"
    item_id = "live-cc-item"
    run_key = proxy.try_create_run_key(run_id, models=[MODEL], duration="1h")
    key = run_key.key if run_key else proxy.master_key()
    env = {
        **os.environ,
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": key,
        "ANTHROPIC_MODEL": MODEL,
        "ANTHROPIC_SMALL_FAST_MODEL": MODEL,
        "ANTHROPIC_CUSTOM_HEADERS": claude_custom_headers_value(run_id, item_id),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "IS_SANDBOX": "1",
    }
    try:
        subprocess.run(
            [str(HOST_CLAUDE), "-p", "只回 ok", "--output-format", "json", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("host claude timed out against the proxy")

    collector = UsageCollector(
        run_id=run_id,
        model=MODEL,
        mode="litellm",
        run_token=run_key.token if run_key else "",
    )
    collector.prime([item_id])
    usage = collector.for_item(item_id)
    assert usage["source"] == "litellm", f"账没收回来: {usage}"
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0
    assert usage["request_ids"], "spend 行没带 request_id"
    assert usage["cost_usd"] is not None

    # 逐题对不上时的兜底通路：按 run 虚拟 key 归账，训练阶段用的就是这条。
    if run_key and run_key.tagged:
        totals = UsageCollector(
            run_id=run_id, model=MODEL, mode="litellm", run_token=run_key.token
        ).run_totals()
        assert totals["source"] == "litellm"
        assert totals["input_tokens"] >= usage["input_tokens"]


def test_live_claude_untagged_still_reaches_proxy_and_bills_by_run_key():
    """加不了头的客户端（镜像里 2.1.178 那种）也必须到代理，并能按 run 收回账。"""
    from xskill.bench import proxy

    base = _live_proxy_or_skip()
    if not HOST_CLAUDE.is_file():
        pytest.skip("host claude missing")
    run_id = f"live-nohdr-{os.getpid()}"
    run_key = proxy.try_create_run_key(run_id, models=[MODEL], duration="1h")
    if not run_key or not run_key.tagged:
        pytest.skip("proxy refused a run virtual key")
    env = {
        **os.environ,
        "ANTHROPIC_BASE_URL": base,
        "ANTHROPIC_AUTH_TOKEN": run_key.key,
        "ANTHROPIC_API_KEY": run_key.key,
        "ANTHROPIC_MODEL": MODEL,
        "ANTHROPIC_SMALL_FAST_MODEL": MODEL,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "IS_SANDBOX": "1",
    }
    env.pop("ANTHROPIC_CUSTOM_HEADERS", None)
    try:
        subprocess.run(
            [str(HOST_CLAUDE), "-p", "只回 ok", "--output-format", "json", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("host claude timed out against the proxy")
    collector = UsageCollector(
        run_id=run_id, model=MODEL, mode="litellm", run_token=run_key.token
    )
    totals = collector.run_totals()
    assert totals["source"] == "litellm", f"无头请求没按 run key 归账: {totals}"
    assert totals["input_tokens"] > 0
    # 没有 item 头时逐题就是对不上，这点必须暴露出来，不许假装对上。
    assert collector.for_item("live-nohdr-item") == missing_usage()


def test_skillopt_optimizer_client_cannot_take_tagging_headers():
    """vendor 的 Chat 客户端写死了 default_headers，所以只能靠 run 虚拟 key 归账。"""
    from xskill.bench.dataset import skillopt_vendor_root

    path = skillopt_vendor_root() / "skillopt/model/azure_openai.py"
    if not path.is_file():
        pytest.skip("vendor client missing")
    text = path.read_text(encoding="utf-8")
    assert 'default_headers={"User-Agent": "SkillOpt"}' in text
    assert "X-Run-Id" not in text
    reason = (ROOT / "src/xskill/bench/proxy.py").read_text(encoding="utf-8")
    assert "default_headers" in reason and "虚拟 key" in reason


def test_live_direct_deepseek_fallback_probe(tmp_path):
    env = _load_proxy_env()
    key = env.get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        pytest.skip("no deepseek key")
    logs = tmp_path / "logs"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/bench/litellm_usage.py"),
            "probe",
            "--via",
            "direct",
            "--run-id",
            "probe-fallback",
            "--item-id",
            "probe-fb-item",
            "--logs-dir",
            str(logs),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "DEEPSEEK_API_KEY": key},
    )
    if proc.returncode == 2 and "chat failed" in proc.stderr:
        pytest.skip(f"upstream unavailable: {proc.stderr.strip()}")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip().startswith("{")]
    assert lines[0]["via"] == "direct"
    assert lines[0]["has_usage"] is True
    collected = lines[-1]["collected"]
    assert collected["source"] == "local_fallback"
    assert collected["input_tokens"] > 0
    sidecar = logs / "probe-fb-item.usage.json"
    assert sidecar.is_file()
    filled = collect_item_usage(
        run_id="probe-fallback",
        uid="probe-fb-item",
        model=MODEL,
        logs_dir=logs,
        mode="local_fallback",
    )
    assert filled["source"] == "local_fallback"
    assert filled["input_tokens"] == collected["input_tokens"]
