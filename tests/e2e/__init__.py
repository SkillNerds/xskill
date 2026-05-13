"""tests/e2e — Smoke E2E suite (P4).

每个 agent (claude_code / codex / opencode) 一份 parametrized smoke：把
fixture session 丢到 tmp_path 模拟的 agent home → 跑 ingester 桥成 traj_*.md
→ 跑 install_to_<agent> 把 mock skill 装到该 agent 的 discovery 路径。

设计原则：
- 全程 tmp_path 隔离，不污染用户 ``~/.xskill`` / ``~/.claude`` / ``~/.codex``
- stub LLM/embed —— 不调外网；只验路径 / 文件 schema / 安装结果
- 单 e2e ≤ 10s——不起子进程、不起 server，直接 instantiate
  ingester + 调 installer
"""
