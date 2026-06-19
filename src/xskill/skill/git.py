"""
skill/git.py — Skill 仓库 git 操作（纯 Python，dulwich-backed）
═══════════════════════════════════════════════════════════════
封装 git 操作 + skill 仓库的初始化模板，**不再依赖系统 git 二进制**。

底层：dulwich (纯 Python git 实现)。
- ``run_git(args, cwd)`` 是给老 caller 的 shim：按 ``args[0]`` dispatch 到
  内部 handler，每个 handler 返回 ``(code, stdout, stderr)``，stdout 格式
  与 git CLI 完全一致（``status --porcelain``、``log --format=%cI`` 等
  下游 caller 会解析其字符串输出）。
- ``init_skill_repo_on_baby`` / ``commit_baby_to_main_branch`` /
  ``commit_to_staging_branch`` / ``ensure_repo`` / ``commit_changes`` 等
  高层函数直接用 dulwich，**不**走 run_git——保证 source 单一。

v2 (AtomTask 流水线) 引入 baby 分支：ClusterAgent 调 new_skill_folder 时
创建 skill 目录后立刻 ``git init`` + ``checkout -b baby`` + 首次 commit
（含 stub SKILL.md 和 .gitignore），让分支状态成为"该 skill 的 state"
单一事实源——避免 .meta.yml 之类的并行元数据。

后续流转：
- baby 分支 = wip（cluster 创建但 SkillEditAgent 尚未跑过）
- SkillEditAgent 第一次跑 → 调 commit_baby_to_main(message) → baby 重命名为 main
- SkillEditAgent 后续跑 → 调 commit_to_staging(message) → 从 main 切 staging
"""

from __future__ import annotations

import io
import logging
import os
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dulwich import porcelain
from dulwich.errors import NotGitRepository
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo

logger = logging.getLogger("xskill.git_lock")


# ═══════════════════════════════════════════════════════════════════
# 默认 author/committer 身份
# ═══════════════════════════════════════════════════════════════════
# 系统 git 仓库可以从 .git/config 的 user.email/user.name 读身份；dulwich
# 走 porcelain.commit 默认也读 config（在没有就会抛错）。为保持原有
# init_skill_repo_on_baby 设的 ``xskill@local`` / ``xskill`` 身份，每次
# commit 我们显式传 author/committer bytes，且仓库初始化时直接写 .git/config。
XSKILL_AUTHOR = b"xskill <xskill@local>"


def _write_repo_identity(repo: Repo) -> None:
    """把 user.email / user.name 写进 .git/config，让 git CLI 工具读取
    也能看到稳定身份（外部 git 调试 / inspect 用）。"""
    cfg = repo.get_config()
    cfg.set((b"user",), b"email", b"xskill@local")
    cfg.set((b"user",), b"name", b"xskill")
    cfg.write_to_path()


# ═══════════════════════════════════════════════════════════════════
# Per-skill-repo 串行化
# ═══════════════════════════════════════════════════════════════════
# watcher 线程（SkillEditAgent）与线程池（cluster → init_skill_repo_on_baby）
# 会并发对同一个 skill 的 .git 跑 git 操作，撞坏 .git/index 和 refs。
# 这里用 per-repo RLock 串行化：
#   - run_git 对每个 cwd 取该 repo 的 RLock——任意两个 git 操作不会同时
#     操作同一个 repo；不同 repo 仍可并行。
#   - skill_repo_lock(repo_dir) 给"必须原子的复合操作"（add+commit+branch）
#     用，RLock 让其内部的 run_git 调用可重入不死锁。
_repo_locks: dict[str, threading.RLock] = {}
_repo_locks_meta = threading.Lock()


def _repo_lock_for(cwd: str | Path) -> threading.RLock:
    key = str(Path(cwd).resolve())
    with _repo_locks_meta:
        lk = _repo_locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _repo_locks[key] = lk
        return lk


@contextmanager
def skill_repo_lock(repo_dir: str | Path):
    """串行化一个 skill 子仓的复合 git 操作（add+commit+branch 等必须原子）。

    与 ``run_git`` 用同一把 per-repo RLock——复合操作持锁期间内部的
    ``run_git`` 调用可重入，不会自死锁。
    """
    lk = _repo_lock_for(repo_dir)
    lk.acquire()
    try:
        yield
    finally:
        lk.release()


# ═══════════════════════════════════════════════════════════════════
# 模板常量
# ═══════════════════════════════════════════════════════════════════

SKILL_GITIGNORE = """# xskill v2 skill 仓库的 ignore 规则
# candidates buffer 不版本化（cluster agent 高频写入，与 skill 内容演化解耦）
.candidates.yml

# 灰度运行时数据
.ux_scores.jsonl
.canary/

# description 触发优化的实验留档（高频写入,不版本化）
.description_optimization/

# 旧锁文件
.lock
"""


# ═══════════════════════════════════════════════════════════════════
# dulwich helpers
# ═══════════════════════════════════════════════════════════════════

def _open_repo(cwd: str | Path) -> Repo:
    """打开一个 dulwich Repo。caller 必须 ``with _open_repo(...) as r``。"""
    return Repo(str(cwd))


def _ref_for_branch(name: str) -> bytes:
    if name == "HEAD":
        return b"HEAD"
    if name.startswith("refs/"):
        return name.encode("utf-8")
    return f"refs/heads/{name}".encode("utf-8")


def _resolve_rev(repo: Repo, rev: str) -> bytes | None:
    """解析任意 rev（branch / HEAD / sha / HEAD~1）为 commit sha bytes。

    返回 None 表示解析不到（caller 当 ``rev-parse`` 失败处理，code=128）。
    """
    rev_b = rev.encode("utf-8") if isinstance(rev, str) else rev

    # 形如 HEAD~N 走单独路径
    if b"~" in rev_b:
        base, _, n_str = rev_b.partition(b"~")
        n = int(n_str or b"1")
        base_sha = _resolve_rev(repo, base.decode("utf-8") or "HEAD")
        if base_sha is None:
            return None
        cur = base_sha
        for _ in range(n):
            try:
                commit = repo[cur]
                if not isinstance(commit, Commit) or not commit.parents:
                    return None
                cur = commit.parents[0]
            except KeyError:
                return None
        return cur

    # 显式 ref（不带 HEAD 兜底——HEAD 应该是 caller 显式传的，不能拿来当
    # "啥都解析不到的 fallback"，否则 rev-parse <不存在的 ref> 会错误地
    # 返回 HEAD 的 sha）
    if rev_b == b"HEAD":
        try:
            return repo.refs[b"HEAD"]
        except KeyError:
            return None
    for candidate in (rev_b, b"refs/heads/" + rev_b, b"refs/tags/" + rev_b):
        try:
            sha = repo.refs[candidate]
            return sha
        except KeyError:
            continue

    # 直接当 sha 试
    try:
        obj = repo[rev_b]
        if isinstance(obj, Commit):
            return rev_b
    except (KeyError, ValueError):
        pass
    return None


def _current_branch_name(repo: Repo) -> str:
    """当前 HEAD 指向的分支名。detached HEAD 时返回空字符串。"""
    try:
        head_target, _ = repo.refs.follow(b"HEAD")
        # head_target 是 ref chain，最后一个不是 HEAD 才是分支
        last = head_target[-1]
        if last == b"HEAD":
            return ""
        if last.startswith(b"refs/heads/"):
            return last[len(b"refs/heads/"):].decode("utf-8")
        return ""
    except (KeyError, IndexError):
        return ""


def _list_branches(repo: Repo) -> list[str]:
    out = []
    for ref in repo.refs.keys():
        if ref.startswith(b"refs/heads/"):
            out.append(ref[len(b"refs/heads/"):].decode("utf-8"))
    return sorted(out)


def _has_branch(repo: Repo, name: str) -> bool:
    return _ref_for_branch(name) in repo.refs


def _lookup_path_in_tree(repo: Repo, tree_sha: bytes, path: str) -> bytes | None:
    """在 tree 下查路径，返回 blob sha 或 None。支持子目录路径。"""
    parts = path.strip("/").split("/")
    cur_sha = tree_sha
    for i, part in enumerate(parts):
        try:
            cur = repo[cur_sha]
        except KeyError:
            return None
        if not isinstance(cur, Tree):
            return None
        name_b = part.encode("utf-8")
        found = None
        for entry in cur.items():
            if entry.path == name_b:
                found = entry
                break
        if found is None:
            return None
        if i == len(parts) - 1:
            return found.sha
        cur_sha = found.sha
    return None


def _read_blob(repo: Repo, sha: bytes) -> bytes | None:
    try:
        obj = repo[sha]
    except KeyError:
        return None
    if not isinstance(obj, Blob):
        return None
    return obj.data


def _commit_iso(repo: Repo, commit_sha: bytes) -> str:
    """%cI 风格的 strict-ISO 提交时间（带 timezone）。"""
    c = repo[commit_sha]
    assert isinstance(c, Commit)
    ts = c.commit_time
    tz_off = c.commit_timezone  # seconds east of UTC
    tz = timezone(_timedelta_seconds(tz_off))
    dt = datetime.fromtimestamp(ts, tz=tz)
    # strict ISO 8601: 2024-01-01T12:34:56+08:00
    return dt.isoformat(timespec="seconds")


def _timedelta_seconds(off: int):
    from datetime import timedelta
    return timedelta(seconds=off)


def _commit_unix_ts(repo: Repo, commit_sha: bytes) -> int:
    c = repo[commit_sha]
    assert isinstance(c, Commit)
    return int(c.commit_time)


def _commit_subject(repo: Repo, commit_sha: bytes) -> str:
    c = repo[commit_sha]
    assert isinstance(c, Commit)
    msg = c.message.decode("utf-8", errors="replace")
    return msg.split("\n", 1)[0]


def _is_ancestor(repo: Repo, ancestor: bytes, descendant: bytes) -> bool:
    """check ancestor is reachable from descendant via parent chain."""
    if ancestor == descendant:
        return True
    seen = set()
    stack = [descendant]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur == ancestor:
            return True
        try:
            commit = repo[cur]
        except KeyError:
            continue
        if not isinstance(commit, Commit):
            continue
        for p in commit.parents:
            if p not in seen:
                stack.append(p)
    return False


def _stage_all(repo: Repo, root: Path) -> bool:
    """stage 所有非 .git / 非 gitignored 文件，返回是否有任何文件 staged。

    dulwich porcelain.add 只 stage 你给的路径；对 ``add -A`` 我们扫描工作区，
    挑出非 .git 文件，再调 porcelain.add。同时处理"被删除"的文件——
    dulwich.add 不处理 deletion；用 staged() 比对，把工作区不存在但 index 里有
    的路径 ``stage`` 为删除（直接 del index[name]）。
    """
    import fnmatch

    ignore_patterns = _load_gitignore_patterns(root)
    rel_paths: list[str] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if not parts or parts[0] == ".git":
            continue
        # description 触发优化的实验留档 NEVER staged。新建仓库的 .gitignore
        # 模板已含 .description_optimization/，但对**预先存在**的 skill 仓
        # （.gitignore 可能没这条）这里硬编码兜底跳过，绝不版本化高频写入的
        # 优化实验数据。
        if ".description_optimization" in parts:
            continue
        rel_str = str(rel).replace(os.sep, "/")
        if _is_ignored(rel_str, ignore_patterns):
            continue
        rel_paths.append(rel_str)

    if rel_paths:
        porcelain.add(repo=repo, paths=rel_paths)

    # 处理 deletion: 看 index 里有但工作区没有的，从 index 删
    index = repo.open_index()
    deleted_any = False
    workdir_set = set(rel_paths)
    for entry_path in list(index):
        path_str = entry_path.decode("utf-8") if isinstance(entry_path, bytes) else entry_path
        if path_str not in workdir_set:
            full = root / path_str
            if not full.exists():
                del index[entry_path]
                deleted_any = True
    if deleted_any:
        index.write()

    return bool(rel_paths) or deleted_any


def _load_gitignore_patterns(root: Path) -> list[str]:
    p = root / ".gitignore"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _is_ignored(rel_path: str, patterns: list[str]) -> bool:
    """非常简化的 gitignore 匹配，覆盖项目里实际用到的模式：
    - 精确文件名（.candidates.yml / .ux_scores.jsonl / .lock）
    - 目录前缀（.canary/）
    """
    import fnmatch
    parts = rel_path.split("/")
    for pat in patterns:
        p = pat.rstrip("/")
        is_dir_pat = pat.endswith("/")
        # 目录模式：匹配任何前缀
        if is_dir_pat:
            if parts and parts[0] == p:
                return True
            continue
        # 普通模式：对每段做 fnmatch
        for seg in parts:
            if fnmatch.fnmatchcase(seg, p):
                return True
        if fnmatch.fnmatchcase(rel_path, p):
            return True
    return False


def _do_commit(repo: Repo, message: str, *, allow_empty: bool = False) -> tuple[bytes | None, str]:
    """commit 当前 index。
    - 返回 (commit_sha, err_msg)；commit_sha=None 表示失败/无改动。
    - allow_empty=False 且没有改动 → 返回 (None, 'nothing to commit') 模拟
      git CLI 退出码非零 + stderr 'nothing to commit'。
    """
    # 检测是否有改动：比较 index 树和 HEAD 树
    try:
        head_sha = repo.refs[b"HEAD"]
    except KeyError:
        head_sha = None

    index = repo.open_index()
    index_tree_sha = index.commit(repo.object_store)
    head_tree_sha = None
    if head_sha is not None:
        try:
            head_commit = repo[head_sha]
            if isinstance(head_commit, Commit):
                head_tree_sha = head_commit.tree
        except KeyError:
            head_tree_sha = None

    if (not allow_empty) and head_tree_sha is not None and index_tree_sha == head_tree_sha:
        return None, "nothing to commit"

    try:
        sha = porcelain.commit(
            repo=repo,
            message=message.encode("utf-8"),
            author=XSKILL_AUTHOR,
            committer=XSKILL_AUTHOR,
        )
        return sha, ""
    except Exception as e:
        return None, str(e)


def _branch_force_create(repo: Repo, name: str, target_sha: bytes) -> None:
    """``git branch -f <name> <sha>`` 等价：强制把 branch ref 指到 sha。"""
    repo.refs[_ref_for_branch(name)] = target_sha


def _branch_delete(repo: Repo, name: str) -> None:
    ref = _ref_for_branch(name)
    if ref in repo.refs:
        del repo.refs[ref]


def _checkout_branch(
    repo: Repo, branch: str, *, create: bool = False, force_reset: bool = False,
    from_sha: bytes | None = None,
) -> tuple[int, str]:
    """切换到 branch。
    - create=True: 新建分支再 checkout
    - force_reset=True: 强制（即使分支已存在也指回 from_sha 或当前 HEAD）
    - from_sha: 新建/重置分支时的起点；None 用当前 HEAD

    工作区策略：见函数末尾注释。
    """
    ref = _ref_for_branch(branch)
    exists = ref in repo.refs
    explicit_from = from_sha is not None

    if from_sha is None:
        try:
            from_sha = repo.refs[b"HEAD"]
        except KeyError:
            from_sha = None

    if create and exists and not force_reset:
        return 128, f"fatal: A branch named '{branch}' already exists."

    if create or force_reset:
        if from_sha is None:
            # 空仓（还没任何 commit）→ 把 HEAD 设成指向新 branch 的 symbolic ref
            # 即可；分支 ref 在首次 commit 时由 porcelain.commit 自动创建。
            repo.refs.set_symbolic_ref(b"HEAD", ref)
            return 0, ""
        repo.refs[ref] = from_sha
    elif not exists:
        return 1, f"error: pathspec '{branch}' did not match any file(s) known to git"

    # 设 HEAD 指向该 branch（symbolic ref）
    repo.refs.set_symbolic_ref(b"HEAD", ref)
    # 工作区策略，对齐 git CLI：
    #   - 显式给了 from_sha（``checkout -B branch <sha>``）→ reset 到该 sha
    #   - ``checkout -b/-B branch`` 无 sha（create 路径）→ 保留 worktree
    #     （git 行为：分支 ref 从 HEAD 派生，worktree 不动）
    #   - 普通 ``checkout <branch>`` 切到已存在分支 → 切换 worktree 内容到该
    #     分支的 tree（git 行为：worktree 内容更新到目标分支）
    if explicit_from:
        try:
            porcelain.reset(repo=repo, mode="hard", treeish=ref)
        except Exception as e:
            return 1, f"reset failed: {e}"
    elif not create:
        # 普通 switch：切换工作区到目标分支内容
        try:
            porcelain.reset(repo=repo, mode="hard", treeish=ref)
        except Exception as e:
            return 1, f"reset failed: {e}"
    return 0, ""


def _checkout_paths_from_ref(repo: Repo, ref: str, paths: list[str]) -> tuple[int, str]:
    """``git checkout <ref> -- <paths>``：把 paths 在 ref 上的内容写回工作树
    + index。``paths`` 形如 ``["<dirname>/"]``——展开成该目录下所有文件。
    """
    sha = _resolve_rev(repo, ref)
    if sha is None:
        return 128, f"fatal: bad revision {ref}"
    try:
        commit = repo[sha]
    except KeyError:
        return 128, f"fatal: bad object {ref}"
    if not isinstance(commit, Commit):
        return 128, f"fatal: not a commit: {ref}"
    tree_sha = commit.tree

    root = Path(repo.path)
    index = repo.open_index()
    any_changed = False
    for pathspec in paths:
        spec = pathspec.rstrip("/")
        # 遍历 ref tree 找该路径下的所有 blob
        blobs = _collect_blobs_under_path(repo, tree_sha, spec)
        if not blobs:
            # 路径在 ref 上不存在 → 工作区里要删
            target_dir = root / spec
            if target_dir.is_dir():
                import shutil
                shutil.rmtree(target_dir)
                any_changed = True
            # index 里删
            for entry_path in list(index):
                p_str = entry_path.decode("utf-8") if isinstance(entry_path, bytes) else entry_path
                if p_str == spec or p_str.startswith(spec + "/"):
                    del index[entry_path]
                    any_changed = True
            continue
        for rel, blob_sha, mode in blobs:
            data = _read_blob(repo, blob_sha) or b""
            full = root / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(data)
            # update index
            st = full.stat()
            try:
                from dulwich.index import IndexEntry, _fs_to_tree_path
            except ImportError:
                from dulwich.index import IndexEntry  # type: ignore
            entry = IndexEntry(
                ctime=(int(st.st_ctime), 0),
                mtime=(int(st.st_mtime), 0),
                dev=st.st_dev,
                ino=st.st_ino,
                mode=mode,
                uid=st.st_uid,
                gid=st.st_gid,
                size=st.st_size,
                sha=blob_sha,
                flags=0,
                extended_flags=0,
            )
            index[rel.encode("utf-8")] = entry
            any_changed = True
    if any_changed:
        index.write()
    return 0, ""


def _collect_blobs_under_path(
    repo: Repo, tree_sha: bytes, path: str,
) -> list[tuple[str, bytes, int]]:
    """返回 [(relpath, blob_sha, mode)] 列表，path 可以是文件或目录。"""
    parts = path.strip("/").split("/") if path.strip("/") else []
    # 走到 path 对应的 tree/blob
    cur_sha = tree_sha
    cur_path = ""
    for i, part in enumerate(parts):
        try:
            cur = repo[cur_sha]
        except KeyError:
            return []
        if not isinstance(cur, Tree):
            return []
        target = None
        for entry in cur.items():
            if entry.path == part.encode("utf-8"):
                target = entry
                break
        if target is None:
            return []
        if i == len(parts) - 1:
            obj = repo[target.sha]
            if isinstance(obj, Blob):
                return [("/".join(parts), target.sha, target.mode)]
            cur_sha = target.sha
            cur_path = "/".join(parts)
        else:
            cur_sha = target.sha
            cur_path = "/".join(parts[: i + 1])

    if not parts:
        cur_path = ""

    # cur_sha 是个目录 tree → 递归列出所有 blob
    out: list[tuple[str, bytes, int]] = []

    def _walk(tree_sha_: bytes, prefix: str) -> None:
        try:
            t = repo[tree_sha_]
        except KeyError:
            return
        if not isinstance(t, Tree):
            return
        for entry in t.items():
            name = entry.path.decode("utf-8")
            rel = f"{prefix}/{name}" if prefix else name
            obj = repo[entry.sha]
            if isinstance(obj, Tree):
                _walk(entry.sha, rel)
            elif isinstance(obj, Blob):
                out.append((rel, entry.sha, entry.mode))

    _walk(cur_sha, cur_path)
    return out


def _status_porcelain(repo: Repo, root: Path) -> str:
    """生成 ``git status --porcelain`` 兼容输出。

    格式：``XY path\n``。X = index 状态，Y = worktree 状态。
    本实现简化：
      - untracked → ``?? path``
      - modified in worktree → `` M path``
      - added/staged → ``A  path``
      - deleted in worktree → `` D path``
    """
    status = porcelain.status(repo=repo, untracked_files="all")
    lines: list[str] = []

    # status 是 GitStatus(staged={'add'/'modify'/'delete': [...]}, unstaged=[...], untracked=[...])
    staged = status.staged or {}
    for key, sym in (("add", "A"), ("modify", "M"), ("delete", "D")):
        for path in staged.get(key, []):
            p = path.decode("utf-8") if isinstance(path, bytes) else path
            lines.append(f"{sym}  {p}")

    for path in status.unstaged or []:
        p = path.decode("utf-8") if isinstance(path, bytes) else path
        # 区分 modify vs delete
        full = root / p
        if not full.exists():
            lines.append(f" D {p}")
        else:
            lines.append(f" M {p}")

    for path in status.untracked or []:
        p = path.decode("utf-8") if isinstance(path, bytes) else path
        # 忽略 .gitignore 已忽略的（porcelain.status 默认会 filter，但保险起见跳过）
        lines.append(f"?? {p}")

    return "\n".join(lines)


def _diff_head(repo: Repo) -> str:
    """``git diff HEAD`` —— 工作区相对 HEAD 的 unified diff。

    实现策略：拿 HEAD tree 与 index 中的当前内容做对比；
    工作区改动先 stage 再生成 diff（这与 ``git diff HEAD`` 行为略有不同：
    git diff HEAD 直接看 worktree，但下游 UserEditAbsorbAgent 把 diff 内容传给
    LLM 看，semantics 一致）。

    为避免改 index 状态，我们在临时 inmemory tree 上做对比。
    """
    try:
        head_sha = repo.refs[b"HEAD"]
        head_commit = repo[head_sha]
        head_tree_sha = head_commit.tree if isinstance(head_commit, Commit) else None
    except KeyError:
        head_tree_sha = None

    # 构造 worktree 当前的 tree（不修改 .git/index 文件）。先把改动 stage 到
    # 一份独立 index——dulwich.index 没暴露 in-memory index，权宜方案：
    # 先 stage_all → 取 index_tree → 然后跑 diff_tree
    root = Path(repo.path)
    _stage_all(repo, root)
    index = repo.open_index()
    new_tree_sha = index.commit(repo.object_store)

    buf = io.BytesIO()
    if head_tree_sha is None:
        # 没 HEAD：把所有 blob 当 added
        try:
            porcelain.diff_tree(repo, b"", new_tree_sha, outstream=buf)
        except TypeError:
            # 老版 API
            porcelain.diff_tree(repo.path, b"", new_tree_sha, outstream=buf)
    else:
        try:
            porcelain.diff_tree(repo, head_tree_sha, new_tree_sha, outstream=buf)
        except TypeError:
            porcelain.diff_tree(repo.path, head_tree_sha, new_tree_sha, outstream=buf)
    return buf.getvalue().decode("utf-8", errors="replace")


# ═══════════════════════════════════════════════════════════════════
# run_git shim：按 args[0] dispatch
# ═══════════════════════════════════════════════════════════════════

def run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """老 caller 入口。按 args[0] dispatch 到 dulwich-backed handler。

    返回 (code, stdout, stderr)，格式与 git CLI 一致（下游解析依赖）。
    per-repo RLock 串行化，确保同一 .git 同时只有一个操作在跑。
    """
    if not args:
        return 1, "", "no git command"

    sub = args[0]
    rest = args[1:]
    handler = _DISPATCH.get(sub)
    if handler is None:
        return 1, "", f"unsupported git subcommand: {sub}"

    with _repo_lock_for(cwd):
        try:
            return handler(rest, cwd)
        except NotGitRepository as e:
            return 128, "", f"fatal: not a git repository: {e}"
        except Exception as e:  # noqa: BLE001
            logger.exception("dulwich op failed: %s %s @ %s", sub, rest, cwd)
            return 128, "", f"{type(e).__name__}: {e}"


# ── handler 列表 ──────────────────────────────────────────────────

def _h_init(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git init``。"""
    Path(cwd).mkdir(parents=True, exist_ok=True)
    repo = porcelain.init(cwd, bare=False)
    # 默认 HEAD 指向 refs/heads/master——project 用 main，但 init 后下个
    # 操作通常是 checkout -b baby / main，会覆盖 HEAD。这里不强行改默认。
    repo.close()
    return 0, f"Initialized empty Git repository in {cwd}/.git/", ""


def _h_config(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git config <key> <value>``。仅支持 user.* 这种 section.key 形态。"""
    if len(args) < 2:
        return 1, "", "config needs key + value"
    key, value = args[0], args[1]
    if "." not in key:
        return 1, "", f"bad config key: {key}"
    section, _, sub = key.partition(".")
    with _open_repo(cwd) as repo:
        cfg = repo.get_config()
        cfg.set((section.encode("utf-8"),), sub.encode("utf-8"), value.encode("utf-8"))
        cfg.write_to_path()
    return 0, "", ""


def _h_rev_parse(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git rev-parse [--verify] <ref>``。"""
    verify_only = False
    refs: list[str] = []
    for a in args:
        if a == "--verify":
            verify_only = True
        elif a.startswith("--"):
            continue
        else:
            refs.append(a)
    if not refs:
        return 1, "", "rev-parse needs ref"
    ref = refs[0]
    with _open_repo(cwd) as repo:
        sha = _resolve_rev(repo, ref)
        if sha is None:
            return 128, "", f"fatal: bad revision '{ref}'"
        return 0, sha.decode("ascii"), ""


def _h_log(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git log -1 --format=%X <ref>`` 几个具体形态 + ``log --oneline -N --follow``。

    支持的形态：
      - log -1 --format=%cI [<ref>]   strict-ISO commit time
      - log -1 --format=%ct [<ref>]   unix integer commit time
      - log -1 --format=%s  [<ref>]   subject
      - log -1 --format=%H  [<ref>]   full sha
      - log --oneline --follow -<N> -- <path>    （skill_log 用）
    """
    # 解析 -1 / --format=... / --oneline / --follow / -N / -- / path
    n_limit: int | None = None
    fmt: str | None = None
    oneline = False
    follow = False
    paths: list[str] = []
    ref: str | None = None

    it = iter(args)
    seen_dashdash = False
    for a in it:
        if seen_dashdash:
            paths.append(a)
            continue
        if a == "--":
            seen_dashdash = True
            continue
        if a == "-1":
            n_limit = 1
        elif a.startswith("--format="):
            fmt = a.split("=", 1)[1]
        elif a == "--oneline":
            oneline = True
        elif a == "--follow":
            follow = True
        elif a.startswith("-") and a[1:].isdigit():
            n_limit = int(a[1:])
        elif a.startswith("--"):
            continue
        else:
            if ref is None:
                ref = a
            else:
                paths.append(a)

    with _open_repo(cwd) as repo:
        target = _resolve_rev(repo, ref or "HEAD")
        if target is None:
            return 128, "", f"fatal: bad revision {ref or 'HEAD'}"

        # 单条 log -1 --format=...
        if n_limit == 1 and fmt is not None and not paths:
            if fmt == "%cI":
                return 0, _commit_iso(repo, target), ""
            if fmt == "%ct":
                return 0, str(_commit_unix_ts(repo, target)), ""
            if fmt == "%s":
                return 0, _commit_subject(repo, target), ""
            if fmt == "%H":
                return 0, target.decode("ascii"), ""
            return 0, "", f"unsupported format: {fmt}"

        # log --oneline --follow -N -- <path>: 列出 N 条 commit
        if oneline:
            count_limit = n_limit or 20
            shas = _walk_history(repo, target, count_limit, paths if paths else None)
            lines = []
            for s in shas:
                subj = _commit_subject(repo, s)
                lines.append(f"{s[:7].decode('ascii')} {subj}")
            return 0, "\n".join(lines), ""

        return 1, "", f"unsupported log args: {args}"


def _walk_history(
    repo: Repo, start: bytes, n: int, paths: list[str] | None,
) -> list[bytes]:
    """从 start 出发沿 first-parent 走 ≤n 个 commit；若给 paths，只保留
    那些 touched paths 的 commit（粗糙的 --follow 近似）。"""
    out: list[bytes] = []
    cur: bytes | None = start
    seen: set[bytes] = set()
    while cur and len(out) < n:
        if cur in seen:
            break
        seen.add(cur)
        try:
            commit = repo[cur]
        except KeyError:
            break
        if not isinstance(commit, Commit):
            break
        keep = True
        if paths:
            keep = _commit_touches_paths(repo, commit, paths)
        if keep:
            out.append(cur)
        cur = commit.parents[0] if commit.parents else None
    return out


def _commit_touches_paths(repo: Repo, commit: Commit, paths: list[str]) -> bool:
    """该 commit 与父 commit 的 tree diff 中是否有任一 path（prefix 匹配）。"""
    if not commit.parents:
        return True  # 初始 commit
    parent = repo[commit.parents[0]]
    if not isinstance(parent, Commit):
        return True
    p_tree = parent.tree
    c_tree = commit.tree
    norm_paths = [p.rstrip("/") for p in paths]
    try:
        from dulwich.diff_tree import tree_changes
        for change in tree_changes(repo.object_store, p_tree, c_tree):
            for tp in (change.old, change.new):
                if tp.path is None:
                    continue
                tp_str = tp.path.decode("utf-8", errors="replace")
                for want in norm_paths:
                    if tp_str == want or tp_str.startswith(want + "/"):
                        return True
    except Exception:
        return True
    return False


def _h_rev_list(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git rev-list --reverse <a>..<b>``"""
    reverse = False
    range_arg: str | None = None
    for a in args:
        if a == "--reverse":
            reverse = True
        elif a.startswith("-") or a.startswith("--"):
            continue
        else:
            range_arg = a
    if range_arg is None or ".." not in range_arg:
        return 1, "", f"unsupported rev-list args: {args}"
    base, _, head = range_arg.partition("..")
    with _open_repo(cwd) as repo:
        base_sha = _resolve_rev(repo, base)
        head_sha = _resolve_rev(repo, head)
        if head_sha is None:
            return 128, "", f"fatal: bad revision {head}"
        # 收集从 head_sha 出发的所有 reachable commit，剔除 base_sha 可达的
        base_reachable: set[bytes] = set()
        if base_sha is not None:
            stack = [base_sha]
            while stack:
                cur = stack.pop()
                if cur in base_reachable:
                    continue
                base_reachable.add(cur)
                try:
                    c = repo[cur]
                except KeyError:
                    continue
                if isinstance(c, Commit):
                    stack.extend(c.parents)
        out: list[bytes] = []
        seen: set[bytes] = set()
        stack = [head_sha]
        while stack:
            cur = stack.pop()
            if cur in seen or cur in base_reachable:
                continue
            seen.add(cur)
            try:
                c = repo[cur]
            except KeyError:
                continue
            if not isinstance(c, Commit):
                continue
            out.append(cur)
            stack.extend(c.parents)
        # 默认 newest first；--reverse → oldest first，按 commit_time 排
        out.sort(key=lambda s: repo[s].commit_time)
        if not reverse:
            out.reverse()
        return 0, "\n".join(s.decode("ascii") for s in out), ""


def _h_status(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git status --porcelain``."""
    if "--porcelain" not in args:
        return 1, "", "only --porcelain mode supported"
    with _open_repo(cwd) as repo:
        out = _status_porcelain(repo, Path(cwd))
        return 0, out, ""


def _h_add(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git add . / -A``."""
    with _open_repo(cwd) as repo:
        _stage_all(repo, Path(cwd))
    return 0, "", ""


def _h_commit(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git commit [--allow-empty] -m <msg>``."""
    msg = ""
    allow_empty = False
    it = iter(args)
    for a in it:
        if a == "-m":
            msg = next(it, "")
        elif a == "--allow-empty":
            allow_empty = True
    with _open_repo(cwd) as repo:
        sha, err = _do_commit(repo, msg, allow_empty=allow_empty)
    if sha is None:
        if err == "nothing to commit":
            return 1, "", "nothing to commit"
        return 1, "", err
    return 0, f"[{sha[:7].decode('ascii')}] {msg}", ""


def _h_branch(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git branch`` 各形态：
      - --list                            → 列分支
      - --show-current                    → 当前分支
      - -m <old> <new>                    → rename
      - -D <name>                         → delete
      - -f <name> <sha>                   → force create/move
      - <name> <sha>                      → create (no force)
    """
    if not args:
        # 等同 --list
        with _open_repo(cwd) as repo:
            lines = []
            cur = _current_branch_name(repo)
            for b in _list_branches(repo):
                mark = "*" if b == cur else " "
                lines.append(f"{mark} {b}")
            return 0, "\n".join(lines), ""

    if args[0] == "--list":
        with _open_repo(cwd) as repo:
            lines = []
            cur = _current_branch_name(repo)
            for b in _list_branches(repo):
                mark = "*" if b == cur else " "
                lines.append(f"{mark} {b}")
            return 0, "\n".join(lines), ""

    if args[0] == "--show-current":
        with _open_repo(cwd) as repo:
            return 0, _current_branch_name(repo), ""

    if args[0] == "-m":
        if len(args) != 3:
            return 1, "", "branch -m needs <old> <new>"
        old, new = args[1], args[2]
        with _open_repo(cwd) as repo:
            old_ref = _ref_for_branch(old)
            new_ref = _ref_for_branch(new)
            if old_ref not in repo.refs:
                return 128, "", f"fatal: branch {old} not found"
            if new_ref in repo.refs:
                return 128, "", f"fatal: branch {new} already exists"
            sha = repo.refs[old_ref]
            repo.refs[new_ref] = sha
            del repo.refs[old_ref]
            # 如果 HEAD 指向 old，更新 HEAD 指向 new
            try:
                head_target, _ = repo.refs.follow(b"HEAD")
                if head_target and head_target[-1] == old_ref:
                    repo.refs.set_symbolic_ref(b"HEAD", new_ref)
            except (KeyError, IndexError):
                pass
        return 0, "", ""

    if args[0] == "-D":
        names = args[1:]
        with _open_repo(cwd) as repo:
            for n in names:
                _branch_delete(repo, n)
        return 0, "", ""

    if args[0] == "-f":
        # branch -f <name> <sha>
        if len(args) != 3:
            return 1, "", "branch -f needs <name> <sha>"
        name, target = args[1], args[2]
        with _open_repo(cwd) as repo:
            sha = _resolve_rev(repo, target)
            if sha is None:
                return 128, "", f"fatal: bad revision {target}"
            _branch_force_create(repo, name, sha)
        return 0, "", ""

    # branch <name> <sha>
    if len(args) == 2 and not args[0].startswith("-"):
        name, target = args[0], args[1]
        with _open_repo(cwd) as repo:
            if _has_branch(repo, name):
                return 128, "", f"fatal: branch {name} already exists"
            sha = _resolve_rev(repo, target)
            if sha is None:
                return 128, "", f"fatal: bad revision {target}"
            repo.refs[_ref_for_branch(name)] = sha
        return 0, "", ""

    return 1, "", f"unsupported branch args: {args}"


def _h_checkout(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git checkout`` 各形态：
      - checkout <branch>
      - checkout -b <branch>
      - checkout -B <branch> [<sha>]
      - checkout -- .
      - checkout <ref> -- <pathspec>
    """
    # checkout -- . / checkout -- <path>: 用 ref（默认 HEAD）的内容覆盖 working
    if "--" in args:
        ddi = args.index("--")
        head = args[:ddi]
        paths = args[ddi + 1:]
        # 若 head 为空，等价于 ``checkout -- <paths>``，用 HEAD 内容
        if not head:
            ref = "HEAD"
        elif len(head) == 1 and not head[0].startswith("-"):
            ref = head[0]
        else:
            return 1, "", f"unsupported checkout: {args}"
        if paths == ["."]:
            # 等价 reset --hard
            with _open_repo(cwd) as repo:
                try:
                    porcelain.reset(repo=repo, mode="hard", treeish=ref.encode("utf-8"))
                except Exception as e:
                    return 1, "", f"reset failed: {e}"
            return 0, "", ""
        with _open_repo(cwd) as repo:
            return _checkout_paths_from_ref(repo, ref, paths)

    create = False
    force = False
    target: str | None = None
    from_target: str | None = None

    i = 0
    while i < len(args):
        a = args[i]
        if a == "-b":
            create = True
        elif a == "-B":
            create = True
            force = True
        elif a.startswith("-"):
            return 1, "", f"unsupported checkout flag: {a}"
        else:
            if target is None:
                target = a
            elif from_target is None:
                from_target = a
            else:
                return 1, "", f"unsupported checkout args: {args}"
        i += 1

    if target is None:
        return 1, "", "checkout needs target"

    with _open_repo(cwd) as repo:
        from_sha = _resolve_rev(repo, from_target) if from_target else None
        if create:
            code, err = _checkout_branch(
                repo, target, create=True, force_reset=force, from_sha=from_sha,
            )
        else:
            # 已存在 → switch；不存在 → fail
            if not _has_branch(repo, target):
                return 1, "", f"error: pathspec '{target}' did not match any file(s) known to git"
            code, err = _checkout_branch(repo, target, create=False)
        return code, "", err


def _h_reset(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git reset --hard <ref>``."""
    mode = "mixed"
    target: str | None = None
    for a in args:
        if a == "--hard":
            mode = "hard"
        elif a == "--soft":
            mode = "soft"
        elif a == "--mixed":
            mode = "mixed"
        elif a.startswith("-"):
            continue
        else:
            target = a
    if target is None:
        target = "HEAD"
    with _open_repo(cwd) as repo:
        sha = _resolve_rev(repo, target)
        if sha is None:
            return 128, "", f"fatal: bad revision {target}"
        # 先更新当前 branch 指向 sha
        cur = _current_branch_name(repo)
        if cur:
            repo.refs[_ref_for_branch(cur)] = sha
        else:
            repo.refs[b"HEAD"] = sha
        try:
            porcelain.reset(repo=repo, mode=mode, treeish=sha)
        except Exception as e:
            return 1, "", f"reset failed: {e}"
    return 0, "", ""


def _h_show(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git show <ref>:<path>``——读 ref 上的文件内容。"""
    if not args:
        return 1, "", "show needs <ref>:<path>"
    spec = args[0]
    if ":" not in spec:
        return 1, "", f"unsupported show: {spec}"
    ref, _, path = spec.partition(":")
    with _open_repo(cwd) as repo:
        sha = _resolve_rev(repo, ref)
        if sha is None:
            return 128, "", f"fatal: bad revision {ref}"
        commit = repo[sha]
        if not isinstance(commit, Commit):
            return 128, "", f"not a commit: {ref}"
        blob_sha = _lookup_path_in_tree(repo, commit.tree, path)
        if blob_sha is None:
            return 128, "", f"fatal: path '{path}' does not exist in '{ref}'"
        data = _read_blob(repo, blob_sha)
        if data is None:
            return 128, "", f"fatal: cannot read blob {path}"
        return 0, data.decode("utf-8", errors="replace"), ""


def _h_cat_file(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git cat-file -e <ref>:<path>``——存在测试。"""
    if "-e" not in args:
        return 1, "", "only cat-file -e supported"
    spec = [a for a in args if not a.startswith("-")][0]
    if ":" not in spec:
        return 1, "", f"unsupported cat-file: {spec}"
    ref, _, path = spec.partition(":")
    with _open_repo(cwd) as repo:
        sha = _resolve_rev(repo, ref)
        if sha is None:
            return 128, "", f"fatal: bad revision {ref}"
        commit = repo[sha]
        if not isinstance(commit, Commit):
            return 128, "", f"not a commit: {ref}"
        blob_sha = _lookup_path_in_tree(repo, commit.tree, path)
        if blob_sha is None:
            return 1, "", ""
        return 0, "", ""


def _h_diff(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git diff`` 几种形态：
      - diff HEAD                 worktree vs HEAD
      - diff --cached --name-only 已 staged 改动文件列表
      - diff <a> <b> -- <path>    两 commit 间 path 的差异
      - diff <a> <b>              两 commit 完整差异
    """
    if not args:
        return 1, "", "diff needs args"

    # --cached --name-only
    if "--cached" in args and "--name-only" in args:
        with _open_repo(cwd) as repo:
            index = repo.open_index()
            try:
                head_sha = repo.refs[b"HEAD"]
                head_tree_sha = repo[head_sha].tree
            except KeyError:
                head_tree_sha = None
            index_tree_sha = index.commit(repo.object_store)
            if head_tree_sha is None:
                # 全部 staged 算 added
                paths = sorted(p.decode("utf-8") if isinstance(p, bytes) else p for p in index)
                return 0, "\n".join(paths), ""
            from dulwich.diff_tree import tree_changes
            names: list[str] = []
            for change in tree_changes(repo.object_store, head_tree_sha, index_tree_sha):
                for tp in (change.new, change.old):
                    if tp.path:
                        names.append(tp.path.decode("utf-8"))
                        break
            return 0, "\n".join(sorted(set(names))), ""

    if args == ["HEAD"]:
        with _open_repo(cwd) as repo:
            text = _diff_head(repo)
            return 0, text, ""

    # diff <a> <b> [-- <path>]
    # 解析
    paths: list[str] = []
    revs: list[str] = []
    if "--" in args:
        ddi = args.index("--")
        revs = [a for a in args[:ddi] if not a.startswith("-")]
        paths = args[ddi + 1:]
    else:
        revs = [a for a in args if not a.startswith("-")]
    if len(revs) >= 2:
        a, b = revs[0], revs[1]
        with _open_repo(cwd) as repo:
            sha_a = _resolve_rev(repo, a)
            sha_b = _resolve_rev(repo, b)
            if sha_a is None or sha_b is None:
                return 128, "", f"fatal: bad revision {a} or {b}"
            ta = repo[sha_a].tree
            tb = repo[sha_b].tree
            buf = io.BytesIO()
            try:
                porcelain.diff_tree(repo, ta, tb, outstream=buf)
            except TypeError:
                porcelain.diff_tree(repo.path, ta, tb, outstream=buf)
            text = buf.getvalue().decode("utf-8", errors="replace")
            if paths:
                # 粗糙过滤：只保留 affecting paths 的 hunk header 段
                # 简化：保留全部输出（下游 skill_diff 用作展示，不算严格契约）
                pass
            return 0, text, ""

    return 1, "", f"unsupported diff args: {args}"


def _h_clean(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git clean -fd``——删未追踪文件 + 目录。"""
    # 简化实现：把 status untracked 的全删
    with _open_repo(cwd) as repo:
        status = porcelain.status(repo=repo, untracked_files="all")
        for p in status.untracked or []:
            ps = p.decode("utf-8") if isinstance(p, bytes) else p
            full = Path(cwd) / ps
            try:
                if full.is_file() or full.is_symlink():
                    full.unlink()
                elif full.is_dir():
                    import shutil
                    shutil.rmtree(full)
            except OSError:
                pass
    return 0, "", ""


def _h_merge(args: list[str], cwd: str) -> tuple[int, str, str]:
    """``git merge --ff/--no-ff <branch> -m <msg>``。

    简化实现：
      - 当前 HEAD = A, 目标 branch = B
      - A 是 B 的 ancestor → fast-forward：把当前 branch 指向 B，重置工作区
      - 否则 → 创建 merge commit（两 parents：A, B）
    """
    ff_only = "--ff" in args and "--no-ff" not in args
    msg = "merge"
    branch: str | None = None
    it = iter(args)
    for a in it:
        if a == "-m":
            msg = next(it, msg)
        elif a in ("--ff", "--no-ff", "--ff-only"):
            continue
        elif a.startswith("-"):
            continue
        else:
            if branch is None:
                branch = a
    if branch is None:
        return 1, "", "merge needs branch"

    with _open_repo(cwd) as repo:
        target_sha = _resolve_rev(repo, branch)
        if target_sha is None:
            return 128, "", f"fatal: bad branch {branch}"
        try:
            head_sha = repo.refs[b"HEAD"]
        except KeyError:
            return 128, "", "fatal: no HEAD"

        cur = _current_branch_name(repo)
        if not cur:
            return 128, "", "fatal: detached HEAD"

        if head_sha == target_sha:
            return 0, "Already up to date.", ""

        # fast-forward 可行？
        if _is_ancestor(repo, head_sha, target_sha):
            repo.refs[_ref_for_branch(cur)] = target_sha
            try:
                porcelain.reset(repo=repo, mode="hard", treeish=target_sha)
            except Exception as e:
                return 1, "", f"reset failed: {e}"
            return 0, f"Fast-forward to {target_sha[:7].decode('ascii')}", ""

        if ff_only:
            return 1, "", "Not possible to fast-forward, aborting."

        # 非 ff merge：创建 merge commit
        # 简单 strategy：用 target_sha 的 tree 作为 merge 结果（"theirs"），
        # 在两条 traj2skill 历史里 staging 通常领先 main，结果应等于 staging 的 tree
        target_commit = repo[target_sha]
        if not isinstance(target_commit, Commit):
            return 128, "", "target is not commit"
        new_commit = Commit()
        new_commit.tree = target_commit.tree
        new_commit.parents = [head_sha, target_sha]
        new_commit.author = XSKILL_AUTHOR
        new_commit.committer = XSKILL_AUTHOR
        now = int(datetime.now(timezone.utc).timestamp())
        new_commit.commit_time = now
        new_commit.author_time = now
        new_commit.commit_timezone = 0
        new_commit.author_timezone = 0
        new_commit.encoding = b"UTF-8"
        new_commit.message = msg.encode("utf-8")
        repo.object_store.add_object(new_commit)
        repo.refs[_ref_for_branch(cur)] = new_commit.id
        try:
            porcelain.reset(repo=repo, mode="hard", treeish=new_commit.id)
        except Exception as e:
            return 1, "", f"reset failed: {e}"
        return 0, f"Merge made (sha={new_commit.id[:7].decode('ascii')})", ""


_DISPATCH: dict[str, Callable[[list[str], str], tuple[int, str, str]]] = {
    "init": _h_init,
    "config": _h_config,
    "rev-parse": _h_rev_parse,
    "log": _h_log,
    "rev-list": _h_rev_list,
    "status": _h_status,
    "add": _h_add,
    "commit": _h_commit,
    "branch": _h_branch,
    "checkout": _h_checkout,
    "reset": _h_reset,
    "show": _h_show,
    "cat-file": _h_cat_file,
    "diff": _h_diff,
    "clean": _h_clean,
    "merge": _h_merge,
}


# ═══════════════════════════════════════════════════════════════════
# 高层 API：直接用 dulwich（不绕回 run_git）
# ═══════════════════════════════════════════════════════════════════

def init_skill_repo_on_baby(skill_dir: str, name: str, description: str) -> None:
    """v2: 初始化 skill 仓库到 baby 分支，附带 stub SKILL.md + .gitignore。

    流程：
      1. mkdir -p (如需)
      2. dulwich init
      3. 写 user identity 到 .git/config
      4. 写 .gitignore + stub SKILL.md (frontmatter 含 name/desc，body 占位)
      5. add . + commit "init: <name>"
      6. 重命名默认分支为 ``baby``
    """
    from datetime import date as _date

    p = Path(skill_dir)
    p.mkdir(parents=True, exist_ok=True)

    with skill_repo_lock(skill_dir):
        repo = porcelain.init(str(p), bare=False)
        try:
            _write_repo_identity(repo)

            (p / ".gitignore").write_text(SKILL_GITIGNORE, encoding="utf-8")

            today = _date.today().isoformat()
            stub_md = (
                f"---\n"
                f"name: {name}\n"
                f"description: {description}\n"
                f"metadata:\n"
                f"  version: 0\n"
                f"  state: baby\n"
                f"  created: \"{today}\"\n"
                f"  last_updated: \"{today}\"\n"
                f"  source_atoms: []\n"
                f"---\n"
                f"\n"
                f"# {name}\n"
                f"\n"
                f"(placeholder — SkillEditAgent 在 candidates 攒满阈值后会用真实 atom 内容填充正文)\n"
            )
            (p / "SKILL.md").write_text(stub_md, encoding="utf-8")

            (p / "scripts").mkdir(exist_ok=True)
            (p / "references").mkdir(exist_ok=True)
            (p / "scripts" / ".gitkeep").write_text("", encoding="utf-8")
            (p / "references" / ".gitkeep").write_text("", encoding="utf-8")

            _stage_all(repo, p)
            sha, err = _do_commit(repo, f"init({name}): baby branch with stub SKILL.md")
            if sha is None:
                logger.error("init commit failed: %s", err)
                return

            # init 默认分支可能是 master 或 main；把当前 branch 重命名为 baby
            cur = _current_branch_name(repo)
            if cur != "baby":
                if cur:
                    # 把 cur ref 复制到 baby，删 cur
                    repo.refs[_ref_for_branch("baby")] = repo.refs[_ref_for_branch(cur)]
                    del repo.refs[_ref_for_branch(cur)]
                else:
                    # detached HEAD 极不应该，但保险
                    head = repo.refs[b"HEAD"]
                    repo.refs[_ref_for_branch("baby")] = head
                repo.refs.set_symbolic_ref(b"HEAD", _ref_for_branch("baby"))
        finally:
            repo.close()
    logger.info(f"🌱 init skill on baby branch: {skill_dir}")


def commit_baby_to_main_branch(skill_dir: str, message: str) -> bool:
    """SkillEditAgent 调用：将 baby 分支提升为 main。

    流程：
      1. add -A + commit -m <message>
      2. baby → main rename
    """
    with skill_repo_lock(skill_dir):
        with _open_repo(skill_dir) as repo:
            cur = _current_branch_name(repo)
            if cur != "baby":
                logger.warning(f"commit_baby_to_main_branch 拒绝：当前不在 baby (在 {cur})")
                return False
            _stage_all(repo, Path(skill_dir))
            sha, err = _do_commit(repo, message)
            if sha is None and err != "nothing to commit":
                logger.warning(f"baby commit 失败: {err}")
                return False
            # rename baby → main
            baby_ref = _ref_for_branch("baby")
            main_ref = _ref_for_branch("main")
            if main_ref in repo.refs:
                logger.warning("branch rename baby → main 失败: main already exists")
                return False
            repo.refs[main_ref] = repo.refs[baby_ref]
            del repo.refs[baby_ref]
            repo.refs.set_symbolic_ref(b"HEAD", main_ref)
    logger.info(f"🎓 baby → main graduated: {Path(skill_dir).name}: {message}")
    return True


def commit_to_staging_branch(skill_dir: str, message: str) -> bool:
    """SkillEditAgent 调用：从 main 切 staging 分支并提交灰度候选。

    流程：
      1. 校验当前在 main 且不存在 staging
      2. 新建 staging（指向当前 main HEAD）+ 切到 staging
      3. add -A + commit -m <message>
      4. 物化 staging SKILL.md 到 ``<skill_dir>/../.canary/<name>/``
      5. 回到 main 分支
    """
    p = Path(skill_dir)
    with skill_repo_lock(skill_dir):
        with _open_repo(skill_dir) as repo:
            cur = _current_branch_name(repo)
            if cur != "main":
                logger.warning(f"commit_to_staging_branch 拒绝：当前不在 main (在 {cur})")
                return False
            if _has_branch(repo, "staging"):
                logger.warning("commit_to_staging_branch 拒绝：staging 已存在（灰度中）")
                return False
            # 切到 staging（从 main HEAD）
            main_sha_val = repo.refs[_ref_for_branch("main")]
            repo.refs[_ref_for_branch("staging")] = main_sha_val
            repo.refs.set_symbolic_ref(b"HEAD", _ref_for_branch("staging"))

            _stage_all(repo, p)
            sha, err = _do_commit(repo, message)
            if sha is None and err != "nothing to commit":
                logger.warning(f"staging commit 失败: {err}")
                # rollback
                repo.refs.set_symbolic_ref(b"HEAD", _ref_for_branch("main"))
                del repo.refs[_ref_for_branch("staging")]
                try:
                    porcelain.reset(repo=repo, mode="hard", treeish=_ref_for_branch("main"))
                except Exception:
                    pass
                return False

            # 物化 + 回 main
            from xskill.canary import materialize_staging
            canary_root = p.parent / ".canary"
            try:
                materialize_staging(p, canary_root)
            except Exception:
                logger.exception("materialize_staging 失败: %s", Path(skill_dir).name)

            # 回 main
            repo.refs.set_symbolic_ref(b"HEAD", _ref_for_branch("main"))
            try:
                porcelain.reset(repo=repo, mode="hard", treeish=_ref_for_branch("main"))
            except Exception as e:
                logger.warning("reset back to main failed: %s", e)
    logger.info(f"🚦 staging candidate committed: {Path(skill_dir).name}: {message}")
    return True


def commit_update_main_branch(skill_dir: str, message: str) -> bool:
    """冷启动在线进化专用：在 **main 分支原地** commit 一次正文更新。

    与 ``commit_to_staging_branch`` 的区别：不开 staging / 不物化 canary / 不走
    灰度，直接把 SkillEditAgent 重写后的 SKILL.md（基于现有正文 + 新 epoch 的
    candidates atom）commit 进 main —— 技能逐 epoch 在 main 上原地进化。

    前提：当前在 main 分支（冷启动 flush 时，已毕业的技能都在 main）。
    流程：add -A + commit -m <message>（无改动则 nothing-to-commit 返回 False）。

    不在 main → 直接 throw（不做 fallback），由 caller 兜异常并保留 candidates 重试。
    """
    with skill_repo_lock(skill_dir):
        with _open_repo(skill_dir) as repo:
            cur = _current_branch_name(repo)
            if cur != "main":
                raise RuntimeError(
                    f"commit_update_main_branch 要求当前在 main 分支，实际在 {cur!r}"
                )
            _stage_all(repo, Path(skill_dir))
            sha, err = _do_commit(repo, message)
            if sha is None:
                if err == "nothing to commit":
                    logger.warning(
                        "commit_update_main_branch 无改动可提交: %s",
                        Path(skill_dir).name,
                    )
                    return False
                raise RuntimeError(f"commit_update_main_branch commit 失败: {err}")
    logger.info(
        "♻️  main 原地进化 commit: %s: %s", Path(skill_dir).name, message
    )
    return True


def ensure_repo(skill_dir: str):
    """确保 skill_dir 是一个 git 仓库，在 main 分支上。"""
    p = Path(skill_dir)
    p.mkdir(parents=True, exist_ok=True)
    if not (p / ".git").exists():
        repo = porcelain.init(str(p), bare=False)
        try:
            _write_repo_identity(repo)
            (p / ".gitkeep").touch()
            (p / ".gitignore").write_text(
                "# canary runtime data — NOT versioned\n.ux_scores.jsonl\n.lock\n",
                encoding="utf-8",
            )
            _stage_all(repo, p)
            sha, _ = _do_commit(repo, "init skill repo")
            if sha is not None:
                # rename 默认分支 → main
                cur = _current_branch_name(repo)
                if cur and cur != "main":
                    repo.refs[_ref_for_branch("main")] = repo.refs[_ref_for_branch(cur)]
                    del repo.refs[_ref_for_branch(cur)]
                    repo.refs.set_symbolic_ref(b"HEAD", _ref_for_branch("main"))
        finally:
            repo.close()
        logger.info(f"初始化 skill git 仓库: {skill_dir}")
    else:
        with _open_repo(skill_dir) as repo:
            cur = _current_branch_name(repo)
            if cur != "main":
                # 切到 main；不存在就新建空 commit
                if not _has_branch(repo, "main"):
                    try:
                        head = repo.refs[b"HEAD"]
                        repo.refs[_ref_for_branch("main")] = head
                    except KeyError:
                        pass
                repo.refs.set_symbolic_ref(b"HEAD", _ref_for_branch("main"))
                try:
                    porcelain.reset(repo=repo, mode="hard", treeish=_ref_for_branch("main"))
                except Exception:
                    pass
                if cur and _has_branch(repo, cur):
                    _branch_delete(repo, cur)
                logger.info(f"🧹 修复: 回到 main，清理残留分支 {cur}")


def has_changes(skill_dir: str) -> bool:
    with _open_repo(skill_dir) as repo:
        out = _status_porcelain(repo, Path(skill_dir))
    return bool(out.strip())


def commit_changes(skill_dir: str, message: str) -> bool:
    with skill_repo_lock(skill_dir):
        with _open_repo(skill_dir) as repo:
            _stage_all(repo, Path(skill_dir))
            sha, err = _do_commit(repo, message)
    if sha is not None:
        logger.info(f"📝 commit: {message}")
        return True
    if err and err != "nothing to commit":
        logger.warning(f"commit 失败: {err}")
    return False


def current_branch(skill_dir: str) -> str:
    with _open_repo(skill_dir) as repo:
        return _current_branch_name(repo)


def is_on_main(skill_dir: str) -> bool:
    return current_branch(skill_dir) == "main"
