"""把用户已有技能目录纳入自有仓（#213）。

落点永远是 ``skill_dir/<name>/``。同名不删整个目录，只换成源目录里的技能
文件，再在现有 main 上多一次提交。辅助文件（体验分、candidates 等）不动。
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from xskill.skill.git import (
    SKILL_GITIGNORE,
    commit_update_main_branch,
    current_branch,
    ensure_head_on_main,
    init_imported_repo_on_main,
    run_git,
    skill_repo_lock,
)

logger = logging.getLogger("xskill.skill.importer")

# rebuild --force 认这个文件和 ``import:`` 提交，避免清掉用户纳入的技能。
ORIGIN_FILENAME = ".xskill-origin"
ORIGIN_IMPORT = "import"
_KEEP_COMMIT_PREFIXES = ("import:",)

RUNTIME_SIDECARS = frozenset({
    ".candidates.yml",
    ".ux_scores.jsonl",
    ".lock",
    ".canary_jam_state.json",
    ".scripting_requested",
})
RUNTIME_SIDECAR_DIRS = frozenset({
    ".git",
    ".canary",
    ".description_optimization",
    ".repo_locks",
})

HARNESS_SKILL_PARENTS = (
    Path(".claude") / "skills",
    Path(".agents") / "skills",
    Path(".cursor") / "skills",
    Path(".codex") / "skills",
    Path(".cac") / "skills",
    Path(".config") / "opencode" / "skills",
    Path(".trae") / "skills",
    Path(".trae-cn") / "skills",
)

HARNESS_IMPORT_WARNING = (
    "源目录是编程代理技能目录。纳入完成后，这份目录会被换成自有仓里的新版本。"
    "若有未提交或未跟踪的内容，会先整份拷到 ~/.xskill/import-stash/<技能名>/<时间>/。"
)


@dataclass
class ImportResult:
    name: str
    existed: bool
    sha: str = ""
    baby_overwritten: bool = False
    staging_kept: bool = False
    main_round_scores_cleared: int = 0
    warnings: list[str] = field(default_factory=list)
    stash_path: str = ""
    pinned: list[str] = field(default_factory=list)


def is_skill_source_dir(path: Path) -> bool:
    return (path / "SKILL.md").is_file() or (path / "skill.md").is_file()


def discover_import_sources(path: Path) -> list[Path]:
    """单个技能目录，或父目录下每个含 SKILL.md 的子目录。"""
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"source not found: {path}")
    resolved = path.resolve()
    if is_skill_source_dir(resolved):
        return [resolved]
    if not resolved.is_dir():
        raise FileNotFoundError(f"source not found: {path}")
    found = [
        child.resolve()
        for child in sorted(resolved.iterdir())
        if child.is_dir() and is_skill_source_dir(child)
    ]
    if not found:
        raise FileNotFoundError(
            f"{path} 下没有 SKILL.md，也不是含多个技能的父目录",
        )
    return found


def is_harness_skill_path(path: Path, *, home_root: Path | None = None) -> bool:
    """源目录是不是编程代理技能目录（或其符号链接解析到那里）。"""
    home = Path(home_root) if home_root is not None else Path.home()
    try:
        real = Path(path).expanduser().resolve()
    except OSError:
        return False
    for rel in HARNESS_SKILL_PARENTS:
        parent = (home / rel).resolve()
        try:
            if real.parent == parent:
                return True
        except OSError:
            continue
        # 用户给的是符号链接本身、尚未 resolve 到自有仓时也算。
        try:
            given = Path(path).expanduser()
            if given.parent.resolve() == parent:
                return True
        except OSError:
            continue
    return False


def source_has_dirty_or_untracked(path: Path) -> bool:
    if not (Path(path) / ".git").is_dir():
        return False
    code, out, _ = run_git(["status", "--porcelain"], cwd=str(path))
    return code == 0 and bool(out.strip())


def stash_import_dir(path: Path, name: str, *, home_root: Path | None = None) -> Path:
    """整份拷到 ~/.xskill/import-stash/<name>/<时间>/。拷贝不移动。"""
    from xskill.config import XSKILL_HOME

    root = Path(home_root) if home_root is not None else XSKILL_HOME
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = root / "import-stash" / name / stamp
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(path, dest, symlinks=True, dirs_exist_ok=False)
    logger.info(
        "import stash: copied %s -> %s (uncommitted or untracked kept)",
        path, dest,
    )
    return dest


def maybe_stash_overwrite_dir(
    path: Path,
    name: str,
    *,
    home_root: Path | None = None,
) -> Path | None:
    """即将被盖掉且有脏文件时整份拷走。返回 stash 路径。"""
    path = Path(path)
    if not path.exists():
        return None
    if not source_has_dirty_or_untracked(path):
        return None
    return stash_import_dir(path, name, home_root=home_root)


def _ignore_runtime(src: str, names: list[str]) -> set[str]:
    del src
    skipped = set()
    for name in names:
        if name in RUNTIME_SIDECARS or name in RUNTIME_SIDECAR_DIRS:
            skipped.add(name)
    return skipped


def _ignore_new_skill_keep_git(src: str, names: list[str]) -> set[str]:
    """新技能带源仓历史时保留 .git，其余运行时辅助文件仍丢掉。"""
    del src
    skipped = set()
    for name in names:
        if name in RUNTIME_SIDECARS:
            skipped.add(name)
        elif name in RUNTIME_SIDECAR_DIRS and name != ".git":
            skipped.add(name)
    return skipped


def _replace_skill_files(source: Path, target: Path) -> None:
    """把目标工作区技能文件换成源目录那份。不动 .git 和辅助文件。"""
    for child in list(target.iterdir()):
        if child.name in RUNTIME_SIDECARS or child.name in RUNTIME_SIDECAR_DIRS:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        if child.name in RUNTIME_SIDECARS or child.name in RUNTIME_SIDECAR_DIRS:
            continue
        dest = target / child.name
        if child.is_dir():
            shutil.copytree(child, dest, symlinks=True)
        elif child.is_file():
            shutil.copy2(child, dest)


def _target_has_git(target: Path) -> bool:
    return (target / ".git").is_dir()


def mark_skill_imported(target: Path) -> None:
    """写入纳入标记。须在 ``_replace_skill_files`` 之后，随 import 那次提交进仓。"""
    (Path(target) / ORIGIN_FILENAME).write_text(
        f"{ORIGIN_IMPORT}\n", encoding="utf-8",
    )


def is_imported_skill(path: Path) -> bool:
    """目录是否带 ``xskill import`` 纳入标记（``.xskill-origin`` 首行为 import）。"""
    marker = Path(path) / ORIGIN_FILENAME
    if not marker.is_file():
        return False
    try:
        line = marker.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return False
    return bool(line) and line[0].strip() == ORIGIN_IMPORT


def skill_kept_on_rebuild(path: Path) -> bool:
    """用户 ``xskill import`` 纳入的技能，全量 rebuild --force 不得删。"""
    path = Path(path)
    if is_imported_skill(path):
        return True
    if not (path / ".git").is_dir():
        return False
    from xskill.skill.git import commit_history_has_subject_prefix
    return commit_history_has_subject_prefix(path, _KEEP_COMMIT_PREFIXES)


def _notify_imported_catalog(
    target: Path,
    catalog_db_path: Path | str | None,
) -> None:
    """import 不跑 Agent 工具上下文，git 写出口的 upsert 会被跳过。

    看板技能库读的是 ``skills_catalog`` 投影表，冷启动灌过表之后不再扫盘。
    这里显式把纳入结果写进 registry，CLI 报成功后面板才能看见。
    """
    from xskill.config import get_registry_db_path
    from xskill.skill.catalog_store import notify_native_upsert

    resolved = (
        Path(catalog_db_path) if catalog_db_path is not None
        else get_registry_db_path()
    )
    notify_native_upsert(target, db_path=resolved)


def import_one_skill(
    skill_dir: Path,
    source: Path,
    *,
    commit_message: str | None = None,
    catalog_db_path: Path | str | None = None,
) -> ImportResult:
    """把一个源技能目录落入 ``skill_dir / source.name``。"""
    source = Path(source).resolve()
    if not is_skill_source_dir(source):
        raise FileNotFoundError(f"not a skill directory (missing SKILL.md): {source}")
    name = source.name
    skill_root = Path(skill_dir)
    skill_root.mkdir(parents=True, exist_ok=True)
    target = skill_root / name
    message = commit_message or f"import: {name} from {source}"

    if target.exists() and source == target.resolve():
        tmp = Path(tempfile.mkdtemp(prefix="xskill-import-src."))
        copied = tmp / name
        try:
            shutil.copytree(source, copied, symlinks=True)
            return import_one_skill(
                skill_dir, copied, commit_message=message,
                catalog_db_path=catalog_db_path,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    existed = target.is_dir() and _target_has_git(target)
    result = ImportResult(name=name, existed=existed)

    if not existed:
        if target.exists():
            shutil.rmtree(target)
        source_git = (source / ".git").is_dir()
        ignore = _ignore_new_skill_keep_git if source_git else _ignore_runtime
        shutil.copytree(source, target, ignore=ignore, symlinks=True)
        if source_git:
            ensure_head_on_main(target)
            gi = target / ".gitignore"
            if not gi.is_file():
                gi.write_text(SKILL_GITIGNORE, encoding="utf-8")
            _replace_skill_files(source, target)
            mark_skill_imported(target)
            commit_update_main_branch(str(target), message)
        else:
            mark_skill_imported(target)
            init_imported_repo_on_main(target, message)
        code, sha, _ = run_git(["rev-parse", "HEAD"], cwd=str(target))
        result.sha = sha.strip() if code == 0 else ""
        _notify_imported_catalog(target, catalog_db_path)
        logger.info("imported new skill %s sha=%s", name, result.sha[:8])
        return result

    with skill_repo_lock(target):
        branch = current_branch(str(target)) or ""
        if branch == "baby":
            result.baby_overwritten = True
            logger.info("import overwriting baby draft: %s", name)

        from xskill.canary import clear_current_main_round_scores, has_staging, main_sha
        result.staging_kept = has_staging(target)
        old_main = main_sha(target) or ""
        if result.staging_kept and old_main:
            result.main_round_scores_cleared = clear_current_main_round_scores(
                target, commit_sha=old_main,
            )

        ensure_head_on_main(target)
        _replace_skill_files(source, target)
        mark_skill_imported(target)
        committed = commit_update_main_branch(str(target), message)
        if not committed:
            logger.info("import %s: worktree already matched, nothing to commit", name)

    code, sha, _ = run_git(["rev-parse", "HEAD"], cwd=str(target))
    result.sha = sha.strip() if code == 0 else ""
    _notify_imported_catalog(target, catalog_db_path)
    logger.info("imported existing skill %s sha=%s staging=%s",
                name, result.sha[:8], result.staging_kept)
    return result


def pack_import_zip(source: Path, *, include_git: bool = True) -> bytes:
    """打源目录 zip。默认带 .git；跳过运行时辅助文件。"""
    import io
    import zipfile

    source = Path(source).resolve()
    buf = io.BytesIO()
    skip_top = set(RUNTIME_SIDECAR_DIRS)
    if include_git:
        skip_top.discard(".git")
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(source.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(source)
            parts = rel.parts
            if any(part == "__pycache__" for part in parts):
                continue
            if rel.name in RUNTIME_SIDECARS:
                continue
            if parts and parts[0] in skip_top:
                continue
            zf.write(file_path, rel.as_posix())
    return buf.getvalue()


def extract_import_zip(payload: bytes, dest: Path, *, max_files: int = 20000) -> Path:
    """解压到 dest，拒绝路径穿越。"""
    import io
    import zipfile

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        if len(files) > max_files:
            raise ValueError(f"import archive contains more than {max_files} files")
        for info in archive.infolist():
            target = (dest / info.filename).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"unsafe archive path: {info.filename}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    return dest


def import_skill_path(
    skill_dir: Path,
    source_path: Path,
    *,
    install: bool = True,
    home_root: Path | None = None,
    stash_home: Path | None = None,
) -> list[ImportResult]:
    """发现路径下的技能并逐个纳入。独立部署走这条；团队部署只在 server 上落仓。"""
    results = []
    home = Path(home_root) if home_root is not None else Path.home()
    for source in discover_import_sources(source_path):
        name = source.name
        target = Path(skill_dir) / name
        result_warnings: list[str] = []
        stash_path = ""
        if is_harness_skill_path(source, home_root=home):
            result_warnings.append(HARNESS_IMPORT_WARNING)
        overwrite_paths = []
        if target.exists():
            overwrite_paths.append(target.resolve())
        try:
            real_source = source.resolve()
        except OSError:
            real_source = source
        if real_source not in overwrite_paths and source.exists():
            overwrite_paths.append(source)
        stash_dirs: list[str] = []
        for path in overwrite_paths:
            stashed = maybe_stash_overwrite_dir(
                path, name, home_root=stash_home,
            )
            if stashed is not None:
                stash_dirs.append(str(stashed))
                logger.info(
                    "import %s: stashed %s -> %s before overwrite",
                    name, path, stashed,
                )
        stash_path = stash_dirs[0] if stash_dirs else ""
        imported = import_one_skill(skill_dir, source)
        imported.warnings.extend(result_warnings)
        imported.stash_path = stash_path
        if install:
            from xskill.team.client.daemon import install_skill_to_ecosystems
            install_skill_to_ecosystems(
                Path(skill_dir) / imported.name, home_root=home,
            )
        results.append(imported)
    return results
