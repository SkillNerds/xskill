FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

# ── 系统依赖 + Node 20（claude CLI 运行时）────────────────────────────────
# git/bash 给脚本与 setuptools-scm 用；dulwich 让 xskill 不依赖系统 git，但留着无害。
RUN apt-get update && apt-get install -y --no-install-recommends \
        git bash curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── claude CLI（绝对路径在 entrypoint 用 $(command -v claude)）─────────────
RUN npm install -g @anthropic-ai/claude-code \
    && claude --version

# ── Python 第三方依赖 ─────────────────────────────────────────────────────
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ── xskill 本体（从烘焙源码 editable 装）──────────────────────────────────
# 源码烘焙后无 .git，setuptools-scm 无法推断版本 -> 用 PRETEND_VERSION 兜底。
COPY _ctx/xskill /app/xskill
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.6.1
RUN pip install --no-cache-dir -e /app/xskill \
    && xskill serve --help >/dev/null && echo "xskill serve OK"

# ── SkillOpt 本体（rollout 用 eval_only.py）───────────────────────────────
COPY _ctx/SkillOpt /app/SkillOpt
RUN pip install --no-cache-dir -e /app/SkillOpt || true
ENV PYTHONPATH=/app/SkillOpt

# ── 数据（26M）、配置、训练 split、sync 脚本、空 skill ────────────────────
COPY _ctx/data_root /data
WORKDIR /app
COPY config.yaml /app/config.yaml
COPY train_split/ /app/train_split/
COPY _ctx/sync_skills_to.sh /app/sync_skills_to.sh
COPY entrypoint_train.sh /app/entrypoint_train.sh
COPY multi_turn_rollout.py /app/multi_turn_rollout.py
RUN chmod +x /app/entrypoint_train.sh /app/sync_skills_to.sh \
    && : > /app/empty_skill.md

# IS_SANDBOX=1：容器以 root 运行，claude CLI 默认拒绝 root 下的
# --dangerously-skip-permissions；设此变量让其放行（容器本身即沙箱）。
# rollout 子壳里也会再设一次，这里兜底任何镜像内 claude 调用。
ENV SKILL_DIR=/shared/skill \
    EVAL_MODEL=deepseek-v4-flash \
    DATA_ROOT=/data \
    TRAIN_SPLIT=/app/train_split \
    XSKILL_PROMO_THRESHOLD=5 \
    IS_SANDBOX=1
CMD ["bash","/app/entrypoint_train.sh"]
