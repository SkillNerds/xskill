## ADDED Requirements

### Requirement: SkillHub is an optional CS-mode third-party skill scanner

`SkillHub` SHALL be an optional component (gated by `config.skillhub.enabled`, default
`false`) that scans the configured third-party skill directory (default
`~/.xskill/skillhub_skills/`) for `SKILL.md` files. When disabled, `SkillHub` SHALL be a
no-op and the recommend engine SHALL operate only on the repo's own skills.

#### Scenario: Disabled by default

- **WHEN** `config.yaml` does not set `skillhub.enabled`
- **THEN** `SkillHub` SHALL not scan any directory
- **AND** third-party skills SHALL NOT appear in recommendations

#### Scenario: Enabled scans configured dir

- **WHEN** `config.yaml` has `skillhub.enabled: true` and `skillhub.dir: ~/.xskill/skillhub_skills`
- **THEN** `SkillHub` SHALL scan that directory for `SKILL.md` files and index them

### Requirement: Third-party skills vectorized by description + tags

`SkillHub` SHALL vectorize each third-party skill using the same fusion as `SkillFeature`
(description + tags; third-party skills have no routed atoms in this repo, so the last5-atom
source is absent by definition). The resulting vectors SHALL be L2-normalized and added to
the `SkillRecommendEngine` retrieval pool alongside the repo's own `main`/`staging` skills.

#### Scenario: Third-party skill indexed into pool

- **WHEN** `skillhub.enabled` is true and `~/.xskill/skillhub_skills/foo/SKILL.md` exists
- **THEN** `SkillHub` SHALL compute a fused vector for "foo"
- **AND** "foo" SHALL be retrievable by `get_skill_for_client`'s relevance KNN

### Requirement: Third-party skills participate only in the relevance bucket

Third-party `SkillHub` skills SHALL participate ONLY in the relevance (20%) bucket of
`get_skill_for_client`. They SHALL NOT appear in the quality (ux-score) bucket (they have
no UX scores in this repo), and they SHALL NOT participate in staging-priority达量 logic
(they have no git branches / canary). This keeps the staging达量 accounting clean for
the repo's own skills.

#### Scenario: Third-party skill never in quality bucket

- **WHEN** `get_skill_for_client` builds the quality bucket (ux-ordered)
- **THEN** no third-party `SkillHub` skill SHALL appear in the quality bucket

#### Scenario: Third-party skill never in staging达量 logic

- **WHEN** staging-priority达量 logic runs for a recommended skill
- **THEN** third-party skills SHALL NOT be assigned a `staging` side
- **AND** SHALL NOT count toward any `staging_need` quota

### Requirement: skillhub directory and enablement configurable

`config.yaml` SHALL add a `skillhub` section with `enabled` (bool, default `false`) and
`dir` (path, default `~/.xskill/skillhub_skills`). The `CONFIG_TEMPLATE` SHALL document
both fields. Missing directory when enabled SHALL raise a clear error (not silently skip),
per the no-fallback code convention.

#### Scenario: Enabled but dir missing raises

- **WHEN** `skillhub.enabled: true` but `skillhub.dir` does not exist on disk
- **THEN** `SkillHub` initialization SHALL raise a `FileNotFoundError` with a message
  naming the missing directory
- **AND** SHALL NOT silently skip indexing
