#!/usr/bin/env bash
# Install on a Debian/Ubuntu VPS: creates .venv, installs the package,
# copies example config, and installs + enables the systemd service.
# Usage: cd mm2-sniper && sudo bash deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"

if ! command -v python3 >/dev/null; then
  apt-get update && apt-get install -y python3 python3-venv
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
python3 -m venv --help >/dev/null 2>&1 || apt-get install -y python3-venv

cd "$DIR"
sudo -u "$RUN_USER" python3 -m venv .venv
sudo -u "$RUN_USER" .venv/bin/pip install --upgrade pip >/dev/null
sudo -u "$RUN_USER" .venv/bin/pip install .
sudo -u "$RUN_USER" mkdir -p data logs
[ -f .env ] || sudo -u "$RUN_USER" cp .env.example .env
[ -f config.toml ] || sudo -u "$RUN_USER" cp config.example.toml config.toml
chmod 600 .env

sed -e "s#__DIR__#$DIR#g" -e "s#__USER__#$RUN_USER#g" deploy/mm2sniper.service \
  > /etc/systemd/system/mm2sniper.service
systemctl daemon-reload
systemctl enable mm2sniper >/dev/null

echo
echo "Installed. Next:"
echo "  1. edit $DIR/.env and $DIR/config.toml"
echo "  2. $DIR/.venv/bin/mm2sniper check-config && $DIR/.venv/bin/mm2sniper probe"
echo "  3. sudo systemctl start mm2sniper && journalctl -u mm2sniper -f"
