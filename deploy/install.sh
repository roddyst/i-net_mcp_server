#!/usr/bin/env bash
# Install or update the i-net HelpDesk MCP server as a systemd service.
#
# The same command does both: on a fresh machine it creates the service user,
# the virtualenv, the env file and the unit; on an existing installation it
# pulls the requested version, keeps /etc/inet-helpdesk-mcp/env untouched and
# restarts the service.
#
#   sudo ./deploy/install.sh                     # newest main
#   sudo ./deploy/install.sh --ref v0.2.0        # a tag, branch or commit
#   sudo ./deploy/install.sh --port 9100
#
# Without a clone:
#   curl -fsSL https://raw.githubusercontent.com/roddyst/i-net_mcp_server/main/deploy/install.sh \
#     | sudo bash -s -- --ref main

set -euo pipefail

REPO_URL="https://github.com/roddyst/i-net_mcp_server"
RAW_URL="https://raw.githubusercontent.com/roddyst/i-net_mcp_server"
REF="main"
HOST="127.0.0.1"
PORT="8765"
SERVICE_USER="inet-mcp"
SERVICE="inet-helpdesk-mcp"
PREFIX="/opt/inet-helpdesk-mcp"
CONFIG_DIR="/etc/inet-helpdesk-mcp"
UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
INSTALL_SERVICE=1

usage() {
    cat <<EOF
Usage: $0 [options]

  --ref REF        Git branch, tag or commit to install (default: main)
  --host ADDRESS   Bind address of the service (default: 127.0.0.1)
  --port PORT      Port of the service (default: 8765)
  --prefix DIR     Installation directory (default: /opt/inet-helpdesk-mcp)
  --no-service     Only install the package, do not touch systemd
  -h, --help       Show this help

Running it again updates an existing installation; the env file with the
credentials (${CONFIG_DIR}/env) is never overwritten.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref) REF="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --prefix) PREFIX="$2"; shift 2 ;;
        --no-service) INSTALL_SERVICE=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Please run as root (sudo $0 ...)."
[[ "$PORT" =~ ^[0-9]+$ ]] || die "--port must be a number, got '$PORT'."

VENV="${PREFIX}/venv"
BIN="${VENV}/bin/inet-helpdesk-mcp"
PACKAGE="git+${REPO_URL}@${REF}"
# Deployment files next to this script, if it runs from a clone.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"

# Copy a file from deploy/ - from the clone if there is one, else from GitHub
# at the requested ref, so unit and env template match the installed version.
fetch_deploy_file() {
    local name="$1" target="$2"
    if [[ -n "$SCRIPT_DIR" && -f "${SCRIPT_DIR}/${name}" ]]; then
        cp "${SCRIPT_DIR}/${name}" "$target"
    else
        curl -fsSL "${RAW_URL}/${REF}/deploy/${name}" -o "$target" \
            || die "Could not download deploy/${name} for ref '${REF}'."
    fi
}

# -- prerequisites ------------------------------------------------------------

missing=()
command -v git >/dev/null || missing+=(git)
command -v python3 >/dev/null || missing+=(python3)
command -v curl >/dev/null || missing+=(curl)
if command -v python3 >/dev/null && ! python3 -c 'import ensurepip, venv' 2>/dev/null; then
    missing+=(python3-venv)
fi
if [[ ${#missing[@]} -gt 0 ]]; then
    if command -v apt-get >/dev/null; then
        log "Installing ${missing[*]}"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
    else
        die "Missing ${missing[*]}; please install them and run again."
    fi
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 or newer is required, found $(python3 --version)."

# -- service user and package -------------------------------------------------

if [[ $INSTALL_SERVICE -eq 1 ]] && ! id "$SERVICE_USER" >/dev/null 2>&1; then
    log "Creating service user ${SERVICE_USER}"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

PREVIOUS=""
if [[ -x "$BIN" ]]; then
    PREVIOUS="$("$VENV/bin/pip" show inet-helpdesk-mcp 2>/dev/null \
        | sed -n 's/^Version: //p')"
    log "Updating the existing installation in ${PREFIX} to '${REF}'"
else
    log "Installing into ${PREFIX} (ref '${REF}')"
    install -d "$PREFIX"
    python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet --upgrade "$PACKAGE"
# The version number does not change with every commit, so pip would keep an
# older build of the same version: reinstall the package itself every time.
"$VENV/bin/pip" install --quiet --force-reinstall --no-deps "$PACKAGE"

COMMIT="$(git ls-remote "$REPO_URL" "$REF" 2>/dev/null | awk 'NR==1 {print substr($1, 1, 7)}')"
VERSION="$("$BIN" --version)"
log "Installed ${VERSION}${COMMIT:+ (commit ${COMMIT})}${PREVIOUS:+, previously ${PREVIOUS}}"

if [[ $INSTALL_SERVICE -eq 0 ]]; then
    log "Done. Binary: ${BIN}"
    exit 0
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$PREFIX"

# -- configuration -------------------------------------------------------------

install -d -m 750 -o root -g "$SERVICE_USER" "$CONFIG_DIR"
ENV_FILE="${CONFIG_DIR}/env"
if [[ ! -f "$ENV_FILE" ]]; then
    log "Creating ${ENV_FILE} from the template"
    tmp="$(mktemp)"
    fetch_deploy_file "inet-helpdesk-mcp.env.example" "$tmp"
    install -m 640 -o root -g "$SERVICE_USER" "$tmp" "$ENV_FILE"
    rm -f "$tmp"
else
    log "Keeping the existing ${ENV_FILE}"
fi

# -- systemd unit ----------------------------------------------------------------

tmp="$(mktemp)"
fetch_deploy_file "inet-helpdesk-mcp.service" "$tmp"
sed -i \
    -e "s|/opt/inet-helpdesk-mcp/venv|${VENV}|g" \
    -e "s|--host 127.0.0.1|--host ${HOST}|" \
    -e "s|--port 8765|--port ${PORT}|" \
    "$tmp"
if [[ -f "$UNIT_FILE" ]] && ! cmp -s "$tmp" "$UNIT_FILE"; then
    cp "$UNIT_FILE" "${UNIT_FILE}.bak"
    warn "The unit changed; the previous one is saved as ${UNIT_FILE}.bak"
fi
install -m 644 "$tmp" "$UNIT_FILE"
rm -f "$tmp"
systemctl daemon-reload

if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]]; then
    warn "The service listens on ${HOST}. It does not authenticate clients itself -" \
         "put a reverse proxy with TLS and authentication in front of it."
fi

# Do not start with the placeholders of the template: every call would fail.
if grep -qE '^INET_TOKEN=hier-den-access-token-eintragen|^INET_BASE_URL=https://helpdesk\.example\.com' "$ENV_FILE"; then
    systemctl enable "$SERVICE" >/dev/null 2>&1
    warn "${ENV_FILE} still contains the template values."
    echo "  1. Enter INET_BASE_URL and INET_TOKEN:  editor ${ENV_FILE}"
    echo "  2. Start the service:                   systemctl restart ${SERVICE}"
    exit 0
fi

systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"
sleep 2
if systemctl is-active --quiet "$SERVICE"; then
    log "Service ${SERVICE} is running on http://${HOST}:${PORT}/mcp"
    echo "  Logs:  journalctl -u ${SERVICE} -f"
    echo "  Check: call the server_info tool from the agent - \"connection\": \"ok\""
else
    journalctl -u "$SERVICE" -n 30 --no-pager >&2 || true
    die "The service did not start; see the log above."
fi
