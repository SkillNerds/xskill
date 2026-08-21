Feature: Platform feeds ready trajectories with atom views to algorithm kernels
  As an algorithm kernel developer
  I want create_temp + ready-only feeding with stable atom_ids
  So that I can consume sub-trajectories without waiting on pending splits

  Scenario: create_temp succeeds, stays pending until split, then feeds atoms
    Given a trajectory reader with a temp root
    When the kernel creates a temp trajectory from platform-shaped markdown
    Then the temp trajectory is pending with no atoms
    And the kernel-temp watch directory has auto_index enabled
    And the temp trajectory is absent from the feed
    When the platform finishes splitting the temp trajectory into one atom
    Then the temp trajectory enters the feed as ready
    And the fed atom exposes its content, ux_score and used_skills

  Scenario: create_temp rejects non-platform markdown
    Given a trajectory reader with a temp root
    When the kernel creates a temp trajectory from evidence markdown without a User section
    Then create_temp raises a validation error mentioning the platform format

  Scenario: pending user trajectories never enter the feed
    Given a discovered user trajectory that is still pending
    When the host builds the feed snapshot
    Then the pending trajectory is absent from the feed

  Scenario: incremental ready re-feeds the full atom list and the kernel dedups by atom_id
    Given a ready user trajectory with one atom already consumed by the kernel
    When the platform splits one more atom for the trajectory
    Then the trajectory re-enters the feed as ready
    And the fed atoms include both the previously seen atom and the newly split atom
    And only the newly split atom_id is unseen by the kernel

  Scenario: demo kernel offline distillation still succeeds without atoms
    Given the demo algorithm kernel and the mock runtime trajectories
    When offline distillation runs against the demo kernel
    Then the distillation report status is success with submitted skills
