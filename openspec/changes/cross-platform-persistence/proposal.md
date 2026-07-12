# 跨平台常驻兼容性升级：能力探测式 fallback + 产品级自愈与更新回滚

## Why

`xskill connect` 的常驻能力目前按「平台名」硬编码策略，存在四个武断点，导致
Windows / WSL / Ubuntu / 鸿蒙 / 其他 Linux 上体验参差：

1. **WSL 无 systemd 直接硬失败**（`WSLSystemdRequiredBackend`）。这个策略双重错误：
   - 过苛：用户明明可以以「会话内常驻 + 崩溃自愈」的降级模式运行，却被一刀切拒绝；
   - 没解决真问题：即使 systemd + linger 齐备，**Windows 重启后 WSL VM 也不会自动拉起**
     ——WSL 的开机自启只能靠 Windows 侧触发器（计划任务/启动项经 interop 调 `wsl.exe`），
     现行实现完全没有这一层。
2. **detached 降级无崩溃自愈**（`restart_policy: none`）。无 systemd 的 Linux（精简容器、
   老发行版、鸿蒙）上进程一崩就死透，与「常驻」的承诺不符。
3. **鸿蒙（HarmonyOS/OpenHarmony）零适配**。鸿蒙终端是 Linux 内核 + 无 systemd 用户态，
   现行代码把它当普通 Linux，直接掉进无自愈的 detached。
4. **自动更新无健康检查、无回滚**。pip 装上一个坏 wheel（半残依赖、二进制不兼容
   ——鸿蒙/老 glibc 上很现实）后直接重启，进程起不来 → systemd/schtasks 无限重启循环，
   且 updater 永远不会重试回好版本。

## What Changes

### 1. 设计原则：按「能力探测」选择后端，平台名只用于提示与遥测

后端选择不再 `if 平台名 == X 则拒绝/允许`，而是逐项探测能力（systemd --user 可用？
crontab 可用？WSL interop 可用？），按优先级取第一个可用项；每一级降级都在
`xskill status` 里如实汇报（新增 `crash_recovery` / `boot_autostart` / `degraded` 字段），
**绝不伪装成完整常驻，也绝不因为不完美而拒绝服务**。

### 2. Linux 族（linux / wsl / harmony 统一链路）

```
systemd --user 可用 ──► SystemdUserBackend（自愈=systemd，自启=linger）
        │                  linger 失败 → 降级警告，不再硬失败
        ▼ 不可用
SupervisedProcessBackend（新增）
    watchdog 进程托管 connect --foreground，指数退避自动重启（自愈=watchdog）
    开机自启按 flavor 补挂：
      wsl     → Windows 计划任务经 interop 调 `wsl.exe -d <distro> … xskill start`
      linux/harmony → crontab @reboot（marker 管理，幂等装卸）
      探测不可用 → status.degraded 明示「不随开机自启」，仍正常常驻
```

- WSL + systemd 场景同样补挂 Windows 侧计划任务（否则 Windows 重启后 unit 不会跑）。
- 鸿蒙识别：`/etc/os-release` 的 `ID/ID_LIKE ∈ {harmonyos, openharmony, ohos}` 或
  `uname -r` 带 `-ohos`；识别结果仅影响提示文案与自启挂载方式，主链路与 Linux 一致。
- `XSKILL_CONNECT_BACKEND` 支持 `systemd|supervised|detached` 显式覆盖（原有 `detached`
  语义保留：裸 detached 仍可选，但不再是默认降级）。

### 3. Windows 原生路径加固

schtasks 主路径不变；Group Policy 拒绝后的「启动文件夹」降级从裸 `connect --foreground`
改为拉 supervisor —— 降级路径同样获得崩溃自愈。

### 4. 自动更新产品级加固（updater）

- **升级后健康检查**：pip 安装成功后、重启前，用子进程跑 `<python> -m xskill --version`
  验证新版本可导入可执行；失败即 **pip 回滚到升级前版本**。
- **坏版本拉黑**：健康检查失败/回滚的版本记入 `~/.xskill/update_journal.json`，
  后续检查跳过该版本，杜绝「升级→崩→回滚→再升级」死循环。
- **supervisor 感知的重启**：被 watchdog 托管时（`XSKILL_SUPERVISED=1`）统一以非零退出码
  重启，由 watchdog 用新版本拉起，不再自行 spawn 孤儿进程。

### 5. 多端测试

- 单测：平台×能力矩阵（wsl/harmony/linux × systemd/cron/interop/裸），全部 monkeypatch，
  三大 OS 的 CI ut-it 矩阵均可跑；重写 `test_wsl_persistence_policy.py` 以匹配新策略。
- e2e（Linux CI + 本地）：supervised 链路「start → 杀 connect 子进程 → watchdog 自动拉起
  → stop 全清理」真实进程验证；updater 回滚用假 PyPI + 坏包验证。
- **docker 发行版矩阵**（`tests/docker_e2e/platform_matrix/`）：ubuntu:24.04、debian:12、
  openEuler（鸿蒙用户态最近似，另以覆写 os-release 模拟 harmony 识别），容器内跑同一套
  lifecycle + 自愈 e2e。
- CI：connect-lifecycle e2e 扩到 windows-latest（真 schtasks）；docker 矩阵挂
  nightly/workflow_dispatch。

## Impact

- 受影响模块：`team/client/service.py`（重构选择链）、新增 `team/client/supervisor.py`、
  `team/client/updater.py`（健康检查/回滚/journal）、`cli.py`（`--supervise` 隐藏 flag、
  status 新字段渲染）、CI workflow。
- 行为变化（面向用户）：
  - WSL 无 systemd：从报错拒绝 → 正常常驻（自愈 by watchdog）+ 尽力挂 Windows 自启；
  - 无 systemd Linux/鸿蒙：崩溃自愈从无到有；
  - `xskill status` 多出 `flavor/crash_recovery/boot_autostart/degraded` 字段（增量，
    不破坏既有字段）；
  - 坏版本更新不再导致服务瘫痪。
- 兼容性：既有 systemd/schtasks 安装态可原位接管（state 文件 `method` 向后兼容）；
  `XSKILL_CONNECT_BACKEND=detached` 行为不变。
