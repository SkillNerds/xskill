## ADDED Requirements

### Requirement: `--name` flag provides stable cross-device user identity

The `xskill connect` command SHALL accept an optional `--name <userid>` flag. When provided,
the team server SHALL derive a deterministic `client_id` from the normalized `user_name`
(`sha256("name:" + norm_name)[:16]`) and use it as the user's stable identity — the same
`--name` on different devices or after reinstall SHALL resolve to the same `client_id` and
thus the same `ClientInterest`/profile. The server SHALL NOT issue a new uuid when
`--name` is provided.

The `--name` identity SHALL take precedence over the existing `claimed_client_id` /
`(hostname, label)` fingerprint resolution: when `--name` is present, the server SHALL NOT
run the fingerprint lookup path.

#### Scenario: Connect with --name on two devices shares identity

- **WHEN** user runs `xskill connect host:port --token T --name alice` on device A
- **AND** user runs `xskill connect host:port --token T --name alice` on device B
- **THEN** the server SHALL return the same `client_id` for both connections
- **AND** both devices SHALL share the same `ClientInterest` profile history

#### Scenario: Connect with --name after reinstall keeps identity

- **WHEN** user previously connected with `--name alice` and the local `team_client.json`
  was deleted (reinstall)
- **AND** user reconnects with `xskill connect host:port --token T --name alice`
- **THEN** the server SHALL return the same `client_id` as before
- **AND** the user's historical profile SHALL remain associated

#### Scenario: --name takes precedence over fingerprint resolution

- **WHEN** a client sends `user_name="alice"` together with a stale `claimed_client_id`
  that the server no longer recognizes
- **THEN** the server SHALL resolve identity via the `--name` deterministic id
- **AND** SHALL NOT fall back to `(hostname, label)` fingerprint lookup

### Requirement: Anonymous connect falls back to hashid (existing uuid logic)

When `--name` is omitted, the connect SHALL be anonymous and the server SHALL resolve
`client_id` via the existing three-tier logic (`claimed_client_id` → `(hostname, label)`
fingerprint → new uuid). Anonymous behavior SHALL be identical to before this change.

#### Scenario: Connect without --name is anonymous

- **WHEN** user runs `xskill connect host:port --token T` (no `--name`)
- **THEN** the server SHALL resolve `client_id` via the existing uuid/fingerprint logic
- **AND** SHALL NOT derive a name-based id

### Requirement: Server `allow_anonymous_user` gate at /register

The team server SHALL read `team.server.allow_anonymous_user` from `config.yaml` (default
`true`). When set to `false`, the `/api/v1/team/register` endpoint SHALL reject any
`RegisterRequest` whose `user_name` is null/empty with HTTP 403
`anonymous users not allowed`. When `true` (default), anonymous registration SHALL behave
as before.

#### Scenario: Anonymous rejected when allow_anonymous_user is false

- **WHEN** `config.yaml` has `team.server.allow_anonymous_user: false`
- **AND** a client registers without `--name` (`user_name` is null)
- **THEN** the server SHALL return HTTP 403 with detail `anonymous users not allowed`
- **AND** no `client_id` SHALL be issued

#### Scenario: Named connect allowed when allow_anonymous_user is false

- **WHEN** `config.yaml` has `team.server.allow_anonymous_user: false`
- **AND** a client registers with `--name alice`
- **THEN** the server SHALL accept the registration and return the name-derived `client_id`

#### Scenario: Default allows anonymous (backward compatible)

- **WHEN** `config.yaml` does not set `team.server.allow_anonymous_user`
- **AND** a client registers without `--name`
- **THEN** the server SHALL accept the anonymous registration (behavior identical to before)
