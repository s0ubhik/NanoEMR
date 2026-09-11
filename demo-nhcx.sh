#!/usr/bin/env bash
# Brings up the claims stack on this machine and leaves it running:
#   NanoEMR  ->  nhcx-adapter  ->  a mock NHCX exchange with a payer behind it
# Open http://127.0.0.1:8765/claims
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
HARNESS="$ROOT/harness"
ADAPTER="$ROOT/nhcx-adapter/nhcx-adapter"
STATE="$HERE/.nhcx-demo"

APP_PORT="${APP_PORT:-8765}"
ADAPTER_PORT="${ADAPTER_PORT:-8090}"
GATEWAY_PORT="${GATEWAY_PORT:-8080}"
PROVIDER="${PROVIDER_CODE:-1000004446@hcx}"
PAYER="${PAYER_CODE:-1000003538@hcx}"
API_KEY="local-demo-key"

[[ -x "$ADAPTER" ]] || { echo "adapter not built. Run: (cd $ROOT/nhcx-adapter && make build)" >&2; exit 1; }
[[ -d "$HARNESS/lib" ]] || { echo "harness not found at $HARNESS" >&2; exit 1; }

cleanup(){ echo; echo "stopping..."; kill $(jobs -p) 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

for p in "$APP_PORT" "$ADAPTER_PORT" "$GATEWAY_PORT"; do lsof -ti tcp:$p 2>/dev/null | xargs -r kill 2>/dev/null || true; done
/bin/rm -rf "$STATE"; mkdir -p "$STATE"

python3 "$HARNESS/lib/gen_keys.py" "$STATE/keys" >/dev/null
cp "$STATE/keys/provider_private.key"     "$STATE/private_key.pem"
cp "$STATE/keys/provider_certificate.crt" "$STATE/certificate.pem"

cat > "$STATE/config.json" <<JSON
{ "env": "sandbox", "listen": "127.0.0.1:$ADAPTER_PORT",
  "publicUrl": "http://127.0.0.1:$ADAPTER_PORT/in",
  "apiKey": "$API_KEY", "requireApiKey": true,
  "participant": { "participantId": "$PROVIDER", "name": "NanoEMR Hospital",
    "clientId": "LOCAL_DEMO", "clientSecret": "local-demo-secret",
    "privateKey": "@private_key.pem" },
  "callback": { "url": "http://127.0.0.1:$APP_PORT/callback", "appendPath": true, "timeoutSeconds": 20 },
  "urls": { "nhcx": "http://127.0.0.1:$GATEWAY_PORT/hcx/v1",
            "participant": "http://127.0.0.1:$GATEWAY_PORT/participanthcxservice",
            "sessions": "http://127.0.0.1:$GATEWAY_PORT/api/hiecm/gateway/v3/sessions" },
  "auth": { "mode": "sessions", "tokenTtlSeconds": 1200 },
  "ledger": { "enabled": true, "dir": "ledger" },
  "log": { "level": "info", "format": "text" } }
JSON

echo "starting mock NHCX exchange on $GATEWAY_PORT"
python3 "$HARNESS/lib/mock_gateway.py" --port "$GATEWAY_PORT" --keys-dir "$STATE/keys" \
  --certs-format json --provider-code "$PROVIDER" --payer-code "$PAYER" \
  --callback-base "http://127.0.0.1:$ADAPTER_PORT" > "$STATE/gateway.log" 2>&1 &
sleep 2

echo "starting nhcx-adapter on $ADAPTER_PORT"
( cd "$STATE" && "$ADAPTER" serve --config "$STATE/config.json" \
    --skip-checks --no-tui --no-banner --no-update-check > "$STATE/adapter.log" 2>&1 & )
sleep 2

echo "starting NanoEMR on $APP_PORT"
export NANOEMR_HCXKIT_URL="http://127.0.0.1:$ADAPTER_PORT"
export NANOEMR_HCXKIT_API_KEY="$API_KEY"
export NANOEMR_PAYER_CODE="$PAYER"
export NANOEMR_PAYER_NAME="Harness TPA"
( cd "$HERE" && python3 run.py --port "$APP_PORT" ${DEMO:+--demo} ${RESET:+--reset} > "$STATE/app.log" 2>&1 & )

for i in $(seq 1 90); do
  curl -sf "http://127.0.0.1:$APP_PORT/claims" >/dev/null 2>&1 && break
  sleep 1
done

cat <<TXT

  ready.

  NanoEMR          http://127.0.0.1:$APP_PORT
  NHCX claims      http://127.0.0.1:$APP_PORT/claims
  Adjudicator      http://127.0.0.1:$APP_PORT/adjudicator

  This hospital    $PROVIDER
  Payer            $PAYER
  Adapter          http://127.0.0.1:$ADAPTER_PORT

  Live traffic:    tail -f $STATE/adapter.log
  Ctrl-C to stop everything.

TXT
wait
