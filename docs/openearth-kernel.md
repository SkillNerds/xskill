# 可选：使用 OpenEarth 算法内核

xskill 默认使用内置的 native 内核（拆分、归类、编辑三个代理）完成技能进化。如果你希望换用 OpenEarth 算法内核来驱动进化，可以按本文档操作。切换是可选的，不影响已有功能。

> 对应设计文档：[合入设计](plans/2026-08-27-openearth-landing-on-main.md)、[第一期用户说明书](plans/2026-08-27-openearth-first-user-guide.md)

## 什么是算法内核

算法内核是轨迹进站之后、Skill 发布之前可替换的那一层。native 内核由平台自带；OpenEarth 是第三方内核，通过桥接目录挂载到平台。切换内核只改变"ready 之后谁来生成 Skill"，轨迹采集、拆分、推荐、灰度等流程不变。

## 前置条件

- 团队模式（`xskill serve --server`）已正常运行
- admin 权限

## 三步启用

### 1. 安装 wheel 并拷贝目录

在跑 `xskill serve` 的同一个 Python 环境里操作：

```bash
python -m pip install \
  examples/kernels/openearth/wheels/openearth_skill_sdk-0.10.1-py3-none-any.whl

mkdir -p "$HOME/.xskill/kernels"
cp -R examples/kernels/openearth "$HOME/.xskill/kernels/openearth"
cp "$HOME/.xskill/kernels/openearth/config.yaml.example" \
   "$HOME/.xskill/kernels/openearth/config.yaml"
```

### 2. 填写核自己的 config.yaml

编辑 `~/.xskill/kernels/openearth/config.yaml`（平台不读这份文件）：

```yaml
reflect:
  base_url: opencode
  model: YOUR_OPENEARTH_MODEL
  binary: opencode
  timeout: 600

benchmark:
  enabled: false
```

`model` 和 `binary` 换成这台机器上真实可用的值。benchmark 必须关掉，因为 kernel-host 启动首圈会 `full_rebuild`。

### 3. 在设置页切换 kernel_id

admin 登录看板，打开设置，在全文编辑器里补上：

```yaml
kernel:
  kernel_id: openearth
  kernels_path: ~/.xskill/kernels   # 可省略，默认值
```

点「校验并热加载」。`kernel` 段属于重启域，需要重启 `xskill serve` 才生效。

## 常见输出

校验通过：

```
✓ 校验通过
```

kernel_id 格式错误（如写成 `OpenEarth`）：

```
✗ kernel id must match [a-z0-9][a-z0-9_-]{0,63}: 'OpenEarth'
```

目录不存在：

```
✗ kernel not found: not-a-real-kernel
```

SDK 没装上：

```
✗ kernel openearth is unavailable: ModuleNotFoundError: No module named 'openearth_skill_sdk'
```

切核成功后提示：

```
✓ 已生效 无变更;⚠ kernel 段需重启 serve
```

重启后日志中会出现：

```
10:28:01 [xskill.kernel.host] INFO external kernel host selected openearth (interval 30.0s, server=True)
```

## 注意事项

- 切换只在 server 侧生效，客户端 `xskill connect` 不读 `kernel` 段
- 不要用 `xskill distill` 换线上的核；`xskill generate` 与换核无关
- 不要把平台 `llm`、`embedding`、`canary` 等配置写进核的私有 config.yaml
- 核自己 print 的内容目前看不到（stdout 送了 DEVNULL），只看 `~/.xskill/logs/xskill.log`

## 切回 native

把 `kernel_id` 改回 `native`（或删掉 `kernel` 段），重启 serve 即可恢复默认内核。

## 更多细节

详见 [第一期用户说明书](plans/2026-08-27-openearth-first-user-guide.md)，包含设置页截图样式、两份配置的职责划分、错误场景完整列表等。
