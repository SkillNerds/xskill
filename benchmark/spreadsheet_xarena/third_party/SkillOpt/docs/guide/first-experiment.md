# Your First Experiment

This guide walks through running a complete SkillOpt training on SearchQA.

## 1. Choose a Benchmark

SkillOpt includes ready-to-use configs for several benchmarks:

| Benchmark | Difficulty | Typical Runtime |
|---|---|---|
| SearchQA | ⭐ Easy | ~30 min |
| DocVQA | ⭐⭐ Medium | ~2 hours |
| ALFWorld | ⭐⭐⭐ Hard | ~3 hours |

We'll use **SearchQA** as it's the fastest to complete.

## 2. Configure

Review the config file:

```bash
cat configs/searchqa/default.yaml
```

Key parameters (deep learning analogy in parentheses):

```yaml
train:
  num_epochs: 4           # (epochs)
  batch_size: 40          # (batch size)

optimizer:
  learning_rate: 4        # (max edits per step)
  lr_scheduler: cosine    # (learning rate schedule)
  use_slow_update: true   # (momentum at epoch boundary)
  use_meta_skill: true    # (cross-epoch optimizer memory)

gradient:
  analyst_workers: 16     # (parallel reflection workers)

evaluation:
  use_gate: true          # (validation gating)
```

## 3. Train

```bash
python scripts/train.py --config configs/searchqa/default.yaml
```

You'll see output like:

```
[Step 1/8] Rollout: 20 items, 4 workers...
[Step 1/8] Score: 0.65 → Reflect...
[Step 1/8] 6 edit patches generated
[Step 1/8] Selected 4 edits (lr=8, cosine → 7.7)
[Step 1/8] Gate: val score 0.68 > 0.65 ✓ ACCEPT
[Step 2/8] ...
```

## 4. Monitor

Training outputs are saved to `outputs/<benchmark>/<run_id>/`:

```
outputs/searchqa/2024-01-15_10-30-00/
├── steps/
│   ├── step_0001/
│   │   ├── candidate_skill.md
│   │   ├── step_record.json
│   │   └── trajectory_digest.json
│   └── step_0002/
├── slow_update/
│   └── epoch_02/
├── meta_skill/
│   └── epoch_02/
├── skills/
│   └── step_0001.md
├── best_skill.md
├── history.json
└── config.yaml
```

## 5. Evaluate

Evaluate the best skill on the test split:

```bash
python scripts/eval_only.py \
  --config configs/searchqa/default.yaml \
  --skill outputs/searchqa/<run_id>/skills/best_skill.md
```

## WebUI

Prefer a graphical interface? Launch the WebUI:

```bash
pip install -e ".[webui]"
python -m skillopt_webui.app
```

Then open `http://localhost:7860` in your browser to configure parameters and launch training.

## Next Steps

- [Understand the training loop](training-loop.md)
- [Configuration reference](../reference/config.md)
- [Add a new benchmark](new-benchmark.md)
