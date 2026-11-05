#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$HOME/.local/share/ponytail-mobile"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/ponytail"
CFG_FILE="$CFG_DIR/mobile.json"
SECRETS_FILE="$CFG_DIR/mobile-secrets.json"

mkdir -p "$ROOT" "$CFG_DIR"
install -m 700 "$HERE/free_gateway.py" "$ROOT/free_gateway.py"
install -m 700 "$HERE/configure_opencode.py" "$ROOT/configure_opencode.py"
install -m 700 "$HERE/ptmobile" "$ROOT/ptmobile"
[ -f "$CFG_FILE" ] || install -m 600 "$HERE/mobile.example.json" "$CFG_FILE"
chmod 600 "$CFG_FILE"

# Create the random loopback token atomically. Upstream API keys are added later
# with `ptmobile key ...` and never need to be sourced as shell code.
python "$ROOT/free_gateway.py" ensure-secrets
chmod 600 "$SECRETS_FILE"

printf 'Installing Termux dependencies...\n'
pkg install -y proot-distro python git curl >/dev/null

if ! proot-distro login ubuntu -- /bin/true >/dev/null 2>&1; then
  printf 'Installing Ubuntu proot...\n'
  proot-distro install ubuntu
fi

printf 'Installing OpenCode in Ubuntu...\n'
proot-distro login ubuntu -- bash -lc '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends curl ca-certificates git python3 >/dev/null
  mkdir -p /root/projects /root/.config/opencode /root/.config/ponytail
  if [ ! -x /root/.opencode/bin/opencode ]; then
    curl -fsSL https://opencode.ai/install | bash
  fi
  grep -q ".opencode/bin" /root/.profile 2>/dev/null || echo '\''export PATH="$HOME/.opencode/bin:$PATH"'\'' >> /root/.profile
'

# Only the random localhost token crosses into the Ubuntu proot. Provider keys
# stay in Termux's mode-0600 mobile-secrets.json.
python "$ROOT/free_gateway.py" token | proot-distro login ubuntu -- python3 -c '
from pathlib import Path
import sys
p = Path("/root/.config/ponytail/gateway.token")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(sys.stdin.read().strip() + "\n")
p.chmod(0o600)
'

# OpenCode 2 (current) and OpenCode 1 use different config schemas. Ponytail
# supports both plugin APIs, so detect the installed major and write only the
# matching provider/plugin shape.
OC_VERSION="$(proot-distro login ubuntu -- /root/.opencode/bin/opencode --version 2>/dev/null | head -1 | tr -d 'v[:space:]')"
OC_MAJOR="${OC_VERSION%%.*}"
[[ "$OC_MAJOR" =~ ^[0-9]+$ ]] || OC_MAJOR=2

cat "$ROOT/configure_opencode.py" | proot-distro login ubuntu -- python3 - --major "$OC_MAJOR"

install -m 700 "$ROOT/ptmobile" "$PREFIX/bin/ptmobile"

printf '\nPonytail Mobile installed.\n'
printf '1) Add at least one free API key, for example:\n'
printf '   ptmobile key gemini\n'
printf '   ptmobile key groq\n'
printf '   ptmobile key openrouter\n'
printf '2) Check the installation: ptmobile doctor\n'
printf '3) Check routes: ptmobile status\n'
printf '4) Create/open a project: ptmobile init myproject\n'
printf '\nProvider keys stay only in %s (0600).\n' "$SECRETS_FILE"
