#!/usr/bin/env bash
# Installs the DiscoPanel Crossplay Provisioner as an enabled systemd service.
# Run on the DiscoPanel host as root:  sudo ./install.sh
set -euo pipefail

APP_DIR=/opt/discopanel-crossplay
ENV_FILE=/etc/discopanel-crossplay.env
UNIT=/etc/systemd/system/discopanel-crossplay.service
RUN_USER="${RUN_USER:-niklas}"
SRC="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo ./install.sh)"; exit 1
fi

echo ">> Installing application to $APP_DIR"
mkdir -p "$APP_DIR/templates"
install -m 644 "$SRC/app.py" "$APP_DIR/app.py"
install -m 644 "$SRC/requirements.txt" "$APP_DIR/requirements.txt"
install -m 644 "$SRC/geyser-config.yml" "$APP_DIR/geyser-config.yml"
install -m 644 "$SRC/templates/index.html" "$APP_DIR/templates/index.html"

echo ">> Creating virtualenv and installing dependencies"
if [[ ! -x "$APP_DIR/.venv/bin/pip" ]]; then
  rm -rf "$APP_DIR/.venv"
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

chown -R "$RUN_USER":"$RUN_USER" "$APP_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  echo ">> Creating $ENV_FILE (fill in DP_TOKEN!)"
  install -m 600 "$SRC/config.example.env" "$ENV_FILE"
else
  echo ">> Keeping existing $ENV_FILE"
fi

echo ">> Installing systemd unit"
sed "s/^User=.*/User=$RUN_USER/" "$SRC/discopanel-crossplay.service" > "$UNIT"

# Scoped sudo rule so the service can pin a server container's restart policy to
# "no" (autostart then depends solely on DiscoPanel). Only this exact command is
# allowed, without a password.
DOCKER_BIN="$(command -v docker || echo /usr/bin/docker)"
SUDOERS=/etc/sudoers.d/discopanel-crossplay
echo ">> Installing scoped sudo rule ($SUDOERS)"
printf 'ALL=(root) NOPASSWD: %s update --restart no discopanel-server-*\n' \
  "$DOCKER_BIN" | sed "s/^/$RUN_USER /" > "$SUDOERS"
chmod 440 "$SUDOERS"
visudo -cf "$SUDOERS" >/dev/null || { echo "Invalid sudoers rule, removing"; rm -f "$SUDOERS"; }

systemctl daemon-reload
systemctl enable --now discopanel-crossplay.service

echo
echo "Done. Service status:"
systemctl --no-pager --full status discopanel-crossplay.service | head -n 8 || true
echo
echo "If DP_TOKEN was not set yet, edit $ENV_FILE and run:"
echo "  systemctl restart discopanel-crossplay.service"
echo "Web UI: http://$(hostname -I | awk '{print $1}'):5005"
