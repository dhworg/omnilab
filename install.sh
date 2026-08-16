#!/usr/bin/env bash
# OmniLab installer — container-first (architecture rev 5).
#
#   curl -fsSL https://raw.githubusercontent.com/dhworg/omnilab/main/install.sh | bash
#
# Installs the `omnilab` CLI onto an existing Linux distribution. This
# script NEVER partitions, formats, or otherwise touches your disk layout,
# and never installs a kernel driver. It reports what's missing and hands
# you the exact command; it asks before running anything with sudo.
#
# Env overrides:
#   OMNILAB_REF=<branch|tag>   install from a different ref (default: main)
#   OMNILAB_REPO=<url>         install from a fork
#   OMNILAB_NO_TUI=1           skip the textual extra
#   OMNILAB_ASSUME_YES=1       don't prompt (for CI)

set -euo pipefail

OMNILAB_REPO="${OMNILAB_REPO:-https://github.com/dhworg/omnilab}"
OMNILAB_REF="${OMNILAB_REF:-main}"
OMNILAB_ASSUME_YES="${OMNILAB_ASSUME_YES:-0}"
OMNILAB_NO_TUI="${OMNILAB_NO_TUI:-0}"

# Subdirectory of the repo containing the Python package.
CLI_SUBDIR="cli/omnilab"

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
if [ ! -t 1 ]; then BOLD=""; DIM=""; RED=""; GRN=""; YLW=""; RST=""; fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }
step() { printf '\n%s==>%s %s%s%s\n' "$BOLD" "$RST" "$BOLD" "$*" "$RST"; }

# Prompt unless we're non-interactive or told to assume yes.
confirm() {
  [ "$OMNILAB_ASSUME_YES" = "1" ] && return 0
  [ -t 0 ] || return 1
  printf '%s [y/N] ' "$1"
  read -r reply </dev/tty || return 1
  case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# ---- 0. refuse to run somewhere this can't work -------------------------

step "Checking platform"

if [ "$(uname -s)" != "Linux" ]; then
  die "OmniLab v1 is Linux-only. \`podman run --network host\` has different
   semantics under a podman machine VM, which breaks DDS discovery and
   therefore \`omnilab pair\`. macOS/Windows are explicit non-goals for v1."
fi
ok "Linux $(uname -r)"

DISTRO="unknown"; DISTRO_NAME="unknown"
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  DISTRO="${ID:-unknown}"
  DISTRO_NAME="${PRETTY_NAME:-$DISTRO}"
fi
ok "$DISTRO_NAME"

# Package-manager install line for a given package, per distro.
pkg_install_cmd() {
  case "$DISTRO" in
    fedora|rhel|centos|rocky|almalinux) echo "sudo dnf install -y $*" ;;
    ubuntu|debian|linuxmint|pop)        echo "sudo apt-get install -y $*" ;;
    arch|manjaro|endeavouros)           echo "sudo pacman -S --noconfirm $*" ;;
    opensuse*|sles)                     echo "sudo zypper install -y $*" ;;
    *)                                  echo "" ;;
  esac
}

# Offer to run a command, but never run sudo silently.
maybe_run() {
  local desc="$1"; shift
  local cmd="$*"
  if [ -z "$cmd" ]; then
    warn "$desc — no package command known for '$DISTRO'; install it manually"
    return 1
  fi
  say "  ${DIM}${cmd}${RST}"
  if confirm "  Run this?"; then
    # shellcheck disable=SC2086
    eval "$cmd" || { warn "command failed — continuing"; return 1; }
    return 0
  fi
  warn "skipped — run it yourself before using omnilab"
  return 1
}

# ---- 1. prerequisites ---------------------------------------------------

step "Checking prerequisites"

MISSING=0

if have python3; then
  PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
    ok "python $PYV"
  else
    die "python $PYV is too old — OmniLab needs 3.11+."
  fi
else
  die "python3 not found. Install it, then re-run this script."
fi

if have git; then
  ok "git"
else
  warn "git not found"
  maybe_run "git is required to fetch the CLI" "$(pkg_install_cmd git)" || MISSING=1
fi

if have podman; then
  ok "podman $(podman --version 2>/dev/null | awk '{print $3}')"
else
  warn "podman not found — required to run project containers"
  maybe_run "install podman" "$(pkg_install_cmd podman)" || MISSING=1
fi

# ---- 2. install the CLI -------------------------------------------------

step "Installing the omnilab CLI"

EXTRAS=""
[ "$OMNILAB_NO_TUI" = "1" ] || EXTRAS="[tui]"

# Use the repo subdirectory syntax so pip fetches only what it needs.
SPEC="git+${OMNILAB_REPO}@${OMNILAB_REF}#subdirectory=${CLI_SUBDIR}"

install_with_pipx() {
  have pipx || return 1
  say "  using pipx"
  # pipx needs the extras attached to the package name, not the URL.
  if [ -n "$EXTRAS" ]; then
    pipx install --force "omnilab${EXTRAS} @ ${SPEC}" 2>/dev/null \
      || pipx install --force "${SPEC}"
  else
    pipx install --force "${SPEC}"
  fi
}

install_with_pip() {
  say "  using pip --user"
  python3 -m pip install --user --upgrade "omnilab${EXTRAS} @ ${SPEC}" 2>/dev/null \
    || python3 -m pip install --user --upgrade "${SPEC}"
}

if install_with_pipx; then
  ok "installed via pipx"
elif install_with_pip; then
  ok "installed via pip --user"
else
  die "installation failed. Try manually:
   python3 -m pip install --user '${SPEC}'"
fi

# ---- 3. PATH ------------------------------------------------------------

USER_BIN="$(python3 -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))')"
if ! have omnilab; then
  if [ -x "$USER_BIN/omnilab" ]; then
    warn "$USER_BIN is not on your PATH"
    say  "  Add this to your shell profile:"
    say  "    ${DIM}export PATH=\"\$PATH:$USER_BIN\"${RST}"
    export PATH="$PATH:$USER_BIN"
  else
    die "omnilab installed but not found on PATH — check the pip output above."
  fi
fi
ok "$(omnilab version 2>/dev/null || echo 'omnilab (version unknown)')"

# ---- 4. udev + groups for micro-ROS boards ------------------------------

step "Hardware access (micro-ROS boards)"

if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
  ok "user '$USER' is in the dialout group"
else
  warn "user '$USER' is not in 'dialout' — USB serial boards will be permission-denied"
  say  "  ${DIM}sudo usermod -aG dialout $USER   # then log out and back in${RST}"
  if confirm "  Add yourself to dialout now?"; then
    sudo usermod -aG dialout "$USER" && ok "added — log out and back in for it to take effect"
  fi
fi

# ---- 5. GPU -------------------------------------------------------------

step "GPU"

if have nvidia-smi; then
  ok "nvidia-smi present"
  if ! have nvidia-ctk; then
    warn "nvidia-container-toolkit not found — containers won't see the GPU"
    maybe_run "install nvidia-container-toolkit" "$(pkg_install_cmd nvidia-container-toolkit)" || true
  else
    ok "nvidia-container-toolkit present"
  fi
  say ""
  say "  Run ${BOLD}omnilab gpu${RST} to diagnose and repair GPU passthrough."
  say "  ${DIM}It checks power state, kernel modules, device nodes, the CDI spec,${RST}"
  say "  ${DIM}and whether rendering actually lands on the dGPU — then fixes what it can.${RST}"
elif [ -d /dev/dri ]; then
  ok "integrated GPU (/dev/dri present) — iGPU baseline works out of the box"
else
  warn "no GPU detected; simulation will fall back to software rendering"
fi

# ---- 6. done ------------------------------------------------------------

step "Done"

if [ "$MISSING" = "1" ]; then
  warn "some prerequisites were skipped — run 'omnilab doctor' to see what's left"
fi

cat <<EOF

  ${BOLD}Next steps${RST}

    omnilab doctor          # verify the host is ready
    omnilab gpu             # diagnose / repair NVIDIA passthrough
    omnilab new my-robot    # scaffold a project
    cd my-robot && omnilab up

  Nothing on your disk layout was modified by this installer.

EOF
