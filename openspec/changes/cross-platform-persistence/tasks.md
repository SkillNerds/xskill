# tasks — cross-platform-persistence

- [x] 1. 能力探测层：`_is_harmony` / `_linux_flavor` / `_crontab_available` / `_wsl_interop_available`
- [x] 2. `supervisor.py`：watchdog 循环（退避、child_pid 回写、SIGTERM 级联、防双跑）
- [x] 3. `SupervisedProcessBackend`（装/停/看 + 幂等）
- [x] 4. 开机自启挂载：cron @reboot marker 管理 + WSL interop schtasks 任务
- [x] 5. `LinuxServiceBackend` 选择链重写；删 `WSLSystemdRequiredBackend`；linger 失败降级为警告
- [x] 6. Windows startup_folder 降级改走 supervisor
- [x] 7. `cli.py`：`connect --supervise` 隐藏 flag、`start --quiet`、status 渲染新字段
- [x] 8. updater：health check + 回滚 + update_journal 拉黑 + supervisor 感知 `_restart`
- [x] 9. 单测：探测/选择链/cron/interop/journal 矩阵；重写 `test_wsl_persistence_policy.py`
- [x] 10. e2e：supervised 自愈生命周期（Linux）；windows-latest lifecycle
- [x] 11. docker 发行版矩阵（ubuntu/debian/openEuler+鸿蒙模拟）脚本与文档
- [x] 12. CI 接线：windows lifecycle job、platform-matrix nightly job
