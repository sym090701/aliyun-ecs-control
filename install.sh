#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
INSTALL_DIR=/opt/aliyun-ecs-control
ENV_FILE=/etc/aliyun-ecs-control.env
TARGETS_FILE=/etc/aliyun-ecs-control-targets.json
MANAGED_CONFIG_PATH=/var/lib/aliyun-ecs-control/config.json
CREDENTIAL_KEY_PATH=/etc/aliyun-ecs-control.key
AUDIT_PATH=/var/lib/aliyun-ecs-control/audit.log
PASSKEY_PATH=/var/lib/aliyun-ecs-control/passkeys.json
SERVICE_USER=aliyun-ecs-control

command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required." >&2; exit 1; }

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/aliyun-ecs-control --shell /usr/sbin/nologin "$SERVICE_USER"
fi

if [[ -d "$INSTALL_DIR" ]]; then
  BACKUP_DIR="${INSTALL_DIR}.backup.$(date +%Y%m%d-%H%M%S)"
  cp -a "$INSTALL_DIR" "$BACKUP_DIR"
  echo "Existing installation backed up to $BACKUP_DIR"
fi

install -d -m 0755 "$INSTALL_DIR"
cp -a "$SCRIPT_DIR"/. "$INSTALL_DIR"/
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --disable-pip-version-check "$INSTALL_DIR"

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 /var/lib/aliyun-ecs-control
if [[ ! -f "$CREDENTIAL_KEY_PATH" ]]; then
  umask 077
  "$INSTALL_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' > "$CREDENTIAL_KEY_PATH"
fi
chown root:"$SERVICE_USER" "$CREDENTIAL_KEY_PATH"
chmod 0640 "$CREDENTIAL_KEY_PATH"

if [[ ! -f "$TARGETS_FILE" ]]; then
  "$INSTALL_DIR/.venv/bin/aliyun-ecs-control" init-config --path "$TARGETS_FILE"
else
  echo "Keeping existing targets file: $TARGETS_FILE"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  CONTROL_TOKEN=$("$INSTALL_DIR/.venv/bin/aliyun-ecs-control" generate-token)
  read -r -p "Enable TOTP two-factor login? [Y/n]: " ENABLE_TOTP
  ENABLE_TOTP=${ENABLE_TOTP:-Y}
  TOTP_SECRET=""
  TOTP_URI=""
  if [[ "$ENABLE_TOTP" =~ ^[Yy]$ ]]; then
    TOTP_OUTPUT=$("$INSTALL_DIR/.venv/bin/aliyun-ecs-control" generate-totp --issuer "Aliyun ECS Fleet" --account admin)
    TOTP_SECRET=$(printf '%s\n' "$TOTP_OUTPUT" | sed -n 's/^TOTP_SECRET=//p')
    TOTP_URI=$(printf '%s\n' "$TOTP_OUTPUT" | tail -n 1)
  fi
  umask 077
  {
    printf 'CONTROL_TOKEN=%s\n' "$CONTROL_TOKEN"
    printf 'TOTP_SECRET=%s\n' "$TOTP_SECRET"
    printf 'TARGETS_FILE=%s\n' "$TARGETS_FILE"
    printf 'MANAGED_CONFIG_PATH=%s\n' "$MANAGED_CONFIG_PATH"
    printf 'CREDENTIAL_KEY_PATH=%s\n' "$CREDENTIAL_KEY_PATH"
    printf 'AUDIT_PATH=%s\n' "$AUDIT_PATH"
    printf 'PASSKEY_PATH=%s\nPASSKEY_RP_ID=ali.hkz.241214.top\nPASSKEY_ORIGIN=https://ali.hkz.241214.top\n' "$PASSKEY_PATH"
    printf 'LISTEN_HOST=127.0.0.1\nLISTEN_PORT=8787\n'
    printf 'STATE_PATH=/var/lib/aliyun-ecs-control/state.json\nLOCK_DIR=/var/lib/aliyun-ecs-control/locks\n'
    printf 'SESSION_HOURS=8\nCOOKIE_SECURE=false\nTRUST_PROXY=false\n'
  } > "$ENV_FILE"
  echo
  echo "Control token: $CONTROL_TOKEN"
  if [[ -n "$TOTP_URI" ]]; then
    echo "TOTP secret: $TOTP_SECRET"
    echo "Add this otpauth URI to your authenticator:"
    echo "$TOTP_URI"
  fi
else
  echo "Keeping existing runtime configuration: $ENV_FILE"
fi

for SETTING in \
  "MANAGED_CONFIG_PATH=$MANAGED_CONFIG_PATH" \
  "CREDENTIAL_KEY_PATH=$CREDENTIAL_KEY_PATH" \
  "AUDIT_PATH=$AUDIT_PATH" \
  "PASSKEY_PATH=$PASSKEY_PATH" \
  "PASSKEY_RP_ID=ali.hkz.241214.top" \
  "PASSKEY_ORIGIN=https://ali.hkz.241214.top"; do
  KEY=${SETTING%%=*}
  if ! grep -q "^${KEY}=" "$ENV_FILE"; then
    printf '%s\n' "$SETTING" >> "$ENV_FILE"
  fi
done

chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"
chown root:root "$TARGETS_FILE"
chmod 0600 "$TARGETS_FILE"
if [[ ! -f "$MANAGED_CONFIG_PATH" ]]; then
  LEGACY_BACKUP="${TARGETS_FILE}.pre-v2.2.$(date +%Y%m%d-%H%M%S)"
  cp -a "$TARGETS_FILE" "$LEGACY_BACKUP"
  chown root:root "$LEGACY_BACKUP"
  chmod 0600 "$LEGACY_BACKUP"
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
  "$INSTALL_DIR/.venv/bin/aliyun-ecs-control" migrate-config
  echo "Legacy plaintext configuration backed up to $LEGACY_BACKUP"
fi
if [[ -f "$MANAGED_CONFIG_PATH" ]]; then
  chown "$SERVICE_USER":"$SERVICE_USER" "$MANAGED_CONFIG_PATH"
  chmod 0600 "$MANAGED_CONFIG_PATH"
fi

install -m 0644 "$INSTALL_DIR/systemd/aliyun-ecs-control.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/systemd/aliyun-ecs-policy.service" /etc/systemd/system/
install -m 0644 "$INSTALL_DIR/systemd/aliyun-ecs-policy.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable aliyun-ecs-control.service aliyun-ecs-policy.timer
systemctl restart aliyun-ecs-control.service aliyun-ecs-policy.timer

echo
echo "Installed. The app listens only on 127.0.0.1:8787."
echo "For public access, put Caddy or Nginx HTTPS in front, then set COOKIE_SECURE=true and TRUST_PROXY=true."
echo "Traffic policy and power schedules start paused; they can be enabled separately or combined after verification."
