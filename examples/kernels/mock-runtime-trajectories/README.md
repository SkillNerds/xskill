# Mock 运行时轨迹样例

本目录只提供**脱敏后的 mock 运行时轨迹**，供算法内核开发和 `xskill distill` 联调。

- 它不是算法方的垂直领域评测集，也不应被当成 benchmark 数据集根目录。
- 若算法需要自维护防退化评测集，请放在该内核自己的 `context.workspace` 下。
- `.md` 保存轨迹正文；同名 `.json` 仅含有限 sidecar（如 `harness`）。
- 会话编号与本地路径已替换为测试值；不含真实密钥。
