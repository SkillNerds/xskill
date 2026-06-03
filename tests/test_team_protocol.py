from __future__ import annotations

from xskill.team.shared.protocol import (
    RegisterRequest, RegisterResponse,
    UploadTrajectory, UploadRequest, UploadResponse,
    SkillSlot, SyncResponse, PushEditResponse,
)


def test_register_roundtrip():
    req = RegisterRequest(token="abc", client_label="alice-laptop", hostname="alice")
    assert RegisterRequest.model_validate(req.model_dump()) == req
    resp = RegisterResponse(client_id="cid-1")
    assert RegisterResponse.model_validate(resp.model_dump()).client_id == "cid-1"


def test_upload_roundtrip():
    req = UploadRequest(trajectories=[
        UploadTrajectory(traj_id="traj_cc_x_001", content="# hi", sha256="deadbeef"),
    ])
    back = UploadRequest.model_validate(req.model_dump())
    assert back.trajectories[0].traj_id == "traj_cc_x_001"
    assert back.trajectories[0].model == ""        # 默认空，老 client 不带也能解析
    resp = UploadResponse(accepted=["traj_cc_x_001"], rejected=[])
    assert UploadResponse.model_validate(resp.model_dump()).accepted == ["traj_cc_x_001"]


def test_upload_carries_model():
    req = UploadRequest(trajectories=[
        UploadTrajectory(traj_id="traj_cc_x_001", content="# hi", sha256="dead",
                         model="claude-opus-4-7"),
    ])
    back = UploadRequest.model_validate(req.model_dump())
    assert back.trajectories[0].model == "claude-opus-4-7"


def test_sync_response_slots():
    slot = SkillSlot(skill_name="fix-foo", side="staging", sha="abc123", bucket="ranked")
    resp = SyncResponse(slots=[slot], server_time=1.0)
    back = SyncResponse.model_validate(resp.model_dump())
    assert back.slots[0].side == "staging" and back.slots[0].bucket == "ranked"


def test_skill_slot_rejects_bad_side():
    import pytest
    with pytest.raises(Exception):
        SkillSlot(skill_name="x", side="prod", sha="abc", bucket="ranked")
