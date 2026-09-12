# ALFWorld

家居场景文本交互。划分 39、18、134，名单见 `manifests/alfworld_skillopt_id_split.json`。

做题：xskill 用 Native Claude Code；SkillOpt 用 `claude_code_exec`。规定步数内环境返回 won 算通过。算法镜像从 sub80（SkillOpt）和 sub81（xskill）复制后再改，评测镜像从现有 `alfworld-eval` 复制，不覆盖旧 tag。

正式 xskill 训练把 val 的 18 关并进 train，共 57 关。
