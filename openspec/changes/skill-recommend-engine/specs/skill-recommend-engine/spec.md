## ADDED Requirements

### Requirement: SkillRecommendEngine manages user and skill vector stores

`SkillRecommendEngine` SHALL be constructed with the `XSkillConfig` and SHALL maintain two
vector stores: the user-profile store (per-user `feature_tensor`/`mean_tensor`) and the
skill-feature store (the fused skill vectors from `.skill_index.pkl`, restricted to
distributable `main`+`staging` skills plus enabled `SkillHub` third-party skills). `baby`
-branch skills SHALL NOT be in the retrieval pool.

#### Scenario: baby skills excluded from retrieval

- **WHEN** the skill repo contains a skill that only has a `baby` branch (no `main`)
- **THEN** that skill SHALL NOT appear in any `get_skill_for_client` result

### Requirement: update_user_interest incrementally updates the profile

`SkillRecommendEngine.update_user_interest(ClientInterest, TaskAtom)` SHALL, given a
completed (vectorized) atom, update the user's profile store: append the atom's summary
embedding to the user's point set, recompute `feature_tensor` (re-cluster, respecting the
`k` cap), recompute `mean_tensor`, and persist to the `client_interest` table.

#### Scenario: Atom updates profile

- **WHEN** `update_user_interest` is called with a new atom for a user
- **THEN** the user's `feature_tensor` SHALL be recomputed from the updated atom set
- **AND** the `client_interest` row SHALL be upserted with the new tensors

### Requirement: get_skill_for_client mixes 80% quality + 20% relevance with backfill

`get_skill_for_client(ClientUser, skill_num) -> list[Skill]` SHALL return `skill_num`
skills composed of: a quality bucket of `ceil(skill_num * quality_ratio)` skills (default
`quality_ratio=0.8`) ordered by ux score, and a relevance bucket filling the remainder via
per-center KNN vector search over the skill-feature store (cosine, deduped against the
quality bucket). When the quality bucket has fewer than its target (skill total is small),
the relevance bucket SHALL backfill up to `skill_num`. The ratio SHALL be configurable via
`recommend.quality_ratio`.

#### Scenario: Standard 80/20 split

- **WHEN** `skill_num=10`, `quality_ratio=0.8`, and the repo has 30 skills
- **THEN** the result SHALL contain 8 quality-ordered skills + 2 relevance-ordered skills

#### Scenario: Relevance backfills when quality pool is small

- **WHEN** `skill_num=10` but only 4 skills have ux scores
- **THEN** the result SHALL contain 4 quality skills + 6 relevance skills (backfilled)

### Requirement: Staging-priority达量 push fixes starvation

When `get_skill_for_client` selects a skill that has a `staging` branch, the engine SHALL
apply staging-priority达量 logic before resolving the slot's side:

1. If the skill's staging side has fewer than `staging_need` UX scores
   (`staging_need` = `canary.total_samples` by default), the engine SHALL assign the
   `staging` side to the users most likely to use this skill (ordered by recency of this
   skill in their `used_skills`), until staging reaches `staging_need`.
2. Once staging is达量 but the current `main` hash is not, the engine SHALL assign the
   `main` side until main also reaches `staging_need`.
3. When both sides are达量, side resolution SHALL defer to `CanaryRouter.assign`
   (existing per-client钉死 + balanced shunting).

This replaces the stateless `pick_side` starvation where small client bases leave staging
with zero traffic.

#### Scenario: Staging prioritized when under quota

- **WHEN** a recommended skill has staging with 2 UX scores, `staging_need=5`
- **AND** the most-likely user (most recent `used_skills` entry for this skill) is selected
- **THEN** that user's slot for this skill SHALL resolve to `staging`

#### Scenario: Main pushed after staging reaches quota

- **WHEN** a recommended skill's staging side has 5 UX scores (`staging_need=5`,达量)
- **AND** the current main hash has only 2 UX scores
- **THEN** the next recommended user's slot for this skill SHALL resolve to `main`

#### Scenario: Both sides达量 defers to CanaryRouter

- **WHEN** both staging and main sides have ≥ `staging_need` UX scores
- **THEN** side resolution SHALL defer to `CanaryRouter.assign` (existing behavior)

### Requirement: recommend_users and recommended_skills recorded bidirectionally

After `get_skill_for_client` resolves a slot, the engine SHALL record the assignment
bidirectionally: `Skill.recommend_users[side]` SHALL include the `ClientUser`, and
`ClientUser.recommended_skills` SHALL include `{skill, branch, hash}`. Both are views over
the persisted recommendation records, not independent stores.

#### Scenario: Bidirectional recording

- **WHEN** user alice is recommended skill "bar" on staging
- **THEN** `Skill("bar").recommend_users["staging"]` SHALL contain alice
- **AND** `alice.recommended_skills` SHALL contain `{"skill": "bar", "branch": "staging", "hash": <sha>}`

### Requirement: find_friend returns users by mean_tensor similarity

`SkillRecommendEngine.find_friend(ClientUser) -> list[ClientUser]` SHALL compute the user's
`mean_tensor` and perform a nearest-neighbor search over all other users' `mean_tensor`
vectors, returning the closest matches. Users without a profile (cold start) SHALL be
excluded from both query and candidates.

#### Scenario: find_friend returns similar users

- **WHEN** alice's `mean_tensor` is closest to bob's among all profiled users
- **THEN** `find_friend(alice)` SHALL return bob (and other close matches) ordered by
  cosine similarity

### Requirement: find_tag_for_user and find_tag_for_skill via semantic search

`SkillRecommendEngine.find_tag_for_user(ClientUser) -> list[str]` SHALL semantically
retrieve relevant tags from the skill-atom tag set for the user's interests.
`find_tag_for_skill(Skill) -> list[str]` SHALL return the most relevant tags for that
skill. Both use vector similarity over the tag embedding index.

#### Scenario: find_tag_for_user returns relevant tags

- **WHEN** alice's interests cluster around "django migration" atoms
- **THEN** `find_tag_for_user(alice)` SHALL return tags semantically close to that domain
- **AND** the list SHALL be ordered by relevance
