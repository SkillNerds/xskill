## ADDED Requirements

### Requirement: ClientInterest.feature_tensor is ≤5 cluster centers

`ClientInterest` SHALL expose a `feature_tensor` property: the user's trajectory-atom
summary embeddings clustered into at most 5 centers via a lightweight numpy-only k-means
(no sklearn/scipy). The number of centers `k` SHALL be `min(5, max(1, n_atoms // 3))` so
that users with few atoms produce fewer (but meaningful) centers rather than 5 noise
centers. `feature_tensor` SHALL be a `(≤5, D)` array; it MAY contain fewer than 5 rows.

#### Scenario: User with many atoms gets 5 centers

- **WHEN** a user has 60 atom summaries embedded
- **THEN** `ClientInterest.feature_tensor` SHALL have shape `(5, D)`
- **AND** the 5 rows SHALL be the k-means centers

#### Scenario: User with few atoms gets fewer centers

- **WHEN** a user has 4 atom summaries embedded
- **THEN** `k = min(5, max(1, 4//3)) = 1`
- **AND** `ClientInterest.feature_tensor` SHALL have shape `(1, D)` (a single center)

#### Scenario: Cold-start user has no feature_tensor

- **WHEN** a user has zero atoms
- **THEN** `ClientInterest.feature_tensor` SHALL be `None`
- **AND** the user is considered to have no profile (cold start)

### Requirement: ClientInterest.mean_tensor is the center mean

`ClientInterest` SHALL expose a `mean_tensor` property: the mean of `feature_tensor` rows,
L2-normalized. When `feature_tensor` is `None` (cold start), `mean_tensor` SHALL be `None`.

#### Scenario: mean_tensor from multiple centers

- **WHEN** `feature_tensor` has 5 rows
- **THEN** `mean_tensor` SHALL be `normalize(mean(feature_tensor, axis=0))`

### Requirement: Clustering uses numpy-only k-means (no heavy deps)

The clustering implementation SHALL depend only on `numpy` (already a dependency). It SHALL
NOT import `sklearn`, `scipy`, `torch`, or any other heavy ML package. The implementation
SHALL be deterministic given the same input ordering with a fixed seed.

#### Scenario: No sklearn import

- **WHEN** the clustering module is imported
- **THEN** it SHALL NOT transitively import `sklearn` or `scipy`

### Requirement: ClientUser tracks used_skills as list-of-dict

`ClientUser` SHALL maintain `used_skills`: a list of dicts `{name, use_count, avg_score}`
derived from the user's trajectory atoms' `used_skills` field and their UX scores. This
SHALL be updated incrementally as atoms are processed.

#### Scenario: used_skills reflects atom history

- **WHEN** a user's atoms reference skill "foo" 3 times with ux scores [8, 9, 7]
- **THEN** `ClientUser.used_skills` SHALL contain `{"name": "foo", "use_count": 3, "avg_score": 8.0}`

### Requirement: ClientUser.recommended_skills records pushed skills

`ClientUser` SHALL maintain `recommended_skills`: a list of dicts
`{skill, branch, hash}` recording which skills (and which version) have been recommended
to this user by `SkillRecommendEngine`. This SHALL be persisted so recommendations are
traceable across syncs.

#### Scenario: recommended_skills recorded after push

- **WHEN** `SkillRecommendEngine.get_skill_for_client` recommends skill "bar" on its
  `staging` branch (sha `abc123`) to a user
- **THEN** `ClientUser.recommended_skills` SHALL include `{"skill": "bar", "branch": "staging", "hash": "abc123"}`

### Requirement: Profile persisted in server SQLite by user_id

The team server SHALL persist each user's `ClientInterest` (feature_tensor, mean_tensor,
used_skills) in a `client_interest` SQLite table keyed by `user_id` (= client_id). Tensors
SHALL be serialized as BLOBs. The client (thin) SHALL NOT store profiles locally. On cold
start (no row), the user SHALL have no profile and recommendations SHALL fall back to ux
ordering — this is the correct definition of "no profile", not a fallback branch.

#### Scenario: Profile survives server restart

- **WHEN** the server restarts and a user syncs again
- **THEN** the user's `feature_tensor` and `used_skills` SHALL be loaded from the
  `client_interest` table
- **AND** recommendations SHALL use the persisted profile

#### Scenario: Cold start falls back to ux ordering

- **WHEN** a user has no row in `client_interest` (no atoms yet)
- **THEN** `get_skill_for_client` SHALL return skills ordered by ux score (quality path)
- **AND** SHALL NOT throw
