#!/usr/bin/env bash
# 建三个 xskill 官方训练镜像的副本，只让上游地址可覆盖，用新 tag，不动旧 tag。
set -euo pipefail
cd "$(dirname "$0")"
TAG_SUFFIX="${TAG_SUFFIX:-litellm-20260909}"

build_one() {  # $1=base image  $2=target image
  echo "=== $2"
  docker build --build-arg "BASE_IMAGE=$1" -t "$2" .
  docker push "$2" 2>/dev/null || echo "WARN: push failed (registry offline?); image is in the local daemon"
}

build_one \
  localhost:5000/p_user1/algo-xskill-officeqa:hard-canary-noforce-fix-20260909 \
  "localhost:5000/p_user1/algo-xskill-officeqa:hard-canary-noforce-fix-$TAG_SUFFIX"
build_one \
  localhost:5000/p_user1/algo-xskill-spreadsheet-full:noforce-mergeval-20260909 \
  "localhost:5000/p_user1/algo-xskill-spreadsheet-full:noforce-mergeval-$TAG_SUFFIX"
build_one \
  localhost:5000/p_user1/algo-xskill-alf-full:noforce-mergeval-fix-20260909 \
  "localhost:5000/p_user1/algo-xskill-alf-full:noforce-mergeval-fix-$TAG_SUFFIX"

echo "built with suffix $TAG_SUFFIX"
