## ADDED Requirements

### Requirement: SkillFeature fuses description + tags + last5 atom summaries

A skill's vector feature SHALL be a single fused vector combining up to three sources:
the SKILL.md `description` embedding, the mean of frontmatter `metadata.tags` embeddings,
and the mean of the embeddings of the most recent N (default 5) trajectory-atom summaries
that were routed to this skill. The fused vector SHALL be L2-normalized. When a source is
absent (no tags, no atoms), that source SHALL be excluded from the fusion — this is part
of the feature definition, not a runtime fallback.

#### Scenario: Full feature fusion

- **WHEN** a skill has a description, two tags, and five recent atoms
- **THEN** `Skill.feature.vec` SHALL be `normalize(embed(description) + mean(embed(tags)) + mean(embed(last5_atom_summaries)))`

#### Scenario: Cold-start skill with no atoms

- **WHEN** a skill has a description and tags but no routed atoms yet
- **THEN** `Skill.feature.vec` SHALL be `normalize(embed(description) + mean(embed(tags)))`
- **AND** SHALL NOT throw

#### Scenario: Skill with only description

- **WHEN** a skill has only a description (no tags, no atoms)
- **THEN** `Skill.feature.vec` SHALL equal `normalize(embed(description))`

### Requirement: `Skill.vec` property is lazy

`Skill` SHALL expose a `vec` property that lazily computes (or reads from
`.skill_index.pkl`) the fused feature vector on first access and caches it on the instance.
Accessing `vec` SHALL NOT trigger a full index rebuild.

#### Scenario: vec computed once and cached

- **WHEN** `Skill.vec` is accessed twice on the same instance
- **THEN** the embedding SHALL be computed at most once
- **AND** the second access SHALL return the cached vector

### Requirement: `Skill.skill_meta` is a version view

`Skill` SHALL expose a `skill_meta` property returning a view
`{"main": {"git_hash": str, "used_ux_scores": [int,...]}, "staging": {...} | None, "baby": "hash" | None}`.
`used_ux_scores` SHALL be the recent UX scores for that side+sha. This is a read-only view
over existing git state + `.ux_scores.jsonl`, not an independent persisted object.

#### Scenario: skill_meta reflects staging presence

- **WHEN** a skill has a `staging` branch
- **THEN** `Skill.skill_meta["staging"]` SHALL be `{"git_hash": <staging_sha>, "used_ux_scores": [...]}`
- **AND** `Skill.skill_meta["main"]` SHALL be `{"git_hash": <main_sha>, "used_ux_scores": [...]}`

#### Scenario: skill_meta staging None when no staging

- **WHEN** a skill has no `staging` branch
- **THEN** `Skill.skill_meta["staging"]` SHALL be `None`

### Requirement: rebuild_skill_index fuses full feature set

`rebuild_skill_index` SHALL, for each distributable skill, build the fused feature
(description + tags + last5 atom summaries) and store the resulting matrix in
`.skill_index.pkl` alongside `skill_names`. The index file schema SHALL remain
`{"skill_names": [...], "embeddings": np.ndarray(N, D) L2-normalized, ...}` for backward
compatibility with existing cosine retrieval.

#### Scenario: Rebuild produces fused embeddings

- **WHEN** `rebuild_skill_index` runs on a skill repo where skills have descriptions, tags,
  and routed atoms
- **THEN** `.skill_index.pkl["embeddings"]` SHALL contain the fused (not description-only)
  vectors
- **AND** each row SHALL be L2-normalized
