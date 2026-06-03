"""protocol.py — C/S 线协议模型（SP1）

C 与 S 之间所有 HTTP body 的单一事实源。端点：

  POST /api/v1/team/register          RegisterRequest  -> RegisterResponse
  POST /api/v1/team/upload            UploadRequest    -> UploadResponse
  GET  /api/v1/team/sync              (query)          -> SyncResponse
  GET  /api/v1/team/skill/{n}/bundle  (query)          -> application/octet-stream
  POST /api/v1/team/push-edit         (multipart)      -> PushEditResponse

鉴权（除 register 外所有端点）：HTTP header
  X-Xskill-Token   = server join token
  X-Xskill-Client  = client_id
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Side = Literal["main", "staging"]
Bucket = Literal["ranked", "recommended"]


class RegisterRequest(BaseModel):
    token: str
    client_label: str = ""
    hostname: str = ""
    # client 自报本地 state 里已有的 client_id，希望 server 续用——
    # server 按优先级判定（详见 client_registry.register）；None = 客户端
    # 没有历史身份（首次连接或 state 丢失），server 自行新发或按指纹回查。
    claimed_client_id: str | None = None


class RegisterResponse(BaseModel):
    client_id: str


class UploadTrajectory(BaseModel):
    traj_id: str           # 形如 traj_cc_<project>_<sid8>，必须 traj_ 前缀
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


class SyncResponse(BaseModel):
    slots: list[SkillSlot] = Field(default_factory=list)   # ≤100
    server_time: float


class PushEditResponse(BaseModel):
    branch: str            # user-staging/<client_id>
    ref_sha: str
