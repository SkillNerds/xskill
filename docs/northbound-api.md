# xskill 北向接口文档 / Northbound API

> 本文档描述 xskill server（`xskill serve` / `xskill serve --server`）对外暴露的
> RESTful HTTP 接口，供运维 dashboard、第三方集成、CI 编排等「北向」消费方调用。
>
> 所有端点默认监听 `http://<host>:<port>`，前缀 `/api/v1`。team server 模式另有
> `/api/v1/team/*`（C/S 协议）。dashboard 端点需 `config.yaml` 的 `dashboard.enabled: true`。
>
> 本 PR（skill-recommend-engine）新增的接口以 ★ 标注。

---

## 一、Team C/S 协议（`/api/v1/team/*`）

team server 模式（`xskill serve --server`）下，瘦客户端（`xskill connect`）与 server
之间的协议。除 `register` 外，所有端点需 HTTP header 鉴权：

| Header | 值 |
|---|---|
| `X-Xskill-Token` | server join token（`xskill serve --server` 启动时打印 / `~/.xskill/team_server.json`） |
| `X-Xskill-Client` | client_id（`register` 返回） |

### POST `/api/v1/team/register` — 注册 / 续用 client 身份

★ 本 PR 新增 `user_name` 字段（`--name` 稳定身份）。

**Request body**:
```json
{
  "token": "<join_token>",
  "client_label": "alice-laptop",      // 可读标签，缺省=主机名
  "hostname": "alice-laptop",
  "claimed_client_id": null,           // 重连时自报已有 client_id，希望续用
  "user_name": "alice"                 // ★ 显式身份键；非空→派生确定性 client_id（跨设备同 name 共享画像）
                                       //   null→匿名（受 server allow_anonymous_user 闸门）
}
```
**Response 200**:
```json
{"client_id": "7e8e2d7833a2eb0f"}     // 带 --name 时 = sha256("name:"+norm)[:16]；匿名=uuid
```
**Response 403**: `allow_anonymous_user: false` 且 `user_name` 为空 → `{"detail":"anonymous users not allowed"}`

**身份解析优先级**：`user_name`(派生确定性 id) > `claimed_client_id`(续用) > `(hostname,label)` 指纹回查 > 新 uuid。

### POST `/api/v1/team/upload` — 上传脱敏轨迹

**Request body**:
```json
{
  "trajectories": [
    {
      "traj_id": "traj_cc_xxx_001",    // 必须 traj_ 前缀
      "content": "<markdown 全文>",     // 已脱敏
      "sha256": "<content sha256>",     // 完整性校验
      "model": "deepseek-v4-flash",     // 产生该轨迹的用户 agent 模型
      "harness": "claude_code"          // 产生该轨迹的 coding agent
    }
  ]
}
```
**Response 200**: `{"accepted": ["traj_id", ...], "rejected": [{"traj_id","reason"}, ...]}`

轨迹落盘到 `<traj_root>/clients/<client_id>/sessions/<traj_id>.md`，watcher 自动拆 atom、建画像、进推荐池。

### GET `/api/v1/team/sync` — 拉取 skill manifest（≤100 slot）

★ 本 PR：sync 前 server 调 `SkillRecommendEngine.update_user_interest` 刷新该 client 画像（atom 变化时重算，未变指纹命中跳过）；manifest 的 recommended 桶由引擎按画像相关性产出，ranked 桶仍按 ux 滑窗。

**Response 200**:
```json
{
  "slots": [
    {"skill_name": "python-basics", "side": "main", "sha": "abc123...", "bucket": "ranked"},
    {"skill_name": "web-flask", "side": "staging", "sha": "def456...", "bucket": "recommended"}
  ],
  "server_time": 1700000000.0
}
```
- `bucket`: `ranked`（ux 滑窗，前 `ranked_slots` 个）| `recommended`（画像相关性位，其余）
- `side`: `main` | `staging`（★ staging 优先达量：staging 未达 `staging_need` → 优先 staging；staging 达量 main 未达 → main；双侧达量 → `pick_side` 确定性分流）

### GET `/api/v1/team/skill/{name}/bundle` — 拉取 skill git bundle

**Response 200**: `application/octet-stream`（git bundle 二进制，client 解包成工作副本）

### POST `/api/v1/team/push-edit` — 推送本地手改 skill

**Headers**: 额外 `X-Xskill-Skill: <skill_name>`
**Body**: git bundle 二进制
**Response 200**: `{"branch": "user-staging/<client_id>", "ref_sha": "..."}`

### POST `/api/v1/team/ingest-db` — 上传原始 db 文件（ngagent/opencode SQLite）

**multipart**: `file=<db>`, `eco=ngagent|opencode`
**Response 200**: `{"client_id": "...", "saved": "<path>", "bridged": <n_traj>}`

---

## 二、Skill / Trajectory / Canary 核心 API

standalone 与 team server 均可用（无需 team token）。

### Skill 管理

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/skills` | skill 列表（name/version/eval_score/tags/frozen） |
| GET | `/api/v1/skills/{name}` | skill 详情（description/metadata/body/files） |
| GET | `/api/v1/skills/{name}/log` | git log |
| GET | `/api/v1/skills/{name}/diff` | git diff（HEAD~1 vs HEAD） |
| POST | `/api/v1/skills/{name}/rollback` | 回滚到指定 commit |
| POST | `/api/v1/skills/{name}/freeze` | 冻结（metadata.frozen=true） |
| POST | `/api/v1/skills/{name}/unfreeze` | 解冻 |
| GET | `/api/v1/skills/{name}/export` | 导出 skill 目录（zip） |
| POST | `/api/v1/skills/import` | 导入 skill（zip） |
| POST | `/api/v1/skills/search` | 语义检索 skill（向量） |
| POST | `/api/v1/skills/resolve` | 解析 skill 版本（main/staging） |
| GET | `/api/v1/skills/{name}/candidates` | 候选 buffer（.candidates.yml） |
| GET | `/api/v1/skills/{name}/canary` | 灰度状态 + ux 分 |

### Canary

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/canary/overview` | 全局灰度概览 |

### Trajectory / Registry

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/trajectories/search` | 语义检索轨迹/atom |
| GET | `/api/v1/trajectories/content` | 读轨迹原文 |
| GET | `/api/v1/trajectories/logs` | 轨迹处理日志 |
| GET | `/api/v1/trajectories/list` | 轨迹列表 |
| GET | `/api/v1/registry/dirs` | 已注册 watch dir 列表 |
| POST | `/api/v1/registry/dirs` | 注册 watch dir |
| POST | `/api/v1/reindex` | 重建 skill 向量索引（★ 本 PR：传 `atom_store_roots` 算 `atom_feats`，重建后失效引擎缓存） |

---

## 三、Dashboard API（`/api/v1/dashboard/*`）

需 `dashboard.enabled: true`。只读统计 + skill 详情 + ★ ux 查询。

### 概览 / 统计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/dashboard/overview` | 总览 |
| GET | `/api/v1/dashboard/by-domain` | 按领域分组 |
| GET | `/api/v1/dashboard/rates` | 衍生率（推荐触发率/原子采纳率/canary 晋升率） |
| GET | `/api/v1/dashboard/cost` | token 成本估算 |
| GET | `/api/v1/dashboard/models` | 模型分布 |
| GET | `/api/v1/dashboard/dirs` | watch dir |
| GET | `/api/v1/dashboard/canary` | 灰度两侧统计 |
| GET | `/api/v1/dashboard/users` | ★ team client 列表 + 总数（`{total, users:[{client_id,user_name,label,...}]}`） |
| GET | `/api/v1/dashboard/tags` | tag 统计 |
| GET | `/api/v1/dashboard/skills` | skill 列表（含 canary 状态） |

### Skill 详情

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/dashboard/skill/{name}/detail` | skill 详情 |
| GET | `/api/v1/dashboard/skill/{name}/tree` | 文件树 |
| GET | `/api/v1/dashboard/skill/{name}/file` | 读文件内容 |
| GET | `/api/v1/dashboard/skill/{name}/diff` | git diff |
| GET | `/api/v1/dashboard/skill/{name}/trigger` | 触发优化概览 |
| GET | `/api/v1/dashboard/skill/{name}/trigger/cases` | 触发 case 列表 |
| POST | `/api/v1/dashboard/skill/{name}/trigger/rerun` | 重跑触发优化 |

### ★ UX 得分查询（本 PR 新增）

#### GET `/api/v1/dashboard/skill/{name}/ux` — 自有 skill ux 按版本聚合

**Query**:
| 参数 | 类型 | 缺省 | 说明 |
|---|---|---|---|
| `side` | `main`\|`staging` | null(两侧合并) | 过滤侧 |
| `days` | int | 30 | 近 N 天 |

**Response 200**:
```json
{
  "skill": "python-basics",
  "versions": [
    {
      "commit_sha": "abc123...",          // git commit sha（40 位）
      "side": "main",
      "count": 3,                          // 该版本 ux 分条数
      "avg": 8.0,                          // 均分
      "first_scored_at": "2026-07-07T...",
      "last_scored_at": "2026-07-07T..."
    }
  ],
  "current_version": {"main": "abc123...", "staging": null}
}
```
- `versions` 按 `commit_sha` 分组，`side=null` 时同 sha 上 main+staging 合到一组（`side` 标 `"mixed"`）。
- **版本演进**：同名 skill 改了内容 → 新 commit sha → 旧版本分仍保留在 `.ux_scores.jsonl`，各自分组聚合，不混算。

#### GET `/api/v1/dashboard/skill/{name}/ux/atoms` — 自有 skill ux 明细 + 关联 atom

**Query**:
| 参数 | 类型 | 缺省 | 说明 |
|---|---|---|---|
| `side` | `main`\|`staging` | null | 过滤侧 |
| `commit_sha` | string | null(全版本) | 指定版本；不传=全历史 |
| `days` | int | 30 | 近 N 天 |

**Response 200**:
```json
{
  "skill": "python-basics",
  "atom_lookup": "ok",                    // "unavailable" = team traj_root 不可达
  "scores": [
    {
      "atom_id": "atom_traj_xxx_0001",
      "commit_sha": "abc123...",
      "side": "main",
      "score": 8.0,
      "reasons": "...",
      "scored_at": "2026-07-07T...",
      "user_model": "deepseek-v4-flash",
      "atom": {                            // 关联的 atom 内容；atom 文件不存在或 traj_root 不可达 → null
        "traj_id": "traj_xxx",
        "summary": "...",
        "intent": "...",
        "tags": ["python", "loop"],
        "used_skills": ["python-basics"]
      }
    }
  ]
}
```
- `atom` 字段：从 `<traj_root>/clients/*/sessions/*/tasks/<atom_id>.json` 反查。
- `atom_lookup: "unavailable"`：dashboard 不在 team 模式 / traj_root 不可达 → 所有 `atom: null`（不抛错）。
- `commit_sha` 不传 → 返回全版本明细，每条带 `commit_sha` 字段，调用方可自行分组。

#### GET `/api/v1/dashboard/skillhub/{name}/ux` — 三方 skillhub skill ux 按版本聚合

**Response 200**:
```json
{
  "skill": "pytest-fixtures",
  "versions": [
    {
      "commit_sha": "e384aa165b1a6488",   // = sha256(SKILL.md 内容)[:16]（无 git → 内容哈希，16 位）
      "side": "main",                      // 三方 skill 无 staging，恒 main
      "count": 3,
      "avg": 8.0,
      "first_scored_at": "...",
      "last_scored_at": "..."
    }
  ],
  "current_version": {"content_sha": "e384aa165b1a6488"}
}
```
- **skillhub 禁用或 skill 不存在 → 404**。
- 版本号口径与自有 skill 不同：自有=git commit sha（40 位），三方=`sha256(SKILL.md)[:16]`（16 位，仅 SKILL.md 内容）。

#### GET `/api/v1/dashboard/skillhub/{name}/ux/atoms` — 三方 skillhub skill ux 明细 + 关联 atom

**Query**: `commit_sha`、`days`（同自有 skill；三方无 `side` 参数，恒 main）

**Response 200**: 同 `/skill/{name}/ux/atoms` 结构（`scores[].atom` 关联逻辑相同）。

---

## 四、版本号（sha）口径说明

| skill 来源 | sha 算法 | 长度 | 含义 |
|---|---|---|---|
| 自有 skill（`skill_dir`，有 git） | `git rev-parse main` / `staging` | 40 位 | 整个 skill 目录的 git commit 快照 |
| 三方 skillhub skill（`skillhub_dir`，无 git） | `sha256(SKILL.md 文件字节)[:16]` | 16 位 | 仅 SKILL.md 内容版本（不含脚本/辅助文件） |

**不统一是有意为之**：自有 skill 的 git sha 是 xskill canary 体系（`check_and_decide`、`.ux_scores.jsonl` 的 `commit_sha`）的既有版本键；三方 skill 无 git，用内容哈希作版本号。两者各自闭环，不混用。

---

## 五、鉴权与配置速查

| 端点组 | 鉴权 | 配置开关 |
|---|---|---|
| `/api/v1/team/*` | `X-Xskill-Token` + `X-Xskill-Client`（register 除外） | `xskill serve --server` |
| `/api/v1/skills/*`、`/canary/*`、`/trajectories/*`、`/registry/*`、`/reindex` | 无（standalone 默认本机） | 默认开 |
| `/api/v1/dashboard/*` | 可选 `dashboard.password`（HTTP Basic） | `dashboard.enabled: true`；`dashboard.public: true` 放行公网 |

**关键 config 段**（本 PR 相关）：
```yaml
team:
  server:
    allow_anonymous_user: false   # true=允许匿名 connect；false=强制 --name
    skill_slots: 100              # manifest 总槽位
    ranked_slots: 80              # 其中 ux 滑窗占；其余 = recommended(画像相关性)
recommend:
  quality_ratio: 0.8              # recommended 桶里质量位占比；其余=相关性
  cluster_centers: 5              # 用户兴趣聚类中心上限
  last_n_atoms: 5                 # skill.atom_feat 取最近 N atom
  staging_need: 3                 # 可选；缺省=复用 canary.min_samples
skillhub:
  enabled: false                  # 三方 skill 目录扫描开关
  dir: ~/.xskill/skillhub_skills
dashboard:
  enabled: false                  # dashboard API 开关
  public: false                   # 公网可达
```
