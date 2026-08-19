"""protocol.py — C/S 线协议模型（SP1）

C 与 S 之间所有 HTTP body 的单一事实源。端点：

  POST /api/v1/team/register          RegisterRequest  -> RegisterResponse
  POST /api/v1/team/upload            UploadRequest    -> UploadResponse
  GET  /api/v1/team/sync              (query)          -> SyncResponse
  GET  /api/v1/team/skill/{n}/bundle  (query)          -> application/octet-stream
  POST /api/v1/team/push-edit         (multipart)      -> PushEditResponse
  POST /api/v1/team/generate          GenerateRequest  -> {job_id}
  GET  /api/v1/team/generate/{id}/events  SSE 日志流

鉴权（除 register 外所有端点）：HTTP header
  X-Xskill-Token   = server join token
  X-Xskill-Client  = client_id
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Side = Literal["main", "staging"]
Bucket = Literal["pinned", "ranked", "recommended"]  # P2-2.4:pinned=用户/admin 钉住
SkillSource = Literal["repo", "skillhub"]


class RegisterRequest(BaseModel):
    token: str
    client_label: str = ""
    hostname: str = ""
    # client 自报本地 state 里已有的 client_id，希望 server 续用——
    # server 按优先级判定（详见 client_registry.register）；None = 客户端
    # 没有历史身份（首次连接或 state 丢失），server 自行新发或按指纹回查。
    claimed_client_id: str | None = None
    # 显式身份键 ``--name <userid>``。非空时 server 派生确定性 client_id（跨设备
    # 同 name 共享画像），优先于 claimed_client_id / 指纹回查。None = 匿名
    # （回退 hashid/uuid 逻辑；受 server allow_anonymous_user 闸门）。
    user_name: str | None = None
    # P2-2.10:client 自报 xskill 版本。server 写 clients.client_version,
    # 连接状态看板据此标注"落后"。空串=旧 client 未上报。
    client_version: str = ""


class RegisterResponse(BaseModel):
    client_id: str
    # P2-2.2(Q2a):--name 注册时发放的 dashboard 登录 token,client 侧打印一次。
    # 匿名注册为 None(dashboard 登录依赖 user_name 身份)。
    dashboard_token: str | None = None


class UploadTrajectory(BaseModel):
    traj_id: str           # 形如 traj_cc_<project>_<sid>，必须 traj_ 前缀
    content: str           # 已脱敏的 markdown 全文
    sha256: str            # content 的 sha256，server 端去重用
    model: str = ""        # 产生该轨迹的用户 agent 模型（取自本机 .json sidecar；
    #                        只带 model 一字段，不带 cwd/query 等未脱敏元信息）
    harness: str = ""      # 产生该轨迹的用户 coding agent（harness，如 claude_code /
    #                        codex / opencode）；client 按本机 bridge 目录推断。
    #                        server 端据此做"按 coding agent 分组"统计，替代把所有
    #                        team 上传一律标成 team_client。


class UploadRequest(BaseModel):
    trajectories: list[UploadTrajectory] = Field(default_factory=list)


class UploadRejection(BaseModel):
    traj_id: str
    reason: str


class UploadResponse(BaseModel):
    accepted: list[str] = Field(default_factory=list)
    rejected: list[UploadRejection] = Field(default_factory=list)


class SkillSlot(BaseModel):
    """client 应持有的一个 skill 槽位。side/sha 由 server 现算（pick_side + git 状态）。"""
    skill_name: str
    side: Side
    sha: str
    bucket: Bucket         # ranked = ux_score 滑窗；recommended = SP3 画像位（SP1 占位）
    source: SkillSource = "repo"
    display_name: str | None = None
    source_path: str | None = None


class SyncResponse(BaseModel):
    slots: list[SkillSlot] = Field(default_factory=list)   # ≤ skill_slots
    server_time: float
    # client 截取安装：对 slots 取前 take_n；None=装全部（兼容旧 client）
    take_n: int | None = None
    server_slots: int | None = None


class PushEditResponse(BaseModel):
    branch: str            # user-staging/<client_id>
    ref_sha: str


class GenerateRequest(BaseModel):
    instruction: str
    names: list[str] = Field(default_factory=list)


class GenerateAccepted(BaseModel):
    job_id: str

