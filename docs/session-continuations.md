# Session continuations without rewriting Atoms

A session may grow after its last Atom has already been embedded or used to edit
Skills. If the new text contains only an assistant answer or tool result, it is
still evidence for the preceding objective. It must not disappear from Task
ranges or be included in the next objective's Atom.

For example, after “inspect the logs”, a tool reports “validation passed”. The
splitter records this tail against the log-inspection Atom. When the next user
asks to translate a poem, that new Atom starts at the new User boundary. The log
result remains attached to the previous objective.

## Storage and processing

The original Atom's body, line boundaries, intent and summary stay unchanged.
A separate `continuations/<hashed-atom-id>.json` file beside `tasks/` records a
schema version, owning Atom, half-open line range and content fingerprint. This
is the current cumulative continuation revision, atomically replaced as the
session grows. It contains no copied conversation text. Previous Task Graph
generations retain their own locators and content hashes; this file is not an
append-only history archive.

The splitter advances past the verified continuation, without invoking the model
or returning another Atom for an assistant/tool-only append. If a new User turn
arrives in the same update, the continuation stops before that turn. A whitespace-only gap before a new User
keeps the legacy Atom boundary; it does not create a continuation just for spacing.
As with the
existing splitter, execution is serialized per trajectory by the pipeline.

Task collection validates the original Atom text and the continuation fingerprint
against the session. It constructs an evidence-only view with the extended range;
this view is never saved as an Atom or added to the embedding/Skill contribution
queue. A changed or truncated source is rejected for review, rather than silently
attaching different text. Existing source collection failure handling retains the
dirty source for retry.

## Revision and downstream behavior

The pipeline's existing successful-split callback marks Task Graph dirty even
when zero new Atoms were returned. Collection includes the extended range and
content hash in `source_revision`. The next generation therefore changes when
the evidence changes while retaining the same Atom identity and, in the simple
single-attempt replay, the same Task and Attempt identities. Re-reading the same
session after restart does not add another Atom or another contribution.

Task-grounded learning must key its work on the new evidence revision, invalidate
older candidate evidence, and reject stale candidate promotion. This change
supplies the revised Task evidence; it does not re-open historical Skill edits or
complete the Task-to-Skill learning queue. Existing Atom-based Skill processing
continues to use the original immutable Atom. Real Skill production still needs
baseline/candidate validation before production acceptance.

## Verification

Synthetic replays cover assistant-only and tool-only tails, a tail arriving with
a new User turn, restart/idempotency, unchanged persisted Atom bytes, Task evidence
coverage, stable identities, revision changes and rejection of changed sources.
They use a model test double for splitting; Task collection and storage are real.
These tests are not evidence of model quality or real Skill production.
