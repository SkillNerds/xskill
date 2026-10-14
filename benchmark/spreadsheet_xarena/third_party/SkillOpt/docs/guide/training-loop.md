# The Training Loop

SkillOpt's core insight: **optimizing natural-language skill documents follows the same structure as training neural networks**.

## Overview

```
┌─────────────────────────────────────────────────────────┐
│                    Training Loop                         │
│                                                         │
│  for epoch in epochs:                                   │
│    for step in steps:                                   │
│      1. Rollout   — Target executes tasks              │
│      2. Reflect   — Optimizer analyzes trajectories       │
│      3. Aggregate — Hierarchical merge of patches       │
│      4. Select    — Rank & clip edits (learning rate)   │
│      5. Update    — Apply patches to skill doc          │
│      6. Gate      — Validate & accept/reject            │
│                                                         │
│    Epoch Boundary:                                       │
│      • Slow Update (longitudinal comparison & guidance) │
│      • Meta Skill  (cross-epoch strategy memory)        │
└─────────────────────────────────────────────────────────┘
```

## Stage Details

### 1. Rollout (Forward Pass)

The **target** model executes tasks using the current skill document as its prompt. Each task produces a trajectory and a score.

```python
# Analogy: forward pass through the network
predictions = model(input, skill_document)
scores = evaluate(predictions, ground_truth)
```

### 2. Reflect (Backward Pass)

The **optimizer** model analyzes failed trajectories and produces **edit patches** — structured suggestions for improving the skill document.

Two modes:

- **Shallow**: Analyze each trajectory independently
- **Deep**: Cross-reference multiple failures to find systemic issues

```python
# Analogy: computing gradients
gradients = loss.backward()  # → edit patches
```

### 3. Aggregate

Semantically similar edit patches are merged to avoid redundant edits.

### 4. Select (Gradient Clipping)

Edits are ranked by relevance score. The `learning_rate` parameter caps how many edits are applied per step — just like gradient clipping prevents overshooting.

```python
# Analogy: gradient clipping + optimizer step size
selected = top_k(edits, k=learning_rate)
```

The `lr_scheduler` adjusts this over training:

- **cosine**: Start aggressive, taper smoothly
- **linear**: Linear decay
- **constant**: Fixed rate

### 5. Update (Parameter Update)

Selected edits are applied to the skill document, producing a new version.

### 6. Gate (Validation)

The updated skill is evaluated on a **selection split** (analogous to a validation set). The update is only accepted if performance improves.

## Epoch Boundary Mechanisms

### Slow Update

At the end of each epoch (starting from epoch 2), the system performs a **longitudinal comparison**: it rolls out both the previous epoch's skill and the current skill on the same samples, categorizes items as improved/regressed/persistent_fail/stable_success, then generates high-level **guidance** that is injected into the skill document. This prevents catastrophic forgetting of earlier improvements.

### Meta Skill

A **meta-skill memory** accumulates high-level strategy notes across the entire training run. At the end of each epoch, the optimizer reflects on what changed between epochs and produces a compact memory that is provided as additional context during future reflection steps.

## Next Steps

- [Understand Skill Documents](skill-document.md)
- [DL ↔ SkillOpt analogy table](dl-analogy.md)
