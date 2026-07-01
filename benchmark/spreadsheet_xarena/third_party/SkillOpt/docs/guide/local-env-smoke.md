# Local Environment Smoke Tests

This guide describes a lightweight pattern for testing a custom SkillOpt environment before connecting it to expensive model calls or a full benchmark dataset.

The goal is to validate the training loop plumbing first:

- config loading
- adapter construction
- dataloader splits
- rollout output shape
- reflection patch shape
- merge/rank/update control flow
- artifact creation under `out_root`

Once those are stable, you can switch the same environment to real model calls and larger evaluation splits.

## 1. Add a tiny fixture split

Start with a handful of deterministic examples that cover the expected pass/fail cases for your environment. Keep them small enough that a single training step can run locally.

A minimal fixture item usually needs:

```json
{
  "id": "example-1",
  "split": "train",
  "question": "...",
  "expected": "..."
}
```

Use the split names your adapter maps to SkillOpt phases:

- `train` for optimization rollouts
- `val` or `valid_seen` for selection/gating
- `test` or `valid_unseen` for final evaluation

## 2. Support an offline mock mode

Add a configuration flag such as `mock: true` to your adapter. In mock mode, `rollout()` should return deterministic responses without calling external model APIs.

This lets you verify the SkillOpt loop with a fast command such as:

```bash
python scripts/train.py \
  --config configs/myenv/tiny_mock.yaml
```

Mock mode should still write the same artifacts as a real run, for example:

- `responses.json`
- `rollout_results.json`
- `ranked_edits.json`
- `candidate_skill.md`
- `summary.json`

## 3. Keep the smoke config tiny

A CI-friendly smoke config should run a single small step:

```yaml
train:
  num_epochs: 1
  train_size: 3
  batch_size: 3

gradient:
  minibatch_size: 1
  merge_batch_size: 2
  analyst_workers: 1
  max_analyst_rounds: 1

optimizer:
  learning_rate: 1
  min_learning_rate: 1
  lr_scheduler: constant
  skill_update_mode: patch
  use_slow_update: false

evaluation:
  use_gate: true
  sel_env_num: 2
  test_env_num: 2
  eval_test: false

env:
  name: myenv
  out_root: outputs/myenv_tiny_mock
  mock: true
```

Prefer a mock config that runs without credentials. That makes it useful for contributors and CI.

## 4. Validate optimizer JSON before returning it

If your environment or extension asks an LLM to merge or rank skill edits, validate the returned JSON before passing it back into SkillOpt. This avoids silent fallbacks from empty, malformed, or out-of-range responses.

Useful checks for edit payloads:

- response is a JSON object
- `edits` is a non-empty list
- every edit is an object
- every edit has an allowed operation
- required fields such as `content` or `target` are present for that operation

Useful checks for ranking payloads:

- `selected_indices` exists
- indices are integers
- indices are unique
- indices are within the candidate edit range
- selected count does not exceed the edit budget

On failure, retry with a compact prompt that includes the schema error. If retries fail, raise an explicit error instead of silently accepting malformed output.

## 5. Run progressively stronger checks

A good development sequence is:

```bash
python -m py_compile scripts/train.py skillopt/envs/myenv/adapter.py
python scripts/train.py --config configs/myenv/tiny_mock.yaml
python scripts/train.py --config configs/myenv/tiny.yaml
```

For the real tiny run, verify that:

- the run completes
- `summary.json` is written
- `ranked_edits.json` contains the expected ranking metadata
- any optimizer bridge log marks the response schema as valid
- no generated files are written outside `out_root`

## 6. Keep custom environments isolated

When adding a custom environment to the registry, avoid side effects for existing benchmarks:

- lazy-import optional dependencies
- install environment-specific hooks only when `cfg["env"]` matches your environment
- keep mock behavior behind an explicit config flag
- write generated artifacts only under `out_root`

This makes it easier to review and test a custom integration without affecting the built-in benchmarks.
