# OfficeQA

真实财政公报文档问答。划分 50、24、172，名单见 `manifests/officeqa_skillopt_id_split.json`。

做题：xskill 挂 Read、Bash、Skill；SkillOpt 挂 Read、Bash。从文档里抽出答案后，调用 SkillOpt 的 `evaluate`，hard 用归一化整串匹配。不要接 Databricks `reward.py`。

正式 xskill 训练把 val 的 24 题并进 train，共 74 题。SkillOpt 训练只吃 train 50 题，val 做门禁。
