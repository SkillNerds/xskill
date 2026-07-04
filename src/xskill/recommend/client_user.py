"""client_user.py — §4 用户实体

面向对象的用户总类：身份、画像、used_skills / user_skills / recommended_skills 追踪。
``used_skills`` / ``recommended_skills`` 为 list-of-dict，便于持久化与反查。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from xskill.recommend.client_interest import ClientInterest


class ClientUser:
    """单个 team client 的用户视图。

    - ``client_interest``：该用户的 ``ClientInterest`` 画像（可能 None，冷启动）。
    - ``used_skills``：listofdict ``{name, use_count, avg_score}``，源自其 atom 的
      ``used_skills`` + UX 分，增量更新。
    - ``user_skills``：本机已加载 skill 的状态视图（skill 名列表）。
    - ``recommended_skills``：listofdict ``{skill, branch, hash}``，引擎推荐记录。
    """

    def __init__(
        self,
        user_id: str,
        *,
        client_interest: "Optional[ClientInterest]" = None,
        used_skills: Optional[list[dict]] = None,
        user_skills: Optional[list[str]] = None,
        recommended_skills: Optional[list[dict]] = None,
    ):
        self.user_id = user_id
        self.client_interest = client_interest
        self.used_skills: list[dict] = list(used_skills or [])
        self.user_skills: list[str] = list(user_skills or [])
        self.recommended_skills: list[dict] = list(recommended_skills or [])

    def __repr__(self) -> str:
        return f"ClientUser({self.user_id!r}, used={len(self.used_skills)}, reco={len(self.recommended_skills)})"
