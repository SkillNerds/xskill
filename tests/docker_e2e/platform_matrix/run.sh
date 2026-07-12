#!/usr/bin/env bash
# 平台矩阵 E2E — 多发行版容器里验证 supervised 常驻链路（无 systemd 环境）
#
# 容器天然没有 systemd user manager，正好逼出 supervised watchdog 降级链，
# 与「未启 systemd 的 WSL / 精简 Linux / 鸿蒙终端」同构。矩阵：
#   ubuntu    ubuntu:24.04                     现代 Debian 系
#   debian    debian:12-slim                   稳定 Debian 系
#   openeuler openeuler/openeuler:24.03-lts    鸿蒙服务器用户态最近似
#   harmony   openeuler + 覆写 /etc/os-release  鸿蒙识别与链路模拟
#
# 每个容器内跑 tests/e2e/test_supervised_selfheal_e2e.py（connect 常驻 →
# SIGKILL 子进程 → watchdog 自愈 → stop 全清理），harmony 额外断言
# _linux_flavor() == "harmony"。
#
# 用法：
#   run.sh [ubuntu|debian|openeuler|harmony|all]     缺省 all
# 环境：
#   PIP_INDEX_URL / PIP_TRUSTED_HOST        透传进容器（内网/镜像加速）
#   XSKILL_MATRIX_IMAGE_PREFIX              镜像仓库前缀（如
#                                           docker.m.daocloud.io/，直连
#                                           docker.io 受限的内网机用）
set -uo pipefail

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$THIS_DIR/../../.." && pwd)"

TARGET="${1:-all}"
if [ "$TARGET" = "all" ]; then
    MATRIX=(ubuntu debian openeuler harmony)
else
    MATRIX=("$TARGET")
fi

PREFIX="${XSKILL_MATRIX_IMAGE_PREFIX:-}"

image_of() {
    case "$1" in
        ubuntu)             echo "${PREFIX}ubuntu:24.04" ;;
        debian)             echo "${PREFIX}debian:12-slim" ;;
        openeuler|harmony)  echo "${PREFIX}openeuler/openeuler:24.03-lts" ;;
        *) echo "unknown distro: $1" >&2; return 2 ;;
    esac
}

# 容器内 bootstrap：装 python → venv → 装 xskill → 跑 e2e。
# 源码只读挂载在 /src；tar 复制到 /work（排除 .git 与宿主产物），避免容器
# 写宿主目录（egg-info / .pytest_cache）。
inner_script() {
    local distro="$1"
    cat <<'EOS'
set -euo pipefail
case "$DISTRO" in
  ubuntu|debian)
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q && apt-get install -yq python3 python3-pip python3-venv >/dev/null
    ;;
  openeuler|harmony)
    dnf install -yq python3 python3-pip >/dev/null
    ;;
esac
if [ "$DISTRO" = harmony ]; then
  printf 'NAME="HarmonyOS"\nID=harmonyos\nVERSION_ID="5.1"\n' > /etc/os-release
fi
mkdir -p /work
tar -C /src --exclude=.git --exclude='*.egg-info' --exclude=.pytest_cache \
    --exclude=node_modules -cf - . | tar -C /work -xf -
python3 -m venv /venv
/venv/bin/pip install -q --upgrade pip
/venv/bin/pip install -q '/work[dev]'
echo "== python: $(/venv/bin/python -V)  distro: $DISTRO =="
if [ "$DISTRO" = harmony ]; then
  /venv/bin/python - <<'PY'
from xskill.team.client.service import _linux_flavor, _is_harmony
assert _is_harmony(), "os-release 覆写后应识别为鸿蒙"
assert _linux_flavor() == "harmony", _linux_flavor()
print("harmony flavor detection OK")
PY
fi
cd /work && /venv/bin/python -m pytest tests/e2e/test_supervised_selfheal_e2e.py -v -p no:cacheprovider
EOS
}

FAILED=()
for distro in "${MATRIX[@]}"; do
    image="$(image_of "$distro")" || exit 2
    echo
    echo "───────────────────────────────────────────────"
    echo "▶ platform_matrix: $distro  ($image)"
    echo "───────────────────────────────────────────────"
    if docker run --rm \
        -v "$REPO":/src:ro \
        -e DISTRO="$distro" \
        ${PIP_INDEX_URL:+-e PIP_INDEX_URL} \
        ${PIP_TRUSTED_HOST:+-e PIP_TRUSTED_HOST} \
        "$image" bash -c "$(inner_script "$distro")"; then
        echo "✔ $distro PASSED"
    else
        echo "✘ $distro FAILED"
        FAILED+=("$distro")
    fi
done

echo
if [ "${#FAILED[@]}" -gt 0 ]; then
    echo "platform_matrix FAILED: ${FAILED[*]}" >&2
    exit 1
fi
echo "platform_matrix: all ${#MATRIX[@]} distro(s) passed."
