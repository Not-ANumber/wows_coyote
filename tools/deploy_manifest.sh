#!/usr/bin/env bash
# 原子替换 wows_coyote 更新清单。
#
# 用法：
#   WOWS_UPDATE_HOST=root@<服务器IP> \
#   WOWS_UPDATE_PATH=<站点根>/wows_coyote \
#   tools/deploy_manifest.sh [releases/latest.json]
#
# 可选：
#   WOWS_UPDATE_URL=https://wowsdg-update.woyiba.com/wows_coyote/latest.json
#
# 要求：本地有 ssh/scp/curl；服务器上有 python3。不会自动上传，需显式设置 HOST。

set -euo pipefail

HOST="${WOWS_UPDATE_HOST:-}"
REMOTE_DIR="${WOWS_UPDATE_PATH:-}"
PUBLIC_URL="${WOWS_UPDATE_URL:-https://wowsdg-update.woyiba.com/wows_coyote/latest.json}"
MANIFEST="${1:-releases/latest.json}"

if [[ -z "$HOST" ]]; then
  echo "请设置 WOWS_UPDATE_HOST，例如：" >&2
  echo "  WOWS_UPDATE_HOST=root@<服务器IP> $0 $MANIFEST" >&2
  exit 2
fi

if [[ -z "$REMOTE_DIR" ]]; then
  echo "请设置 WOWS_UPDATE_PATH，例如：" >&2
  echo "  WOWS_UPDATE_PATH=<站点根>/wows_coyote $0 $MANIFEST" >&2
  exit 2
fi

if [[ ! -f "$MANIFEST" ]]; then
  echo "找不到清单：$MANIFEST" >&2
  exit 2
fi

if command -v py >/dev/null 2>&1; then
  LOCAL_PY=(py -3.13)
elif command -v python3 >/dev/null 2>&1; then
  LOCAL_PY=(python3)
else
  echo "本地需要 py 或 python3 来校验 JSON" >&2
  exit 2
fi

"${LOCAL_PY[@]}" -m json.tool "$MANIFEST" >/dev/null
echo "本地 JSON 校验通过：$MANIFEST"

scp "$MANIFEST" "$HOST:$REMOTE_DIR/latest.json.tmp"

ssh "$HOST" "cd '$REMOTE_DIR' && \
  python3 -m json.tool latest.json.tmp >/dev/null && \
  { cp -f latest.json latest.json.prev 2>/dev/null || true; } && \
  mv -f latest.json.tmp latest.json && \
  chmod 644 latest.json"

echo "服务器清单已替换，保留 latest.json.prev"

curl -fsS "$PUBLIC_URL" | "${LOCAL_PY[@]}" -m json.tool
echo "公网校验通过：$PUBLIC_URL"
