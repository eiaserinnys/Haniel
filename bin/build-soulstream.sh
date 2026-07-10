#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/eias/services/haniel/services/soulstream
ORCH_SHARED=/home/eias/services/soulstream-orch-server/shared
TS_SHARED=/home/eias/services/soulstream-soul-server-ts/shared

cd "$APP_DIR"

ln -sfn "$ORCH_SHARED/.env" .env
ln -sfn "$TS_SHARED/.env.soul-server-ts" .env.soul-server-ts
ln -sfn "$TS_SHARED/agents.yaml" agents.yaml

mkdir -p .git/info
for ignored in .env .env.soul-server-ts agents.yaml node_modules .venv; do
  grep -qxF "$ignored" .git/info/exclude 2>/dev/null || printf '%s
' "$ignored" >> .git/info/exclude
done

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ./packages/soul-common
python -m pip install -e ./orch-server

clean_env=(
  env -i
  HOME=/home/eias
  USER=eias
  LOGNAME=eias
  SHELL=/bin/bash
  PATH=/home/eias/.local/bin:/home/eias/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
  NODE_ENV=production
  COREPACK_ENABLE_DOWNLOAD_PROMPT=0
)

"${clean_env[@]}" pnpm install --frozen-lockfile
"${clean_env[@]}" pnpm --filter @soulstream/soul-server-ts run build
"${clean_env[@]}" pnpm --filter @soulstream/orch-server-ts run build
test -f orch-server-ts/dist/production_main.js
(
  cd unified-dashboard
  "${clean_env[@]}" pnpm run build
)
