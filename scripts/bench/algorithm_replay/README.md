# Atom splitting and routing replay

This suite evaluates immutable, recorded algorithm outputs. It does not call an
LLM, an embedding endpoint, Milvus, or a coding-agent CLI during normal tests.

Run the checked-in baseline:

```bash
python -m scripts.bench.algorithm_replay.evaluate \
  scripts/bench/algorithm_replay/fixtures/baseline_v1.json
```

Use `--format json` for a machine-readable report. The test suite compares that
report with `baseline_v1.report.json`, so metric-definition changes are explicit
and reviewable.

## Fixture contract

The root object contains:

- `schema_version`: currently `1`; unsupported versions fail loudly.
- `metric_config.routing_recall_k`: the candidate cutoff used by Recall@K.
- `metric_config.atom_alignment_min_iou`: the minimum interval IoU used to
  align a prediction with a gold Atom for duplicate and routing metrics.
- `run_manifest`: repository revision, model, harness, prompt fingerprint, seed,
  generation parameters, token counts, cost, and generation time for the
  recorded predictions.
- `skill_catalog`: the only valid routing labels in the suite.
- `cases`: a line-addressable synthetic trajectory, human-authored gold atoms,
  and immutable predicted atoms. `line_count` must exactly match `source_lines`.

Atom ranges are 1-based half-open intervals `[start_line, end_line)`. Gold ranges
must not overlap. Predicted ranges may overlap because overlap is a measured
failure mode. `scorable_ranges` identifies the source lines used by coverage and
overlap metrics.

The checked-in fixture is synthetic and privacy-safe. Its model name is
`recorded-fixture`; it is an evaluator contract, not a claim about current online
model quality. Its prompt fingerprint hashes the literal sentinel
`no-model-prompt:synthetic-baseline-v1`, because no model prompt was used. A real
offline run must replace the run manifest and prediction section while preserving
the same schema.

## Metric definitions

- Boundary precision/recall/F1 compares exact internal Atom start lines. The
  forced start of each scorable range is excluded. When both sides have no
  internal boundary, all three values are `1.0`.
- Pk and WindowDiff reuse the repository's existing, independently tested
  `scripts/bench/evaluate.py` implementations. They expose near-miss and
  over/under-segmentation behavior that exact boundary F1 cannot represent.
- Coverage is the fraction of scorable lines covered by at least one predicted
  Atom. Overlap rate counts repeated predicted coverage over the same denominator.
- Duplicate rate aligns each prediction to the gold Atom with maximum interval
  IoU at or above `atom_alignment_min_iou`, then counts additional predictions
  aligned to an already matched gold Atom.
- Language consistency detects the dominant script of `intent + summary` after
  removing inline code and path-like tokens. An output with no detectable
  natural language is a mismatch, not an excluded sample. Version 1 supports
  English and Chinese fixtures only.
- Routing micro precision/recall/F1 compares `(gold_atom_id, skill)` relations
  after interval alignment. Macro precision/recall/F1 is the unweighted mean of
  per-case scores. Recall@K uses each prediction's ordered `candidates` list.
- Multi-Skill relation retention measures gold relations belonging to atoms with
  more than one expected Skill. It prevents an optimization from collapsing a
  valid one-to-many relation into one label.

Empty-set behavior follows the existing benchmark: precision/recall/F1 is `1.0`
only when true-positive, false-positive, and false-negative counts are all zero.
Duplicate and overlap rates are `0.0` when there is no applicable denominator;
other vacuously satisfied ratios are `1.0`. An unknown detected language still
has a denominator and therefore scores as a mismatch.

Do not turn a metric into a blocking quality threshold until a maintainer has
reviewed a representative recorded baseline. Deterministic schema and metric
tests remain blocking regardless of model quality.
