#!/usr/bin/env bash
# OmniLab installer — container-first (architecture rev 5).
#
#   curl -fsSL https://raw.githubusercontent.com/dhworg/omnilab/main/install.sh | bash
#
# Installs the `omnilab` CLI onto an existing Linux distribution. This
# script NEVER partitions, formats, or otherwise touches your disk layout,
# and never installs a kernel driver.
#
# It DOES install missing prerequisites for you (git, podman, python venv
# support, nvidia-container-toolkit) using your distro's package manager,
# printing each command before it runs. Set OMNILAB_NO_SUDO=1 to have the
# commands printed for you to run yourself instead.
#
# Env overrides:
#   OMNILAB_REF=<branch|tag>   install from a different ref (default: main)
#   OMNILAB_REPO=<url>         install from a fork
#   OMNILAB_NO_TUI=1           skip the textual extra
#   OMNILAB_ASSUME_YES=1       don't prompt (for CI)
#   OMNILAB_NO_SUDO=1          never run sudo; print the commands instead

set -euo pipefail

OMNILAB_REPO="${OMNILAB_REPO:-https://github.com/dhworg/omnilab}"
OMNILAB_REF="${OMNILAB_REF:-main}"
OMNILAB_ASSUME_YES="${OMNILAB_ASSUME_YES:-0}"
OMNILAB_NO_TUI="${OMNILAB_NO_TUI:-0}"
OMNILAB_NO_SUDO="${OMNILAB_NO_SUDO:-0}"

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
#
# NOTE: test /dev/tty, NOT stdin. The documented install path is
# `curl ... | bash`, where stdin is the pipe carrying the script itself and
# `[ -t 0 ]` is therefore always false — which silently auto-declined every
# prompt, including the podman install.
confirm() {
  [ "$OMNILAB_ASSUME_YES" = "1" ] && return 0
  [ -r /dev/tty ] || return 1
  printf '%s [y/N] ' "$1"
  read -r reply </dev/tty || return 1
  case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

have() { command -v "$1" >/dev/null 2>&1; }

# Run a privileged command automatically, announcing it first. Required
# dependencies use this; anything optional still goes through confirm().
# Set OMNILAB_NO_SUDO=1 to be told the command instead of having it run.
auto_run() {
  local desc="$1"; shift
  local cmd="$*"
  [ -n "$cmd" ] || { warn "$desc — no package command known for '$DISTRO'"; return 1; }
  if [ "$OMNILAB_NO_SUDO" = "1" ]; then
    warn "$desc — OMNILAB_NO_SUDO=1, run this yourself:"
    say  "    ${DIM}${cmd}${RST}"
    return 1
  fi
  if ! have sudo && [ "$(id -u)" != "0" ]; then
    warn "$desc — sudo is unavailable; run this as root yourself:"
    say  "    ${DIM}${cmd}${RST}"
    return 1
  fi
  refresh_pkg_index
  say "  ${DIM}${cmd}${RST}"
  # shellcheck disable=SC2086
  eval "$cmd" || { warn "that command failed"; return 1; }
  return 0
}

# Whether a *usable* venv can be created — i.e. one that has pip in it.
#
# Do NOT probe with `python3 -m venv --help`: on Debian-family systems the
# venv module is in the stdlib and answers --help happily, while ensurepip
# lives in the separate python3-venv package. The only honest test is to
# build a throwaway venv and look for pip inside it.
venv_works() {
  local probe rc=1
  probe="$(mktemp -d 2>/dev/null)" || return 1
  if python3 -m venv "$probe/v" >/dev/null 2>&1 && [ -x "$probe/v/bin/pip" ]; then
    rc=0
  fi
  rm -rf "$probe"
  return "$rc"
}

# Install whatever this distro needs for venv+pip to work, then re-verify.
ensure_venv_support() {
  venv_works && return 0

  warn "python3 venv support is incomplete (ensurepip / python3-venv missing)"
  local pyver pkgs=""
  pyver="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
  case "$DISTRO" in
    ubuntu|debian|linuxmint|pop)
      # Debian splits ensurepip out into python3-venv. Lead with the
      # unversioned names: apt aborts the whole transaction if any single
      # package name is unknown, and python3.X-venv is the one most likely
      # to be missing on a given release.
      pkgs="python3-venv python3-pip"
      ;;
    fedora|rhel|centos|rocky|almalinux) pkgs="python3-pip" ;;
    arch|manjaro|endeavouros)           pkgs="python-pip" ;;
    opensuse*|sles)                     pkgs="python3-pip" ;;
  esac

  auto_run "installing python venv support" "$(pkg_install_cmd $pkgs)" || true

  if venv_works; then
    ok "python venv support installed"
    return 0
  fi

  # Version-qualified package may have been the only one that mattered and
  # may have failed alongside a non-existent sibling; try it on its own.
  if [ -n "$pyver" ]; then
    case "$DISTRO" in
      ubuntu|debian|linuxmint|pop)
        auto_run "installing python${pyver}-venv" "$(pkg_install_cmd python${pyver}-venv)" || true
        venv_works && { ok "python venv support installed"; return 0; }
        ;;
    esac
  fi
  return 1
}

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

# Refresh the package index once, lazily — a stale apt cache is the most
# common cause of "Unable to locate package" on an otherwise fine system.
APT_UPDATED=0
refresh_pkg_index() {
  case "$DISTRO" in
    ubuntu|debian|linuxmint|pop)
      [ "$APT_UPDATED" = "1" ] && return 0
      [ "$OMNILAB_NO_SUDO" = "1" ] && return 0
      say "  ${DIM}sudo apt-get update${RST}"
      sudo apt-get update -qq >/dev/null 2>&1 || warn "apt-get update failed; continuing"
      APT_UPDATED=1
      ;;
  esac
  return 0
}

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
  warn "git not found — required to fetch the CLI"
  auto_run "installing git" "$(pkg_install_cmd git)" || MISSING=1
fi

if have podman; then
  ok "podman $(podman --version 2>/dev/null | awk '{print $3}')"
else
  warn "podman not found — required to run project containers"
  auto_run "installing podman" "$(pkg_install_cmd podman)" || MISSING=1
  have podman && ok "podman $(podman --version 2>/dev/null | awk '{print $3}')"
fi

# ---- 2. install the CLI -------------------------------------------------

step "Installing the omnilab CLI"

EXTRAS=""
[ "$OMNILAB_NO_TUI" = "1" ] || EXTRAS="[tui]"

# Use the repo subdirectory syntax so pip fetches only what it needs.
SPEC="git+${OMNILAB_REPO}@${OMNILAB_REF}#subdirectory=${CLI_SUBDIR}"

# Debian, Ubuntu, Pop!_OS, Fedora and Arch all mark the system Python as
# "externally managed" (PEP 668), which makes `pip install --user` fail
# outright. A self-contained venv is the only approach that works
# everywhere without sudo and without touching system packages, so it is
# the primary path; pipx is preferred when already present because it
# manages upgrades for you.
OMNILAB_VENV="${OMNILAB_VENV:-$HOME/.local/share/omnilab/venv}"
USER_LOCAL_BIN="$HOME/.local/bin"

install_with_pipx() {
  have pipx || return 1
  say "  using pipx"
  if [ -n "$EXTRAS" ]; then
    pipx install --force "omnilab${EXTRAS} @ ${SPEC}" 2>/dev/null \
      || pipx install --force "${SPEC}"
  else
    pipx install --force "${SPEC}"
  fi
}

install_with_venv() {
  say "  using a managed venv at $OMNILAB_VENV"

  ensure_venv_support || {
    warn "could not get a working python3 venv on this system"
    return 1
  }

  mkdir -p "$(dirname "$OMNILAB_VENV")" || return 1
  rm -rf "$OMNILAB_VENV"
  python3 -m venv "$OMNILAB_VENV" || return 1
  "$OMNILAB_VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  "$OMNILAB_VENV/bin/python" -m pip install --quiet "omnilab${EXTRAS} @ ${SPEC}" 2>/dev/null \
    || "$OMNILAB_VENV/bin/python" -m pip install --quiet "${SPEC}" || return 1

  # Expose a single entry point on PATH rather than asking the user to
  # activate anything.
  mkdir -p "$USER_LOCAL_BIN"
  ln -sf "$OMNILAB_VENV/bin/omnilab" "$USER_LOCAL_BIN/omnilab"
  return 0
}

install_with_pip() {
  say "  using pip --user"
  python3 -m pip install --user --upgrade "omnilab${EXTRAS} @ ${SPEC}" 2>/dev/null \
    || python3 -m pip install --user --upgrade "${SPEC}"
}

if install_with_pipx; then
  ok "installed via pipx"
elif install_with_venv; then
  ok "installed into $OMNILAB_VENV (linked as $USER_LOCAL_BIN/omnilab)"
elif install_with_pip; then
  ok "installed via pip --user"
else
  die "installation failed. Install python3-venv and re-run, or do it manually:
   python3 -m venv ~/.local/share/omnilab/venv
   ~/.local/share/omnilab/venv/bin/pip install '${SPEC}'
   ln -sf ~/.local/share/omnilab/venv/bin/omnilab ~/.local/bin/omnilab"
fi

# ---- 3. PATH ------------------------------------------------------------

USER_BIN="$(python3 -c 'import site,os;print(os.path.join(site.USER_BASE,"bin"))')"
if ! have omnilab; then
  FOUND_IN=""
  for candidate in "$USER_LOCAL_BIN" "$USER_BIN"; do
    if [ -x "$candidate/omnilab" ]; then FOUND_IN="$candidate"; break; fi
  done
  if [ -n "$FOUND_IN" ]; then
    warn "$FOUND_IN is not on your PATH"
    say  "  Add this to your shell profile (~/.bashrc or ~/.zshrc):"
    say  "    ${DIM}export PATH=\"\$PATH:$FOUND_IN\"${RST}"
    export PATH="$PATH:$FOUND_IN"
  else
    die "omnilab installed but not found on PATH — check the output above."
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
    # NOT in most distros' default repos: Debian-family and Fedora need
    # NVIDIA's own repository added first, or apt/dnf just says "unable
    # to locate package". Arch carries it in 'extra'.
    case "$DISTRO" in
      ubuntu|debian|linuxmint|pop)
        NCT_CMD="curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null && sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit" ;;
      fedora|rhel|centos|rocky|almalinux)
        NCT_CMD="sudo dnf config-manager --add-repo https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo && sudo dnf install -y nvidia-container-toolkit" ;;
      arch|manjaro|endeavouros)
        NCT_CMD="sudo pacman -S --noconfirm nvidia-container-toolkit" ;;
      *)
        NCT_CMD="" ;;
    esac
    auto_run "installing nvidia-container-toolkit (adds NVIDIA's repo where needed)" "$NCT_CMD" || true
    if have nvidia-ctk; then
      ok "nvidia-container-toolkit installed"
      # podman resolves nvidia.com/gpu=all through the CDI spec.
      auto_run "generating the CDI spec" "sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml" || true
    fi
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
