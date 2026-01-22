
#!/usr/bin/env bash
set -euo pipefail

# =========================
# Configurable flags (env)
# =========================
: "${SETUP_SERIAL:=1}"       # 1 = add user to 'dialout'; 0 = skip
: "${SETUP_REDIS:=1}"        # 1 = install/configure redis; 0 = skip
: "${USE_UPSTREAM:=0}"       # 1 = use packages.redis.io; 0 = use distro repo (recommended)
: "${REDIS_CONF:=/etc/redis/redis.conf}"
: "${MAKE_RUN_SH_EXEC:=1}"   # 1 = chmod +x ./run.sh if present

# =========================
# Logging helpers
# =========================
info() { printf "\033[1;34m[setup]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[err]\033[0m %s\n" "$*" >&2; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Missing required command: $1"; exit 1; }
}

ensure_sudo() {
  # Prompt once for sudo; fail fast if not available
  sudo -v >/dev/null 2>&1 || { err "sudo is required for this setup"; exit 1; }
}

detect_debian_like() {
  if command -v apt-get >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

# =========================
# Serial / dialout group
# =========================
setup_serial() {
  info "Configuring serial access (dialout group)"
  ensure_sudo

  # Determine target user (supports sudo)
  local TARGET_USER="${SUDO_USER:-$USER}"

  # Create group if missing
  if ! getent group dialout >/dev/null; then
    info "Creating 'dialout' group"
    sudo groupadd dialout
  fi

  # Add user if not member
  if id -nG "$TARGET_USER" | tr ' ' '\n' | grep -qx "dialout"; then
    info "User '$TARGET_USER' is already in 'dialout' group"
  else
    info "Adding '$TARGET_USER' to 'dialout' group"
    sudo usermod -a -G dialout "$TARGET_USER"
    warn "You must log out and back in (or run 'newgrp dialout') for this to take effect in current shell."
  fi
}

# =========================
# Redis installation
# =========================
setup_redis() {
  info "Installing and configuring Redis"

  if ! detect_debian_like; then
    err "This script currently supports Debian/Ubuntu (apt). Please install Redis manually on this distro."
    exit 1
  fi

  ensure_sudo
  sudo apt-get update -y
  sudo apt-get install -y lsb-release curl gpg ca-certificates

  if [[ "$USE_UPSTREAM" == "1" ]]; then
    info "Using upstream Redis repository (packages.redis.io)"
    # Add key once
    if [[ ! -f /usr/share/keyrings/redis-archive-keyring.gpg ]]; then
      curl -fsSL https://packages.redis.io/gpg \
        | sudo gpg --dearmor -o /usr/share/keyrings/redis-archive-keyring.gpg
      sudo chmod 644 /usr/share/keyrings/redis-archive-keyring.gpg
    else
      info "Upstream Redis key already present"
    fi

    # Add repo once
    if [[ ! -f /etc/apt/sources.list.d/redis.list ]]; then
      echo "deb [signed-by=/usr/share/keyrings/redis-archive-keyring.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" \
        | sudo tee /etc/apt/sources.list.d/redis.list >/dev/null
    else
      info "Upstream Redis repo already configured"
    fi

    sudo apt-get update -y
  else
    info "Using distro Redis (set USE_UPSTREAM=1 to switch to packages.redis.io)"
  fi

  # Install redis-server if present, fallback to redis
  if apt-cache policy redis-server | grep -q Candidate; then
    sudo apt-get install -y redis-server
  else
    sudo apt-get install -y redis
  fi

  # Harden Redis: bind localhost, protected mode, systemd supervision
  if [[ -f "$REDIS_CONF" ]]; then
    info "Hardening $REDIS_CONF (bind 127.0.0.1 ::1, protected-mode yes, supervised systemd)"
    sudo sed -i \
      -e 's/^\s*#\?\s*bind .*/bind 127.0.0.1 ::1/' \
      -e 's/^\s*#\?\s*protected-mode .*/protected-mode yes/' \
      -e 's/^\s*#\?\s*supervised .*/supervised systemd/' \
      "$REDIS_CONF"
  else
    warn "Redis config not found at $REDIS_CONF; skipping hardening"
  fi

  # Enable & restart service
  sudo systemctl enable redis-server || true
  sudo systemctl restart redis-server || true

  # Diagnostics
  if command -v redis-cli >/dev/null 2>&1; then
    info "Pinging Redis on 127.0.0.1:6379..."
    if redis-cli -h 127.0.0.1 -p 6379 ping | grep -q PONG; then
      info "Redis is up (PONG)"
    else
      warn "redis-cli ping did not return PONG"
    fi
  else
    warn "redis-cli not found (may be provided by redis-tools on some distros)"
  fi

  # Print service status (non-fatal if systemctl not available)
  sudo systemctl --no-pager --full status redis-server || true
}

# =========================
# Misc convenience
# =========================
maybe_mark_run_executable() {
  if [[ "${MAKE_RUN_SH_EXEC}" == "1" && -f "./run.sh" ]]; then
    chmod +x ./run.sh || true
    info "Marked ./run.sh as executable"
  fi
}

# =========================
# Main
# =========================
info "Starting combined setup"

require_cmd bash
if ! detect_debian_like; then
  warn "Non-Debian/Ubuntu system detected (no apt-get). Serial step may still work; Redis install will not."
fi

if [[ "${SETUP_SERIAL}" == "1" ]]; then
  setup_serial
else
  info "Skipping serial setup (SETUP_SERIAL=${SETUP_SERIAL})"
fi

if [[ "${SETUP_REDIS}" == "1" ]]; then
  setup_redis
else
  info "Skipping Redis setup (SETUP_REDIS=${SETUP_REDIS})"
fi

maybe_mark_run_executable

info "Setup complete."
