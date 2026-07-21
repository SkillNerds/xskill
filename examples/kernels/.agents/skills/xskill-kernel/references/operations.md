# Kernel evaluation and operations

## Contents

- [Discovery diagnostics](#discovery-diagnostics)
- [Dataset evaluation](#dataset-evaluation)
- [External benchmark evaluator](#external-benchmark-evaluator)
- [Artifact review](#artifact-review)
- [Production handoff](#production-handoff)
- [Failure triage](#failure-triage)

## Discovery diagnostics

From the project Skill directory:

```bash
python scripts/diagnose_kernel.py \
  --kernel <kernel-id>
```

The command imports the implementation and prints resolved metadata, supported triggers,
private configuration path, workspace, and any dependency error. It does not run the
algorithm or change the active production kernel.

## Dataset evaluation

```bash
xskill eval \
  --kernel <kernel-id> \
  --dataset <directory-containing-traj-files> \
  --sample 0.25 \
  --seed 42
```

`--sample` is a floating-point ratio in `(0, 1]`. The same relative-path set and seed
select the same files; hashes of the selected contents determine the dataset identity.
Use `--json` in CI and `--output <dir>` for a fixed artifact destination.

This command alone is a kernel contract smoke test. It does not produce an
independent quality score.

## External benchmark evaluator

Run a provider-owned evaluator automatically after the kernel:

```bash
xskill eval \
  --kernel <kernel-id> \
  --dataset <trajectory-dataset-dir> \
  --benchmark <evaluator-dir>/benchmark.json
```

The trajectory dataset is the kernel's distillation input. The benchmark
manifest separately evaluates the Skills produced by that run. It contains
a command list and timeout. XSkill executes the command without a shell from
the manifest directory and supplies Skills, artifact, and result paths through
documented placeholders and environment variables.

The evaluator writes schema-versioned metric rows with dataset, split, score,
passed, total, and source. XSkill checks types, ranges, duplicate IDs, and that
score equals `passed / total * 100`. The evaluator owns its datasets, models,
credentials, scoring logic, and any container or remote-service integration.

## Artifact review

Check:

- `run.json`: identity, input hash, status, and timestamps;
- `input/selection.json`: exact selected files and hashes;
- `events.jsonl`: phase progress;
- `result.json`: processed count, Skill names, duration, and redacted provider metrics;
- `skills/`: isolated published bundles and Git state;
- `kernel/workspace/`: provider cursor, cache, and intermediate output;
- `kernel_runs.db`: isolated raw run record;
- `benchmarks/<id>/`: manifest copy, evaluator log, and standard metric result when requested.

Do not interpret provider metrics as held-out quality or user satisfaction unless a
separate evaluator produced those values.

## Production handoff

Provide the business operator with:

- the pinned algorithm package and `kernel.py` implementation;
- `config.yaml.example` and secret provisioning instructions;
- the evaluation artifact directory and dataset identity;
- expected resource usage, rollback version, and observation window.

The operator activates the kernel from the XSkill dashboard or configuration. After a
representative observation period, the operator opens the Algorithm Kernel page and clicks
"Export current kernel evaluation JSON". The report contains raw kernel run records,
version-bound Skill UX events, and Canary decisions. Compare sample counts, failures,
latency, output volume, UX averages, and promotion/rejection outcomes together.
The run list and run summary cover at most the latest 500 runs; split a longer observation
period into shorter export windows.

## Failure triage

| Symptom | Check |
| --- | --- |
| Kernel unavailable | Run `diagnose_kernel.py`; verify dependency import and directory ID. |
| Evaluation trigger rejected | Add `evaluation` to `KernelMetadata.triggers`. |
| No inputs processed | Inspect `input/selection.json`, metadata filters, and provider cursor. |
| Existing Skill update rejected | Check stale checkout or active staging; re-checkout current main. |
| Production result differs | Compare dataset, model, provider config, harness, seed, and resource limits. |
| Secret appears in output | Stop sharing artifacts, rotate the credential, and remove it from provider metrics/files. |
