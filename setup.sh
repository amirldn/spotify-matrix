#!/usr/bin/env bash
#
# setup.sh — one-shot setup for Spotify Matrix on a Raspberry Pi
# with the Adafruit RGB Matrix Bonnet (tested target: Pi Zero 2 W, 32x32 panel).
#
# Run as your normal user (NOT root) from the project directory:
#     bash setup.sh
#
# It calls sudo only where required. It is idempotent — safe to re-run; steps
# that are already done are skipped. The Adafruit bindings installer is
# interactive and reboots at the end.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this as your normal user (e.g. nova), not root — it sudo's where needed." >&2
  exit 1
fi

echo "==> Spotify Matrix setup (dir: $PROJECT_DIR)"

# ---------------------------------------------------------------------------
# 1. System build dependencies for the rgbmatrix C bindings.
# ---------------------------------------------------------------------------
echo "==> [1/5] Installing system packages (apt)…"
sudo apt-get update
sudo apt-get install -y \
  git python3-venv python3-dev python3-pip python3-pillow \
  cython3 python3-setuptools cmake unzip curl

# ---------------------------------------------------------------------------
# 2. Project virtualenv (inherits system-wide rgbmatrix via system-site-packages).
# ---------------------------------------------------------------------------
if [[ ! -d .venv ]]; then
  echo "==> [2/5] Creating virtualenv (.venv, --system-site-packages)…"
  python3 -m venv .venv --system-site-packages
else
  echo "==> [2/5] .venv already exists — reusing."
fi
echo "==> Installing Python requirements into .venv…"
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# ---------------------------------------------------------------------------
# 3. Local Spotify credentials file.
# ---------------------------------------------------------------------------
if [[ ! -f .env ]]; then
  echo "==> [3/5] Creating .env from template — EDIT IT with your Spotify credentials."
  cp .env.example .env
else
  echo "==> [3/5] .env already present — leaving it alone."
fi

# ---------------------------------------------------------------------------
# 4. rgbmatrix bindings.
#
# The current Adafruit installer (rgb-matrix.py) builds rgbmatrix into its OWN
# private virtualenv (~/Raspberry-Pi-Installer-Scripts/env), NOT system-wide —
# so our --system-site-packages venv does NOT inherit it. We therefore build it
# once with the Adafruit installer, then copy the compiled package into our
# .venv (same interpreter version + arch + OS = ABI-compatible, no rebuild).
#
# Flow (idempotent, survives the installer's reboot):
#   a) already in .venv           -> done
#   b) built in Adafruit env      -> just copy it into .venv
#   c) not built yet              -> run installer (reboots); re-run setup.sh after
# ---------------------------------------------------------------------------
ADAFRUIT_ENV="$HOME/Raspberry-Pi-Installer-Scripts/env"
PYVER="$(.venv/bin/python -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")')"
ADAFRUIT_SITE="$ADAFRUIT_ENV/lib/$PYVER/site-packages"
VENV_SITE=".venv/lib/$PYVER/site-packages"

if .venv/bin/python -c "import rgbmatrix" 2>/dev/null; then
  echo "==> [4/5] rgbmatrix already in .venv — nothing to do."
elif compgen -G "$ADAFRUIT_SITE/rgbmatrix*" > /dev/null; then
  echo "==> [4/5] rgbmatrix found in Adafruit env — copying into .venv (same $PYVER ABI)…"
  cp -r "$ADAFRUIT_SITE"/rgbmatrix* "$VENV_SITE/"
  .venv/bin/python -c "import rgbmatrix; print('rgbmatrix OK in .venv')"
else
  # Optional swap bump: the C build is memory-hungry and can hang on <1GB RAM.
  total_ram_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  if [[ "${total_ram_kb:-0}" -lt 1048576 ]]; then
    read -rp "    Low RAM (<1GB) detected. Bump swap to 1024MB for the build? [y/N] " ans || ans=""
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      # Portable: add a disk-backed swapfile (works on zram- or dphys-based images).
      # Non-persistent by design — only needed for this build.
      if [[ ! -e /swapfile ]]; then
        echo "==> Adding a 1GB /swapfile for the build…"
        sudo fallocate -l 1G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=1024
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
        sudo swapon /swapfile
      else
        echo "==> /swapfile already exists — ensuring it is enabled."
        sudo swapon /swapfile || true
      fi
    fi
  fi

  echo "==> [4/5] Installing adafruit-python-shell (system, --break-system-packages)…"
  sudo pip3 install --break-system-packages --upgrade adafruit-python-shell

  INSTALLER_DIR="$HOME/Raspberry-Pi-Installer-Scripts"
  if [[ ! -d "$INSTALLER_DIR" ]]; then
    git clone https://github.com/adafruit/Raspberry-Pi-Installer-Scripts.git "$INSTALLER_DIR"
  fi

  echo "==> Running Adafruit rgb-matrix.py installer (interactive)…"
  echo "    Answer:  Interface board = Adafruit RGB Matrix Bonnet"
  echo "             Tradeoff        = convenience (keeps sound; matches --no-hardware-pulse)"
  echo "             CPU isolation   = optional (steadier display if reserved)"
  echo "    It will offer to REBOOT at the end — say yes."
  echo
  echo "    IMPORTANT: after the reboot, re-run 'bash setup.sh' — it will copy the"
  echo "    freshly-built rgbmatrix bindings from the Adafruit env into this project's"
  echo "    .venv (the installer isolates them in its own venv, so this step is required)."
  ( cd "$INSTALLER_DIR" && sudo python3 rgb-matrix.py )
fi

# ---------------------------------------------------------------------------
# 5. Done.
# ---------------------------------------------------------------------------
echo "==> [5/5] Setup complete."
echo
echo "After the reboot, verify the bindings and test the panel:"
echo "  cd $PROJECT_DIR"
echo "  .venv/bin/python -c 'import rgbmatrix; print(\"rgbmatrix OK\")'"
echo "  sudo -E .venv/bin/python spotify_matrix.py \\"
echo "    --rows 32 --cols 32 --gpio-slowdown 4 \\"
echo "    --no-hardware-pulse --hardware-mapping adafruit-hat --test-pattern"
echo
echo "Then fill in .env and authorize Spotify:"
echo "  (laptop) ssh -L 8888:127.0.0.1:8888 $USER@$(hostname).local"
echo "  (pi)     sudo -E .venv/bin/python spotify_matrix.py --auth-only --no-browser"
