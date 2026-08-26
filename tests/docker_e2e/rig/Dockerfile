# xskill Docker E2E 测试基础镜像
# 用 patentdagger-api:latest 作 base 直接绕开 docker hub pull——本机 dockerd
# 拉 docker hub 偶发 TLS handshake timeout（curl 通但 daemon 不通），复用本机
# 已有的 Python 3.11 / Debian trixie 镜像最稳。CI 上若 patentdagger-api 不存
# 在，可以改回 `FROM python:3.11-bookworm`。
# host 在 Singapore，**不**切大陆 mirror（清华源等从 SG 反而慢）。
FROM patentdagger-api:latest

# patentdagger-api 默认 USER 是 appuser(uid=1000)，apt / 写 /usr/local 都被拒；
# 安装阶段必须显式切回 root，最后再切到 xtest 模拟真用户。
USER root

# host 是阿里云 SG，到 deb.debian.org/Fastly 实测 stall 在 9.6MB Packages 列表
# （多次 Ign 重试，apt 30 分钟跑不完）；阿里云内网 mirror.cloud.aliyuncs.com 实
# 测 6 MB/s。这是**内网**链路、不是清华那种外网大陆 mirror，所以优先换源；
# 内网源不可用时恢复 Debian 官方源，避免一次 502 阻断整个 E2E。
ARG XSKILL_APT_MIRROR=http://mirrors.cloud.aliyuncs.com
RUN set -eu; \
    sed -i "s|http://deb.debian.org|${XSKILL_APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    if ! apt-get update; then \
        echo "APT mirror unavailable; falling back to deb.debian.org" >&2; \
        rm -rf /var/lib/apt/lists/*; \
        sed -i "s|${XSKILL_APT_MIRROR}|http://deb.debian.org|g" \
            /etc/apt/sources.list.d/debian.sources; \
        apt-get update; \
    fi; \
    apt-get install -y --no-install-recommends \
        git curl jq rsync build-essential; \
    rm -rf /var/lib/apt/lists/*

# 测试用户：非 root 跑 daemon，模拟真用户安装。
RUN useradd -m -s /bin/bash xtest
WORKDIR /home/xtest

# host 是阿里云 SG，pypi.org / files.pythonhosted.org 实测 fetch metadata 阶段
# Read timed out（rec'd 0 bytes 后挂死）；阿里云内网 pypi mirror 16 MB/s。
# 同 apt 的逻辑：优先内网源，失败时仅对当前安装命令回退官方 HTTPS 源。
ARG XSKILL_PIP_INDEX_URL=http://mirrors.cloud.aliyuncs.com/pypi/simple/
ARG XSKILL_PIP_TRUSTED_HOST=mirrors.cloud.aliyuncs.com
ENV PIP_INDEX_URL=${XSKILL_PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${XSKILL_PIP_TRUSTED_HOST} \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=3

# 探针工具 + uvicorn/fastapi（_fake_llm_server.py 依赖）
RUN pip install --no-cache-dir httpx pyyaml fastapi 'uvicorn[standard]' \
    || PIP_INDEX_URL=https://pypi.org/simple \
        PIP_TRUSTED_HOST= \
        pip install --no-cache-dir httpx pyyaml fastapi 'uvicorn[standard]'

# wheel 通过 build context 注入 —— rig/build.sh 会把 dist/*.whl 拷进来。
# 这里不写死文件名（版本号会变），用通配后 pip install。
COPY ./dist /tmp/xskill_dist
RUN (pip install --no-cache-dir --pre /tmp/xskill_dist/*.whl \
    || PIP_INDEX_URL=https://pypi.org/simple \
        PIP_TRUSTED_HOST= \
        pip install --no-cache-dir --pre /tmp/xskill_dist/*.whl) \
    && rm -rf /tmp/xskill_dist

# rig 内部脚本 + 复用 tests/_fake_llm_server.py 当 scenario 的假 LLM 后端
COPY tests/docker_e2e/rig/entrypoint.sh /rig/entrypoint.sh
COPY tests/docker_e2e/rig/probe.py      /rig/probe.py
COPY tests/_fake_llm_server.py          /rig/_fake_llm_server.py
RUN chmod +x /rig/entrypoint.sh /rig/probe.py && chown -R xtest:xtest /rig

USER xtest
ENV TESTHOME=/home/xtest/testhome
ENV XSKILL_PORT=18910
ENV FAKE_LLM_PORT=19999
ENTRYPOINT ["/rig/entrypoint.sh"]
