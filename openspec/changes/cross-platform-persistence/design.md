# design — cross-platform-persistence

## 1. 能力探测层（service.py）

| 探测函数 | 判定 | 备注 |
|---|---|---|
| `_is_wsl()` | 现有实现不变 | env + /proc osrelease |
| `_is_harmony()` | os-release `ID`/`ID_LIKE` ∈ {harmonyos, openharmony, ohos} 或 `uname -r` 含 `ohos` | 只读文件，无子进程 |
| `_linux_flavor()` | `"wsl" \| "harmony" \| "linux"` | wsl 优先（WSL 里跑鸿蒙容器不现实） |
| `_systemd_user_available()` | 现有实现不变 | `systemctl --user show-environment` |
| `_crontab_available()` | `which crontab` 且 `crontab -l` 退出码 ∈ {0,1}（1=「no crontab for user」） | timeout 5s |
| `_wsl_interop_available()` | `which wsl.exe` 且 `which schtasks.exe`（WSL interop 开启时 Windows PATH 自动追加） | 不主动执行 .exe |

所有探测失败 = 能力缺失，不抛异常。探测结果进 status 的 `degraded` 说明。

## 2. SupervisedProcessBackend（新增，method="supervised"）

- `install_and_start()`：
  1. 已 running 则幂等返回；
  2. detach 拉起 watchdog：`<python> -m xskill connect --supervise`（`start_new_session=True`，
     日志 `~/.xskill/logs/connect-supervisor.log`）；
  3. 按 flavor 挂开机自启（见 §3），失败仅记 `degraded`；
  4. 等待 state 文件出现 child_pid（最多 10s）后返回 status。
- watchdog 主体在新模块 `supervisor.py::run_supervisor()`：
  - 循环 spawn `<python> -m xskill connect --foreground`（env 加 `XSKILL_SUPERVISED=1`），
    每次 spawn 后把 `child_pid` 写回 daemon state；
  - 退避：初值 1s ×2 递增、封顶 300s；子进程存活 ≥600s 则退避归零；
  - **停止语义**：收到 SIGTERM → 给 child SIGTERM（5s 后 SIGKILL）→ 清 state → 退出；
  - **防双跑**：启动时若 state 里 watchdog_pid 存活则直接退出（幂等）。
- `stop()`：SIGTERM watchdog（兜底再杀 child）+ 卸开机自启挂载 + 清 state。
- `status()`：`running` = watchdog 活 && child 活；单侧死亡分别汇报
  （`watchdog_alive` / `child_alive`），便于诊断。

## 3. 开机自启挂载（可插拔，随 supervised/systemd 后端组合）

| flavor | 机制 | 装 | 卸 |
|---|---|---|---|
| linux/harmony | crontab `@reboot <python> -m xskill start --quiet  # xskill-connect` | 读现 crontab，去重后追加 marker 行 | 按 marker 过滤重写 |
| wsl | `schtasks.exe /Create /TN Xskill_WSL_Boot /SC ONLOGON /TR "wsl.exe -d <distro> -u <user> -- <python> -m xskill start --quiet"` | interop 调用 | `schtasks.exe /Delete` |
| 任一失败 | 记 `boot_autostart: "none"` + `degraded` 警告 | — | — |

`xskill start --quiet`：已 running 时静默退出 0（幂等），使自启触发器可无脑重复执行。
WSL + systemd 组合同样挂 Windows 任务（systemd 只解决「VM 内」自启；VM 本身要 Windows 拉）。

## 4. LinuxServiceBackend 选择链（重写）

```
install:  systemd? ──yes─► SystemdUserBackend（linger 失败→警告不阻断）
                 └─no──► SupervisedProcessBackend
override: XSKILL_CONNECT_BACKEND ∈ {systemd, supervised, detached}
stop/status: 先按 state.method 路由（接管旧安装态），state 缺失再按探测链
```

`WSLSystemdRequiredBackend` 删除；`DetachedProcessBackend` 保留但仅显式 override 可达。

## 5. updater 加固

- `_health_check(python) -> bool`：`subprocess.run([python, "-m", "xskill", "--version"],
  timeout=60)`，returncode==0。
- `_install` 成功后：health check 失败 → `pip install xskill==<升级前版本>` 回滚（同一
  `_PIP_TIMEOUT` 约束）→ journal 记 `bad_versions[target] = {ts, reason}`；回滚也失败则
  critical 日志 + 不重启（宁可跑旧代码在内存里，也不重启进坏版本）。
- journal：`~/.xskill/update_journal.json`，损坏容忍（解析失败视为空）；`_check_and_update`
  对 PyPI/server 候选版本先过 `bad_versions` 滤网。健康升级成功记 `last_good`。
- `_restart()`：`XSKILL_SUPERVISED=1` 时全平台统一 `os._exit(1)` 交给 watchdog；
  其余路径维持现状（execv / schtasks exit-1 / startup_folder spawn）。

## 6. 兼容与迁移

- state 文件新增键（watchdog_pid/child_pid/boot_autostart/flavor）全部增量；旧 state 的
  `method ∈ {systemd-user, detached, schtasks, startup_folder}` 继续被识别。
- 旧 detached 安装态在下次 `xskill start` 时被停掉并迁移到新链路（沿用现有迁移逻辑）。
- Windows startup_folder 的 .vbs 内容从 `connect --foreground` 换成 `connect --supervise`；
  旧 .vbs 无需主动迁移，下次 start 重写。

## 7. 测试设计

- **单测**（全 OS 可跑，无真进程）：探测矩阵、选择链矩阵、cron marker 幂等、interop 命令
  拼装、journal 读写与滤网、_restart 分支路由。
- **进程级 e2e**（Linux）：supervised 全生命周期 + kill child 自愈 + stop 全清理；
  伪 crontab（PATH shim）验证 @reboot 装卸。
- **windows e2e**（CI windows-latest）：schtasks 真装卸 + status。
- **docker 矩阵**：ubuntu:24.04 / debian:12 / openEuler(+os-release 覆写模拟鸿蒙)，
  容器内无 systemd → 必然落 supervised 链，跑同一套 e2e 用例。
