---
name: xskill-kernel
description: Build, integrate, evaluate, and diagnose XSkill trajectory-to-Skill algorithm kernels. Use when an engineer asks an agent to create a kernel.py implementation, adapt an algorithm SDK such as SkillOpt, inspect KernelContext capabilities, run xskill eval, analyze evaluation artifacts, prepare a production handoff, or diagnose why a kernel is unavailable or failed.
---

# XSkill Kernel

Implement and evaluate an algorithm kernel without changing the active production kernel.

## Workflow

1. From the `examples/kernels` working directory, read `MAINTAINER_NOTES.md`, then read `README.md` and this Skill's `references/api.md` and `references/operations.md` as needed.
2. Copy `your-demo-algo-kernel/`. If the template is unavailable, create `<plugin-root>/<kernel-id>/kernel.py` from the implementation skeleton in `references/api.md`, plus a `config.yaml.example`.
3. Set `KernelMetadata.id` to the directory name and keep the implementation in `kernel.py`.
4. Import the provider SDK in `kernel.py`; keep provider configuration in its own `config.yaml`.
5. Read inputs from `context.trajectories`, keep all intermediate state in `context.workspace`, and publish only through `context.publisher`.
6. Run an isolated evaluation before proposing production activation:

   ```bash
   xskill eval \
     --kernel <kernel-id> \
     --dataset <trajectory-dataset-dir> \
     --sample 1.0
   ```

7. If the algorithm provider supplies a trusted evaluator, pass its `benchmark.json` with `--benchmark`. Keep evaluator data, configuration, and scoring logic provider-owned.
8. Inspect `run.json`, `result.json`, `events.jsonl`, the isolated `skills/`, benchmark evidence, and the kernel workspace in the reported artifact directory.
9. Report operational metrics separately from external benchmark quality and online user UX.

## Guardrails

- Treat trajectory paths and existing Skill paths as read-only.
- Use `context.trajectories.directories()` for `rg`, `find`, DuckDB, or large batch readers.
- Use `context.skills.checkout()` before editing an existing Skill, then submit it with `context.publisher.submit_checkout()`.
- Do not copy provider secrets into evaluation artifacts or return them in metrics.
- Do not switch `kernel.active`, start production services, or modify live Skill repositories unless the user explicitly requests production activation.
- Do not claim online parity for an adapter that consumes a benchmark format instead of XSkill trajectories.
- Do not couple public XSkill code or documentation to private benchmark systems inspected during development.

## Resources

- Read [references/api.md](references/api.md) when writing `kernel.py` or using Context, trajectory, Skill, and publication objects.
- Read [references/operations.md](references/operations.md) when evaluating, diagnosing, or preparing a production handoff.
- Run `scripts/diagnose_kernel.py` from this Skill directory to check discovery, dependency imports, metadata, triggers, and resolved paths without executing the kernel. It defaults to `~/.xskill/kernels`.
