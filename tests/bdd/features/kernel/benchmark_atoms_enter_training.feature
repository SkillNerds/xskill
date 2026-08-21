Feature: 内部做题原子进入训练
  As an XSkill maintainer accepting OpenEarth
  I want benchmark temp atoms to reach train_skills inputs
  So that vertical-domain rollouts actually train Skills

  Scenario: 做题临时轨拆完后原子进入 train_skills 输入
    Given aimock 在随机本地端口启动 OpenAI-compatible 服务
    And 平台 TrajectoryReader 已配置 kernel-temp 且 auto_index 开启
    And OpenEarth workspace 已记录该临时轨的 oracle 分
    When 内核 create_temp 写入平台形做题轨迹
    And 平台将临时轨迹拆成多个 ready atom
    And 配置指向 aimock 后调用 train_skills
    Then train_skills 收到的 ScoredAtomInput 包含这些 temp atom_id
    And 对应 score_source 均为 oracle
    And aimock 至少收到一次 chat completions 探活请求

  Scenario: 多 atom 带 oracle 时不抛错且全部入训
    Given aimock 在随机本地端口启动 OpenAI-compatible 服务
    And 平台 TrajectoryReader 已配置 kernel-temp 且 auto_index 开启
    And OpenEarth workspace 已记录该临时轨的 oracle 分
    When 内核 create_temp 写入平台形做题轨迹
    And 平台将临时轨迹拆成多个 ready atom
    And 配置指向 aimock 后调用 train_skills
    Then train_skills 不因多 atom 抛错
    And train_skills 收到的 ScoredAtomInput 数量等于拆出的 atom 数
