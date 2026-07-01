# Deep Learning ↔ SkillOpt Analogy

SkillOpt is designed around a core insight: **optimizing natural-language prompts follows the same structure as training neural networks**. This page maps every DL concept to its SkillOpt counterpart.

## Complete Mapping

| Deep Learning | SkillOpt | Description |
|---|---|---|
| **Model weights** | Skill document (Markdown) | The thing being optimized |
| **Forward pass** | Rollout | Target executes tasks using current skill |
| **Loss function** | Task evaluator | Scores task execution quality |
| **Backpropagation** | Reflect | Optimizer analyzes failures → edit patches |
| **Gradients** | Edit patches | Proposed changes to the skill |
| **Gradient aggregation** | Patch aggregation | Merge similar edits |
| **Gradient clipping** | Edit selection | Cap max edits per step |
| **Learning rate** | `learning_rate` | Max number of edits applied per step |
| **LR scheduler** | `lr_scheduler` | Decay schedule: cosine, linear, constant |
| **SGD step** | Skill update | Apply selected patches to document |
| **Validation set** | Selection split | Gate checks improvement before accepting |
| **Early stopping** | Gate patience | Reject updates that don't improve |
| **Training step** | Step | One rollout → reflect → update cycle |
| **Epoch** | Epoch | Full pass with slow update + meta memory |
| **Momentum** | Slow update | Longitudinal comparison at epoch boundary |
| **Meta-learning** | Meta skill | Cross-epoch optimizer strategy memory |
| **Batch size** | `batch_size` | Tasks sampled per rollout |
| **Data parallelism** | `analyst_workers` | Parallel reflection workers |
| **Training set** | Train split | Items used for rollout |
| **Test set** | Test split | Held-out final evaluation |
| **Warm-up** | (implicit) | High LR early steps explore broadly |
| **Checkpointing** | Skill snapshots | Saved after each accepted step |
| **Transfer learning** | Seed skill / cross-benchmark init | Start from pre-trained skill |

## Why This Analogy Matters

1. **Familiar mental model**: ML practitioners immediately understand how to tune SkillOpt
2. **Principled hyperparameter search**: Grid search over `learning_rate` × `lr_scheduler` works just like in DL
3. **Proven mechanisms**: Gating ≈ validation-based selection, patience ≈ early stopping, slow update ≈ momentum — all with strong theoretical motivation

## Hyperparameter Transfer Rules

From our experiments, these DL intuitions transfer well:

!!! success "What transfers"
    - **Cosine schedule > constant** — same as in DL, cosine annealing helps convergence
    - **Moderate LR (4-16) > very high/low** — too few edits = slow learning, too many = noisy
    - **Slow update helps** — longitudinal comparison prevents catastrophic forgetting across epochs
    - **Meta skill memory improves reflection** — optimizer benefits from cross-epoch strategy notes

!!! warning "What doesn't transfer"
    - **Batch size ≠ better** — larger rollout batches have diminishing returns due to API costs
    - **More epochs ≠ better** — skills converge faster than neural networks (2-4 epochs usually enough)
