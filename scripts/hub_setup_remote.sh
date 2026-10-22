#!/usr/bin/env bash
# Install/update the Agent Remote Bridge Hub on this host. Idempotent.
# Expects /etc/arb/hub.json and /etc/arb/ilink.json to already be in place.
set -euo pipefail

REPO="${ARB_REPO:-https://github.com/littletinylee/agent-remote-bridge.git}"
APP=/opt/arb
ETC=/etc/arb
STATE=/var/lib/arb
PORT=18443

echo "==> packages"
if ! command -v git >/dev/null; then apt-get update -qq && apt-get install -y -qq git; fi
python3 -V

echo "==> code"
mkdir -p "$APP" "$STATE" "$ETC"
if [ -d "$APP/.git" ]; then
  git -C "$APP" fetch --depth 1 origin main
  git -C "$APP" reset --hard origin/main
else
  git clone --depth 1 "$REPO" "$APP"
fi
chmod 700 "$ETC" "$STATE"

echo "==> tls (self-signed, pinned by the nodes)"
if [ ! -f "$ETC/hub.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
    -keyout "$ETC/hub.key" -out "$ETC/hub.crt" \
    -subj "/CN=arb-hub" \
    -addext "subjectAltName=IP:120.78.79.184,DNS:arb-hub" 2>/dev/null
  chmod 600 "$ETC/hub.key"
fi
openssl x509 -in "$ETC/hub.crt" -noout -fingerprint -sha256 | sed 's/^/    /'

echo "==> systemd unit"
cat >/etc/systemd/system/arb-hub.service <<UNIT
[Unit]
Description=Agent Remote Bridge Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH=$APP/src
Environment=ARB_HOME=$STATE
WorkingDirectory=$APP
ExecStart=/usr/bin/python3 -m arb hub serve $ETC/hub.json
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$STATE

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable arb-hub >/dev/null 2>&1 || true
systemctl restart arb-hub
sleep 2
systemctl is-active arb-hub
echo "==> health"
python3 - "$PORT" <<'PY'
import json, ssl, sys, urllib.request
port = sys.argv[1]
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
try:
    with urllib.request.urlopen(f"https://127.0.0.1:{port}/v1/health", timeout=5, context=ctx) as r:
        print("   ", r.read().decode())
except Exception as exc:
    print("    health check failed:", exc)
PY

echo "==> listening"
ss -lntp | grep -E ":$PORT" || echo "    (not listening!)"
echo "==> logs"
journalctl -u arb-hub -n 8 --no-pager || true
