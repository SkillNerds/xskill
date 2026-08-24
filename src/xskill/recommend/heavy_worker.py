"""重活进程：Milvus 对账 + 脏用户推荐预计算（与 Web GIL 隔离）。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("xskill.recommend.heavy_worker")


VECTOR_RECONCILE_INTERVAL_SECONDS = 24 * 60 * 60
VECTOR_SYNC_ALGORITHM = "catalog-vector-dirty-v1"


def _load_catalog_rows(
    db_path: Path,
    *,
    catalog_keys: Optional[list[str]] = None,
) -> list[dict]:
    from xskill.pipeline.registry import pooled_connection

    with pooled_connection(db_path) as conn:
        where = ""
        params: list = []
        if catalog_keys is not None:
            if not catalog_keys:
                return []
            placeholders = ",".join("?" for _ in catalog_keys)
            where = f"WHERE c.catalog_key IN ({placeholders})"
            params.extend(catalog_keys)
        rows = conn.execute(
            f"""
            SELECT c.catalog_key, c.name, c.source, c.description,
                   c.content_sha, c.skill_id, c.distributable,
                   CASE WHEN l.state='retired' THEN 1 ELSE 0 END AS retired
            FROM skills_catalog AS c
            LEFT JOIN skill_lifecycle AS l ON l.skill_name=c.name
            {where}
            """,  # noqa: S608 -- placeholders carry all external values
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def _row_target(row: dict | None):
    if row is None:
        return None
    from xskill.recommend.skill_vector_store import (
        catalog_row_is_indexable,
        content_sha_for_text,
    )

    if not catalog_row_is_indexable(row):
        return None
    description = (row.get("description") or "").strip()
    return (
        row.get("content_sha") or content_sha_for_text(description),
        row.get("source") or "",
        row.get("name") or "",
        description,
    )


def _load_catalog_row(db_path: Path, catalog_key: str) -> dict | None:
    rows = _load_catalog_rows(db_path, catalog_keys=[catalog_key])
    return rows[0] if rows else None


def run_vector_reconcile(
    *,
    db_path: Path,
    vector_db_path: Path,
    embed=None,
    memory_index=None,
    force_upsert: bool = False,
    should_apply=None,
) -> dict:
    from xskill.recommend.skill_vector_store import (
        DEFAULT_DIM,
        fake_embed,
        open_skill_vector_index,
        reconcile_catalog_to_index,
    )

    rows = _load_catalog_rows(db_path)
    embed_fn = embed or (lambda text: fake_embed(text, DEFAULT_DIM))
    index = memory_index or open_skill_vector_index(vector_db_path, dim=DEFAULT_DIM)
    stats = reconcile_catalog_to_index(
        index,
        rows,
        embed=embed_fn,
        force_upsert=force_upsert,
        should_apply=should_apply,
    )
    logger.info(
        "vector reconcile: upserted=%s deleted=%s skipped=%s",
        stats["upserted"], stats["deleted"], stats["skipped"],
    )
    return stats


def _full_apply_fence(
    *,
    db_path: Path,
    snapshot_generations: dict[str, int],
):
    """构造低频全量对账的逐 key fence，避免写回扫描后的旧快照。"""
    from xskill.pipeline.registry import pooled_connection
    from xskill.recommend.vector_dirty import (
        mark_catalog_vector_dirty_on_connection,
    )

    def _should_apply(catalog_key: str, snapshot_row: dict | None) -> bool:
        with pooled_connection(db_path) as conn:
            event = conn.execute(
                """
                SELECT generation FROM catalog_vector_dirty
                WHERE catalog_key=? AND dirty=1
                """,
                (catalog_key,),
            ).fetchone()
            current_generation = int(event["generation"]) if event else None
            expected_generation = snapshot_generations.get(catalog_key)
            if current_generation != expected_generation:
                return False
        current_row = _load_catalog_row(db_path, catalog_key)
        if _row_target(current_row) == _row_target(snapshot_row):
            return True
        # 非标准写入没有产生事件时也不写旧快照，并补一个可恢复的脏项。
        target = _row_target(current_row)
        with pooled_connection(db_path) as conn:
            mark_catalog_vector_dirty_on_connection(
                conn,
                catalog_key,
                operation="upsert" if target is not None else "delete",
                content_sha=target[0] if target is not None else "",
            )
            conn.commit()
        return False

    return _should_apply


def run_vector_sync(
    *,
    db_path: Path,
    vector_db_path: Path,
    index,
    embed,
    model_fingerprint: str,
    force_full: bool = False,
    now: float | None = None,
    limit: int = 256,
) -> dict:
    """优先消费增量队列；首次/模型变化/低频周期执行全量修复。"""
    from xskill.recommend.skill_vector_store import indexable_catalog_rows
    from xskill.recommend.vector_dirty import (
        catalog_vector_event_is_current,
        catalog_vector_reconcile_reason,
        clear_catalog_vector_dirty,
        finish_catalog_vector_reconcile,
        list_all_catalog_vector_generations,
        list_catalog_vector_dirty,
    )

    reason = "ephemeral" if force_full else catalog_vector_reconcile_reason(
        model_fingerprint,
        db_path=db_path,
        now=now,
        interval_seconds=VECTOR_RECONCILE_INTERVAL_SECONDS,
    )
    if reason:
        generations = list_all_catalog_vector_generations(db_path=db_path)
        stats = run_vector_reconcile(
            db_path=db_path,
            vector_db_path=vector_db_path,
            embed=embed,
            memory_index=index,
            # 升级 bootstrap 先复用旧索引中 content/source/name 一致的向量，
            # 避免部署本 PR 就为整个 catalog 重付 embedding 成本。模型水位建立后，
            # 真正的模型切换会强制全量重算；空/临时索引自然逐条 miss。
            force_upsert=reason in {"model_changed", "ephemeral"},
            should_apply=_full_apply_fence(
                db_path=db_path,
                snapshot_generations=generations,
            ),
        )
        finish_catalog_vector_reconcile(
            generations,
            model_fingerprint=model_fingerprint,
            reconciled_at=time.time() if now is None else now,
            db_path=db_path,
        )
        return {**stats, "mode": "full", "reason": reason}

    events = list_catalog_vector_dirty(db_path=db_path, limit=limit)
    rows = _load_catalog_rows(
        db_path,
        catalog_keys=[event["catalog_key"] for event in events],
    )
    rows_by_key = {row["catalog_key"]: row for row in rows}
    stats = {"upserted": 0, "deleted": 0, "skipped": 0, "deferred": 0}
    for event in events:
        key = event["catalog_key"]
        generation = int(event["generation"])
        row = rows_by_key.get(key)
        wanted = indexable_catalog_rows([row]) if row is not None else []
        if wanted:
            target = wanted[0]
            vector = embed(target["description"])
            if not catalog_vector_event_is_current(
                key, generation, db_path=db_path,
            ):
                stats["deferred"] += 1
                continue
            index.upsert(
                key,
                vector,
                content_sha=target["content_sha"],
                source=target.get("source") or "",
                name=target.get("name") or "",
            )
            stats["upserted"] += 1
        else:
            if not catalog_vector_event_is_current(
                key, generation, db_path=db_path,
            ):
                stats["deferred"] += 1
                continue
            index.delete(key)
            stats["deleted"] += 1
        if not clear_catalog_vector_dirty(key, generation, db_path=db_path):
            stats["deferred"] += 1
    return {**stats, "mode": "incremental", "reason": ""}


def _skill_name_from_index(vector_index, catalog_key: str) -> str:
    row = vector_index.get(catalog_key)
    if row:
        name = (row.get("name") or "").strip()
        if name:
            return name
        if row.get("source") == "skillhub" and ":" in catalog_key:
            return catalog_key.split(":", 1)[-1]
    if ":" in catalog_key:
        return catalog_key.split(":", 1)[-1]
    return catalog_key


def compute_recommend_for_user(
    user_key: str,
    *,
    db_path: Path,
    vector_index,
    top_k: int = 20,
    profile_centers: Optional[list[list[float]]] = None,
) -> list[str]:
    """用画像中心向量在索引里 search；无中心则写空推荐（sync 侧走 ranked/ux）。"""
    from xskill.recommend.recommend_store import save_recommend_slots

    if not profile_centers:
        save_recommend_slots(user_key, [], fingerprint="no_profile", db_path=db_path)
        return []

    # 每个中心独立召回，再按中心轮询取一个未出现过的技能。直接把第一个
    # 中心的结果填满 top_k 会让后续兴趣永远没有机会进入推荐槽位。
    center_hits = [
        vector_index.search(center, top_k=top_k)
        for center in profile_centers
    ]
    positions = [0] * len(center_hits)
    names: list[str] = []
    seen: set[str] = set()
    source_centers: list[int] = []
    while len(names) < top_k:
        progress = False
        for center_index, hits in enumerate(center_hits):
            while positions[center_index] < len(hits):
                catalog_key, _score = hits[positions[center_index]]
                positions[center_index] += 1
                name = _skill_name_from_index(vector_index, catalog_key)
                if name in seen:
                    continue
                seen.add(name)
                names.append(name)
                source_centers.append(center_index)
                progress = True
                break
            if len(names) >= top_k:
                break
        if not progress:
            break
    fingerprint = (
        f"centers={len(profile_centers)};fusion=round_robin_v1;"
        f"sources={','.join(map(str, source_centers))}"
    )
    save_recommend_slots(
        user_key, names, fingerprint=fingerprint, db_path=db_path,
    )
    return names


def _user_key_for_client(engine, client_id: str) -> str:
    reg = getattr(engine, "client_registry", None)
    if reg is not None:
        try:
            name = reg.user_name_for(client_id)
            if name:
                return name
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return client_id


def _client_id_for_user_key(engine, user_key: str) -> str:
    """推荐表键 → 画像键（client_id）。"""
    if not user_key:
        return user_key
    reg = getattr(engine, "client_registry", None)
    if reg is None:
        return user_key
    try:
        for row in reg.list():
            cid = row["client_id"]
            if cid == user_key:
                return cid
            try:
                if reg.user_name_for(cid) == user_key:
                    return cid
            except Exception:  # pylint: disable=broad-exception-caught
                continue
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("client_id resolve failed for %s", user_key, exc_info=True)
    return user_key


def _load_profile_centers(engine, client_id: str) -> Optional[list[list[float]]]:
    try:
        user = engine.load_client_user(client_id, include_recommended=False)
        ci = user.client_interest
        if ci is None or ci.feature_tensor is None:
            return None
        return [list(map(float, row)) for row in ci.feature_tensor]
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("profile centers unavailable for %s", client_id, exc_info=True)
        return None


def process_dirty_recommends(
    *,
    db_path: Path,
    vector_index,
    engine,
    limit: int = 32,
) -> int:
    from xskill.recommend.recommend_store import (
        clear_recommend_dirty,
        list_dirty_user_keys,
        save_recommend_slots,
    )

    keys = list_dirty_user_keys(limit=limit, db_path=db_path)
    done = 0
    for user_key in keys:
        try:
            client_id = _client_id_for_user_key(engine, user_key)
            centers = _load_profile_centers(engine, client_id)
            compute_recommend_for_user(
                user_key,
                db_path=db_path,
                vector_index=vector_index,
                profile_centers=centers,
            )
            done += 1
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("recommend dirty failed user_key=%s", user_key)
            clear_recommend_dirty(user_key, db_path=db_path)
            save_recommend_slots(user_key, [], fingerprint="error", db_path=db_path)
    return done


def _embed_fn_from_engine(engine):
    """从引擎 embed_client 构造 embed(text)->list[float]；不可用则 None。"""
    client = getattr(engine, "embed_client", None)
    if client is None or not hasattr(client, "encode"):
        return None

    def _embed(text: str) -> list[float]:
        vec = client.encode(text)
        return [float(x) for x in vec]

    return _embed


def _vector_index_identity(index, vector_db_path: Path) -> str:
    """廉价识别持久索引实例；同路径数据库被重建后强制 bootstrap。"""
    stored_path = getattr(index, "db_path", None)
    if stored_path is not None:
        path = Path(stored_path).expanduser().resolve()
        try:
            stat = path.stat()
            return f"file:{path}:{stat.st_dev}:{stat.st_ino}"
        except OSError:
            return f"file:{path}:missing"
    # 显式注入的内存/测试索引只在当前进程实例内可复用。
    return (
        f"object:{type(index).__module__}.{type(index).__qualname__}:"
        f"{id(index)}:{Path(vector_db_path).expanduser().resolve()}"
    )


def run_recommend_heavy_once(
    *,
    engine,
    db_path: Path | None = None,
    vector_db_path: Path | None = None,
    memory_index=None,
    mark_catalog_dirty: bool = True,
) -> dict:
    """对账向量索引并消化推荐脏队列（画像刷新由调用方先跑）。"""
    from xskill.config import XSKILL_HOME, get_registry_db_path
    from xskill.recommend.recommend_store import mark_all_recommend_dirty
    from xskill.recommend.skill_vector_store import (
        DEFAULT_DIM,
        MemorySkillVectorIndex,
        default_vector_db_path,
        fake_embed,
        open_skill_vector_index,
    )

    registry = Path(db_path) if db_path else get_registry_db_path()
    vdb = Path(vector_db_path) if vector_db_path else default_vector_db_path(XSKILL_HOME)
    embed_fn = _embed_fn_from_engine(engine)
    if embed_fn is None:
        embed_fn = lambda text: fake_embed(text, DEFAULT_DIM)  # noqa: E731
        dim = DEFAULT_DIM
        model_fingerprint = f"{VECTOR_SYNC_ALGORITHM}:fake:{dim}"
    else:
        client = engine.embed_client
        dim = int(getattr(client, "dim", 0) or 0)
        if dim <= 0:
            # 正常 EmbedClient 在 engine 构造时已 probe；仅兼容自定义 client。
            dim = len(embed_fn("dimension probe"))
        model = str(getattr(client, "model", "") or "unknown")
        model_fingerprint = f"{VECTOR_SYNC_ALGORITHM}:{model}:{dim}"
    # open_skill_vector_index：无 pymilvus 时退回内存索引并 hourly warn
    index = memory_index or open_skill_vector_index(vdb, dim=dim)
    model_fingerprint = (
        f"{model_fingerprint}:{_vector_index_identity(index, vdb)}"
    )
    # 生产 fallback 每次都会创建空的内存索引，必须全量填充；调用方显式传入的
    # memory_index 可跨 tick 复用，仍走增量路径（用于测试/嵌入式调用）。
    ephemeral_index = memory_index is None and isinstance(
        index, MemorySkillVectorIndex,
    )
    vec_stats = run_vector_sync(
        db_path=registry,
        vector_db_path=vdb,
        embed=embed_fn,
        index=index,
        model_fingerprint=model_fingerprint,
        force_full=ephemeral_index,
    )
    if mark_catalog_dirty and (
        vec_stats.get("upserted", 0) or vec_stats.get("deleted", 0)
    ):
        mark_all_recommend_dirty(reason="catalog_vector_changed", db_path=registry)
    n = process_dirty_recommends(
        db_path=registry, vector_index=index, engine=engine,
    )
    return {"vector": vec_stats, "recommends": n}
