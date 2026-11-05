"""Local dashboard control for replacing the native distillation worker group."""
from __future__ import annotations

import asyncio
import logging
from contextlib import ExitStack
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from xskill.dashboard.auth import require_admin
from xskill.pipeline.distillation import DistillationStore, WORKER_GENERATION_ENV, WORKER_ROOT_ENV
from xskill.skill.catalog_store import search_native_skill_names
from xskill.skill.write_policy import library_write_guard
from xskill.tasks.locking import root_run_lock
from xskill.utils.status_file import AGENT_WORKER_STATUS_FILE, read_status_file


ROUTE = "/api/v1/dashboard/distillation"
logger = logging.getLogger(__name__)


class SwitchRequest(BaseModel):
    mode: Literal["auto", "create_one", "update_only"]
    skill: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    generation: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_fields(self):
        if self.mode == "auto" and (self.skill is not None or self.description is not None):
            raise ValueError("自动蒸馏不接受目标技能或说明")
        if self.mode != "auto" and not self.skill:
            raise ValueError("请指定目标技能")
        if self.mode == "create_one" and not (self.description or "").strip():
            raise ValueError("请填写新技能说明")
        if self.mode == "update_only" and self.description is not None:
            raise ValueError("更新模式使用已有技能的说明")
        return self


class DistillationControl:
    def __init__(self, store: DistillationStore, check_runtime_config):
        self.store = store
        self.check_runtime_config = check_runtime_config
        self.writers = []
        self.fence = ExitStack()
        self.phase = "idle"
        self.error = None
        self.in_flight_writes = 0
        self.task: asyncio.Task | None = None

    @property
    def switching(self) -> bool:
        return self.task is not None and not self.task.done()

    def replace_fence(self, policy):
        self.fence.close()
        self.fence.enter_context(library_write_guard(
            self.store.skill_dir, policy, db_path=self.store.db_path, bind_context=False,
        ))

    async def status(self):
        while True:
            phase = self.phase
            policy, run = await asyncio.to_thread(self._configuration)
            if phase == self.phase:
                break
        return {
            "mode": policy.mode, "target": policy.target_skill, "generation": policy.generation,
            "phase": phase, "error": self.error,
            "description": run["description_snapshot"] if run and policy.mode == "create_one" else None,
        }

    def _configuration(self):
        policy = self.store.policy()
        run = self.store.run() if policy.mode == "create_one" else None
        return policy, run if run and run["run_id"] == policy.run_id else None

    def start_switch(self, req: SwitchRequest):
        if not self.writers:
            raise HTTPException(503, "蒸馏调度尚未就绪")
        if self.switching or self.in_flight_writes:
            raise HTTPException(409, "有其他操作正在进行，请稍后再试")
        self.phase = "stopping"
        self.error = None
        self.task = asyncio.create_task(self._switch(req))

    async def _stop_writers(self):
        stopped = await asyncio.gather(
            *(asyncio.to_thread(writer.stop_gracefully) for writer in self.writers),
            return_exceptions=True,
        )
        for result in stopped:
            if isinstance(result, BaseException):
                raise result
            if not result:
                raise RuntimeError("旧后台进程尚未退出，请稍后重试")

    def _activate(self, req: SwitchRequest):
        # The lifespan retains the host lock. No supervisor is running while
        # the worker lock and the old policy fence protect this transition.
        with root_run_lock(self.store.db_path.parent, self.store.skill_dir):
            self.store._reject_live_host(owned_host=True)
            self.check_runtime_config()
            current = self.store.policy()
            if current.generation != req.generation:
                raise RuntimeError("蒸馏配置已变化，请刷新后重试")
            run = self.store.run()
            if (run and req.mode == current.mode and req.skill == current.target_skill
                    and (req.mode != "create_one"
                         or req.description.strip() == run["description_snapshot"].strip())):
                # A retry can restart the existing run after startup failed.
                self.store._configure_locked(resume=current.run_id)
            else:
                self.store._configure_locked(req.mode, req.skill, req.description)
            policy = self.store.policy()
            self.replace_fence(policy)
            return policy.generation

    async def _wait_for_worker(self, generation: int):
        worker = self.writers[0]
        status_path = self.store.db_path.parent / AGENT_WORKER_STATUS_FILE
        while True:
            heartbeat = await asyncio.to_thread(read_status_file, status_path) or {}
            stats = heartbeat.get("stats") or {}
            if (heartbeat.get("active") is True and heartbeat.get("ok")
                    and heartbeat.get("pid") == worker.child_pid
                    and stats.get("distillation_generation") == generation
                    and stats.get("distillation_root") == self.store.root_key):
                return
            await asyncio.sleep(0.25)

    async def _switch(self, req: SwitchRequest):
        try:
            await self._stop_writers()
            self.phase = "applying"
            generation = await asyncio.to_thread(self._activate, req)
            self.phase = "starting"
            admission = {WORKER_ROOT_ENV: self.store.root_key,
                         WORKER_GENERATION_ENV: str(generation)}
            for writer in self.writers:
                writer.start(env=admission)
            await asyncio.wait_for(self._wait_for_worker(generation), timeout=30)
        except Exception as exc:
            logger.exception("distillation mode switch failed")
            if self.phase == "starting":
                try:
                    await self._stop_writers()
                except Exception:
                    logger.exception("failed to stop distillation writers after startup failure")
            self.error = str(exc) if str(exc) else "等待后台蒸馏启动超时"
            self.phase = "failed"
        else:
            self.phase = "ready"


def build_distillation_router(control: DistillationControl) -> APIRouter:
    router = APIRouter(prefix=ROUTE, dependencies=[Depends(require_admin)])

    @router.get("")
    async def status():
        return await control.status()

    @router.get("/skills")
    def skills(q: str = Query("", max_length=255), offset: int = Query(0, ge=0)):
        return search_native_skill_names(
            control.store.skill_dir, db_path=control.store.db_path, q=q, offset=offset,
        )

    @router.post("", status_code=202)
    async def switch(req: SwitchRequest, request: Request):
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            raise HTTPException(403, "请在本控制台应用蒸馏模式")
        if req.generation != (await control.status())["generation"]:
            raise HTTPException(409, "蒸馏配置已变化，请刷新后重试")
        control.start_switch(req)
        return {"phase": control.phase}

    return router
