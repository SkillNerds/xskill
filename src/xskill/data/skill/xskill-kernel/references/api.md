# Algorithm kernel API reference

## Contents

- [Implementation skeleton](#implementation-skeleton)
- [Kernel metadata](#kernel-metadata)
- [Invocation and context](#invocation-and-context)
- [Trajectory access](#trajectory-access)
- [Skill access and publication](#skill-access-and-publication)
- [Run result](#run-result)

## Implementation skeleton

```python
from xskill.kernels import (
    BaseKernel,
    KernelContext,
    KernelMetadata,
    KernelRunResult,
)


class MyAlgorithmKernel(BaseKernel):
    metadata = KernelMetadata(
        id="my-algorithm-kernel",
        name="My Algorithm Kernel",
        version="1.0.0",
        description="Generate Skills from registered trajectories.",
        triggers=("scheduled", "manual", "evaluation"),
    )

    def run(self, context: KernelContext) -> KernelRunResult:
        # Call the provider package here.
        return KernelRunResult()


KERNEL_CLASS = MyAlgorithmKernel
```

The directory name must equal `metadata.id`.

## Kernel metadata

| Field | Meaning |
| --- | --- |
| `id` | Stable lowercase ID used in configuration and paths. |
| `name` | Human-readable display name. |
| `version` | Version of the algorithm implementation. |
| `description` | Short description shown in the dashboard. |
| `triggers` | Supported invocation types: `scheduled`, `trajectory_changed`, `manual`, `evaluation`. |

## Invocation and context

Each `run(context)` call is one bounded synchronous invocation.

| Attribute | Meaning |
| --- | --- |
| `context.run_id` | Unique run identity for logs and workspace keys. |
| `context.invocation.trigger` | Reason for the call. |
| `context.invocation.dataset_id` | Live scope or content-addressed evaluation dataset. |
| `context.invocation.changed_trajectory_ids` | Change hint for event-triggered runs. |
| `context.invocation.full_rebuild` | Whether the caller requested a full rebuild. |
| `context.config_path` | Provider-owned configuration path. |
| `context.workspace` | Writable provider workspace for cursors, caches, databases, and artifacts. |
| `context.trajectories` | Read-only trajectory access. |
| `context.skills` | Read-only Skill bundles, versions, and UX summaries. |
| `context.publisher` | Managed publication gateway. |

## Trajectory access

Stream individual resources:

```python
for item in context.trajectories.iter():
    text = item.read_text()
    metadata = dict(item.metadata)
```

Use registered roots for native batch tools:

```python
for source in context.trajectories.directories():
    subprocess.run(["rg", "--json", "pattern", str(source.path)], check=True)
```

`TrajectoryResource` exposes `id`, `trajectory_id`, `path`, `watch_dir`, `label`,
`ecosystem`, `status`, `metadata`, `read_text()`, and `read_raw_json()`.

## Skill access and publication

Read a complete current bundle:

```python
skill = context.skills.get("existing-skill", days=30)
files = skill.list_files()
body = skill.read_text("SKILL.md")
```

Edit an existing Skill through a managed checkout:

```python
draft = context.skills.checkout("existing-skill")
provider.edit(draft.path)
published = context.publisher.submit_checkout(
    draft,
    message="improve instructions",
    source_trajectory_ids=tuple(source_ids),
)
```

The checkout records the main commit. A stale base or an active staging candidate is
rejected instead of overwriting another version. New Skill names use
`context.publisher.submit(SkillSubmission(...))`.

`SkillResource.versions` reports main/staging commit IDs and version-bound UX sample
counts, averages, and scoring timestamps.

## Run result

`KernelRunResult` fields:

| Field | Meaning |
| --- | --- |
| `processed_trajectory_ids` | Inputs actually completed in this call. |
| `submitted_skills` | Skill names submitted in this call. |
| `metrics` | JSON-serializable provider diagnostics; not a trusted quality score. |
| `notes` | Short non-sensitive run note. |
