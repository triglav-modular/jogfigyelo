#!/usr/bin/env bash
# Publish the dashboard to https://amunka.hu/jogfigyelo/ over FTP.
#
#   ./deploy.sh            build the page, export data.json and feed.xml, upload all
#   ./deploy.sh --data     export and upload data.json and feed.xml only (after a run)
#   ./deploy.sh --dry-run  build and export, then list what would be uploaded
#
# The page is rebuilt from the live site's chrome on every full deploy
# (build_page.py), so a change to the site's menu or theme reaches it then.
#
# Uploads only add or replace files in /jogfigyelo; nothing on the server is
# deleted. Credentials come from .env (FTPhost, FTPuser, FTPpass: the same
# keys as the aMunka repo's).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REMOTE="amunka:/web/amunka.hu/jogfigyelo"

env_get() {
	sed -nE "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ROOT/.env" | head -1 |
		sed -E "s/[[:space:]]+\$//; s/^[\"']//; s/[\"']\$//"
}

host="$(env_get FTPhost)"
host="${host#*://}"
host="${host%%/*}"

export RCLONE_CONFIG_AMUNKA_TYPE=ftp
export RCLONE_CONFIG_AMUNKA_HOST="$host"
export RCLONE_CONFIG_AMUNKA_USER="$(env_get FTPuser)"
export RCLONE_CONFIG_AMUNKA_PASS="$(env_get FTPpass | rclone obscure -)"

python3 "$ROOT/jogfigyelo.py" --export "$ROOT/web/data.json"
[ "${1:-}" = "--data" ] || python3 "$ROOT/build_page.py"

case "${1:-}" in
	--data) rclone copy "$ROOT/web" "$REMOTE" --include /data.json --include /feed.xml ;;
	--dry-run) rclone copy "$ROOT/web" "$REMOTE" --exclude dashboard.html --dry-run ;;
	"") rclone copy "$ROOT/web" "$REMOTE" --exclude dashboard.html ;;
	*) echo "usage: $0 [--data|--dry-run]" >&2; exit 2 ;;
esac
