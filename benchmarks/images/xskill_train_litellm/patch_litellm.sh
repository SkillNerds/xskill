#!/usr/bin/env bash
# 只把「上游地址从哪来」改成可覆盖，算法一个字不动。
#
# 官方 xskill 训练镜像把地址写死在两处：
#   1) entrypoint_train.sh 里 `export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`
#      （rollout 解题带，alfworld/officeqa 还有 val 解题带）
#   2) /app/config.yaml 里 llm.base_url 与 embedding.base_url（xskill 自己的蒸馏与向量检索）
# 写死就意味着训练请求绕开 LiteLLM，代理那边既看不到也管不到。
#
# 这里把两处换成占位符加环境变量默认值：不设变量时行为与官方镜像逐字相同，
# 设了 XSKILL_ANTHROPIC_BASE_URL / XSKILL_LLM_BASE_URL / XSKILL_EMBEDDING_BASE_URL
# 就整体改道 LiteLLM。钥匙不用另外接：镜像本来就拿 DEEPSEEK_API_KEY 与
# DASHSCOPE_API_KEY 当 key，把它们换成代理的 key 即可。
set -euo pipefail

ENTRY=/app/entrypoint_train.sh
CONFIG=/app/config.yaml
DS_ANTHROPIC='https://api.deepseek.com/anthropic'
DS_OPENAI='https://api.deepseek.com'
DASHSCOPE='https://dashscope.aliyuncs.com/compatible-mode/v1'

[ -f "$ENTRY" ] || { echo "FATAL: $ENTRY missing"; exit 1; }
[ -f "$CONFIG" ] || { echo "FATAL: $CONFIG missing"; exit 1; }

n_anthropic=$(grep -c "export ANTHROPIC_BASE_URL=$DS_ANTHROPIC" "$ENTRY" || true)
[ "$n_anthropic" -ge 1 ] || { echo "FATAL: no hardcoded ANTHROPIC_BASE_URL in $ENTRY"; exit 1; }
grep -q "base_url: $DS_OPENAI\$" "$CONFIG" || { echo "FATAL: no llm.base_url in $CONFIG"; exit 1; }
grep -q "base_url: $DASHSCOPE" "$CONFIG" || { echo "FATAL: no embedding.base_url in $CONFIG"; exit 1; }
grep -q '__DASHSCOPE_API_KEY__' "$ENTRY" || { echo "FATAL: config render block not found"; exit 1; }

# 1) 解题带的 Claude Code 端点
sed -i \
  "s|export ANTHROPIC_BASE_URL=$DS_ANTHROPIC|export ANTHROPIC_BASE_URL=\"\${XSKILL_ANTHROPIC_BASE_URL:-$DS_ANTHROPIC}\"|g" \
  "$ENTRY"

# 2) config.yaml 的两个 base_url 换占位符，再在 entrypoint 的渲染里补两条替换
sed -i "s|base_url: $DS_OPENAI\$|base_url: __LLM_BASE_URL__|" "$CONFIG"
sed -i "s|base_url: $DASHSCOPE|base_url: __EMBEDDING_BASE_URL__|" "$CONFIG"
sed -i \
  "s|-e \"s#__DASHSCOPE_API_KEY__#\$DASHSCOPE_API_KEY#g\" \\\\|-e \"s#__DASHSCOPE_API_KEY__#\$DASHSCOPE_API_KEY#g\" \\\\\n    -e \"s#__LLM_BASE_URL__#\${XSKILL_LLM_BASE_URL:-$DS_OPENAI}#g\" \\\\\n    -e \"s#__EMBEDDING_BASE_URL__#\${XSKILL_EMBEDDING_BASE_URL:-$DASHSCOPE}#g\" \\\\|" \
  "$ENTRY"

# 校验：写死的地址必须没了，三个覆盖变量必须都在，占位符必须都被渲染语句覆盖
! grep -q "export ANTHROPIC_BASE_URL=$DS_ANTHROPIC" "$ENTRY" \
  || { echo "FATAL: hardcoded anthropic endpoint survived"; exit 1; }
for var in XSKILL_ANTHROPIC_BASE_URL XSKILL_LLM_BASE_URL XSKILL_EMBEDDING_BASE_URL; do
  grep -q "$var" "$ENTRY" || { echo "FATAL: $var not wired"; exit 1; }
done
grep -q '__LLM_BASE_URL__' "$CONFIG" || { echo "FATAL: llm placeholder missing"; exit 1; }
grep -q '__EMBEDDING_BASE_URL__' "$CONFIG" || { echo "FATAL: embedding placeholder missing"; exit 1; }
bash -n "$ENTRY" || { echo "FATAL: patched entrypoint is not valid bash"; exit 1; }

echo "patched: anthropic_sites=$n_anthropic entrypoint=$ENTRY config=$CONFIG"
