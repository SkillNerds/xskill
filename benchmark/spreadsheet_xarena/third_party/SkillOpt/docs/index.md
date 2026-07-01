---
hide:
  - navigation
---

<div class="hero" markdown>

# SkillOpt

### Train Agent Skills Like Neural Networks

*Optimize natural-language skill documents through iterative rollout, reflection, and gated validation — with epochs, learning rates, and validation gates — without touching model weights.*

[Get Started :material-rocket-launch:](guide/installation.md){ .md-button .md-button--primary }
[View on GitHub :material-github:](https://github.com/microsoft/SkillOpt){ .md-button }

</div>

---

## How It Works

<div class="pipeline-container" markdown>
<div class="pipeline-wrapper">

<div class="pipeline-stage" id="stage-rollout">
<div class="stage-icon">🎯</div>
<div class="stage-label">Rollout</div>
<div class="stage-desc">Target executes tasks</div>
</div>

<div class="pipeline-arrow"><div class="flow-line"></div></div>

<div class="pipeline-stage" id="stage-reflect">
<div class="stage-icon">🔍</div>
<div class="stage-label">Reflect</div>
<div class="stage-desc">Optimizer analyzes trajectories</div>
</div>

<div class="pipeline-arrow"><div class="flow-line"></div></div>

<div class="pipeline-stage" id="stage-aggregate">
<div class="stage-icon">🔗</div>
<div class="stage-label">Aggregate</div>
<div class="stage-desc">Merge edit patches</div>
</div>

<div class="pipeline-arrow"><div class="flow-line"></div></div>

<div class="pipeline-stage" id="stage-select">
<div class="stage-icon">✂️</div>
<div class="stage-label">Select</div>
<div class="stage-desc">Rank & clip edits</div>
</div>

<div class="pipeline-arrow"><div class="flow-line"></div></div>

<div class="pipeline-stage" id="stage-update">
<div class="stage-icon">📝</div>
<div class="stage-label">Update</div>
<div class="stage-desc">Apply to skill doc</div>
</div>

<div class="pipeline-arrow"><div class="flow-line"></div></div>

<div class="pipeline-stage" id="stage-gate">
<div class="stage-icon">🚦</div>
<div class="stage-label">Gate</div>
<div class="stage-desc">Validate & accept</div>
</div>

</div>

<div class="pipeline-epoch-bar">
<div class="epoch-mechanism">🔄 Slow Update</div>
<div class="epoch-mechanism">🧠 Meta Skill</div>
<div class="epoch-label">Epoch Boundary</div>
</div>

</div>

---

## Deep Learning Analogy

SkillOpt brings the familiar deep-learning training paradigm to agentic prompt optimization:

| Deep Learning | SkillOpt | 
|---|---|
| Model weights | Skill document (Markdown) |
| Forward pass | Rollout (target executes tasks) |
| Loss / gradient | Reflect (optimizer produces edit patches) |
| Gradient clipping | Edit selection (`learning_rate` = max edits) |
| SGD step | Patch application to skill |
| Validation set | Gated evaluation on selection split |
| LR schedule | `lr_scheduler`: cosine, linear, constant |
| Epochs | Multi-epoch with slow update & meta skill memory |

---

## Supported Benchmarks

| Benchmark | Type | Config |
|---|---|---|
| **DocVQA** | Document QA | `configs/docvqa/` |
| **ALFWorld** | Embodied AI | `configs/alfworld/` |
| **OfficeQA** | Enterprise QA | `configs/officeqa/` |
| **SearchQA** | Open-domain QA | `configs/searchqa/` |
| **LiveMathBench** | Math reasoning | `configs/livemathematicianbench/` |
| **SWEBench** | Software Engineering | `configs/swebench/` |
| + 5 more | Various | See [docs](guide/first-experiment.md) |

---

## Quick Example

```bash
# Install
pip install -e .

# Configure credentials
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_OPENAI_API_KEY="your-key"

# Train on SearchQA
python scripts/train.py --config configs/searchqa/default.yaml

# Evaluate best skill
python scripts/eval_only.py \
  --config configs/searchqa/default.yaml \
  --skill outputs/best_skill.md
```

---

<div class="grid cards" markdown>

-   :material-book-open-variant:{ .lg .middle } **Getting Started**

    ---

    Install SkillOpt, configure your API keys, and run your first experiment in 5 minutes.

    [:octicons-arrow-right-24: Installation](guide/installation.md)

-   :material-puzzle:{ .lg .middle } **Add a Benchmark**

    ---

    Extend SkillOpt with your own benchmark in ~100 lines of code.

    [:octicons-arrow-right-24: Extension Guide](guide/new-benchmark.md)

-   :material-cog:{ .lg .middle } **Configuration**

    ---

    Full reference for all hyperparameters with deep learning analogies.

    [:octicons-arrow-right-24: Config Reference](reference/config.md)

-   :material-monitor-dashboard:{ .lg .middle } **WebUI**

    ---

    Configure, launch, and monitor training from your browser.

    [:octicons-arrow-right-24: WebUI Guide](guide/first-experiment.md#webui)

</div>
