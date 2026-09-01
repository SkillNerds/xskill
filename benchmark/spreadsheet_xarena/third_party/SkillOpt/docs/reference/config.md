# Configuration Reference

Complete reference for all SkillOpt configuration parameters.

## Model

| Parameter | Type | Default | Description |
|---|---|---|---|
| `model.backend` | str | `azure_openai` | Backend: `azure_openai` / `openai_chat` / `claude_code_exec` / `qwen` |
| `model.optimizer` | str | `gpt-5.5` | Optimizer model (for reflection & slow update) |
| `model.target` | str | `gpt-5.5` | Target model (for rollout execution) |
| `model.reasoning_effort` | str | `medium` | Reasoning effort level |
| `model.optimizer_backend` | str | `openai_chat` | Optimizer backend: `openai_chat` / `claude_chat` / `qwen_chat` / `minimax_chat` |
| `model.target_backend` | str | `openai_chat` | Target backend: chat backends plus execution harnesses |
| `model.qwen_chat_base_url` | str | `http://localhost:8000/v1` | Shared Qwen/vLLM OpenAI-compatible endpoint |
| `model.qwen_chat_enable_thinking` | bool | `false` | Shared Qwen thinking flag |
| `model.optimizer_qwen_chat_base_url` | str | — | Optimizer-specific Qwen/vLLM endpoint; overrides shared `qwen_chat_base_url` |
| `model.target_qwen_chat_base_url` | str | — | Target-specific Qwen/vLLM endpoint; overrides shared `qwen_chat_base_url` |

## Training (`train`)

| Parameter | Type | Default | DL Analogy | Description |
|---|---|---|---|---|
| `train.num_epochs` | int | 4 | Epochs | Number of training epochs |
| `train.batch_size` | int | 40 | Batch size | Tasks sampled per step |
| `train.accumulation` | int | 1 | Gradient accumulation | Accumulation rounds per step |
| `train.seed` | int | 42 | Random seed | Reproducibility seed |

## Gradient / Reflection (`gradient`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `gradient.minibatch_size` | int | 8 | Reflect minibatch size |
| `gradient.merge_batch_size` | int | 8 | Patch merge batch size |
| `gradient.analyst_workers` | int | 16 | Parallel reflection workers |
| `gradient.max_analyst_rounds` | int | 3 | Max rounds of analyst reflection |
| `gradient.failure_only` | bool | `false` | Only reflect on failures |

## Optimizer (`optimizer`)

| Parameter | Type | Default | DL Analogy | Description |
|---|---|---|---|---|
| `optimizer.learning_rate` | int | 4 | Learning rate | Max edit patches per step (edit budget) |
| `optimizer.min_learning_rate` | int | 2 | Min LR | Min edits for decay schedulers |
| `optimizer.lr_scheduler` | str | `cosine` | LR schedule | `constant` / `linear` / `cosine` / `autonomous` |
| `optimizer.skill_update_mode` | str | `patch` | — | `patch` / `rewrite_from_suggestions` / `full_rewrite_minibatch` |
| `optimizer.use_slow_update` | bool | `true` | Momentum | Epoch-boundary longitudinal comparison & guidance |
| `optimizer.slow_update_samples` | int | 20 | — | Samples for slow update evaluation |
| `optimizer.use_meta_skill` | bool | `true` | Meta-learning | Cross-epoch optimizer-side strategy memory |
| `optimizer.longitudinal_pair_policy` | str | `mixed` | — | `mixed` / `changed` / `unchanged` |

## Evaluation (`evaluation`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `evaluation.use_gate` | bool | `true` | Enable validation gating (accept/reject updates) |
| `evaluation.eval_test` | bool | `true` | Run test evaluation after training |

## Environment (`env`)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `env.name` | str | — | Benchmark name (e.g., `searchqa`, `docvqa`) |
| `env.data_path` | str | — | Path to dataset |
| `env.skill_init` | str | — | Path to initial seed skill (optional) |
| `env.split_mode` | str | `ratio` | `ratio` or `split_dir` |
| `env.split_ratio` | str | `2:1:7` | Train:val:test ratio |
| `env.exec_timeout` | int | 120 | Per-task timeout in seconds |
| `env.out_root` | str | — | Output directory |

## Azure OpenAI Credentials

| Variable | Description |
|---|---|
| `AZURE_OPENAI_ENDPOINT` / `model.azure_openai_endpoint` | Azure resource endpoint |
| `AZURE_OPENAI_API_KEY` / `model.azure_openai_api_key` | Azure API key |
| `OPENAI_API_KEY` | OpenAI API key (for `openai_chat` backend) |
| `ANTHROPIC_API_KEY` | Anthropic API key (for `claude_code_exec` backend) |
| `QWEN_CHAT_BASE_URL` | Shared local vLLM endpoint for `qwen_chat` |
| `QWEN_CHAT_MODEL` | Shared served model name for `qwen_chat` |
| `QWEN_CHAT_API_KEY` | Optional API key for the shared Qwen endpoint |
| `OPTIMIZER_QWEN_CHAT_BASE_URL` | Optimizer-specific local vLLM endpoint |
| `OPTIMIZER_QWEN_CHAT_MODEL` | Optimizer-specific served model name |
| `TARGET_QWEN_CHAT_BASE_URL` | Target-specific local vLLM endpoint |
| `TARGET_QWEN_CHAT_MODEL` | Target-specific served model name |
