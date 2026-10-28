#!/bin/bash
# Install multidrop as a desktop app for the current user (no root): window,
# top bar icon, launcher, autostart, "send with multidrop" in the file manager.
# Works on Ubuntu/Debian, Fedora, Arch/Manjaro, openSUSE and others with
# GTK 4 + libadwaita; missing system packages are listed with the command for
# this distribution (run those yourself, this script never uses sudo).
#
#   tools/install-desktop.sh            install
#   tools/install-desktop.sh --pin      install and add to the GNOME dock
#   tools/install-desktop.sh --remove   uninstall (also unpins)
#   NO_AIRPLAY=1 tools/install-desktop.sh   skip fetching doubletake (screen mirroring)

set -euo pipefail

ID=org.multidrop.Multidrop
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DATA=${XDG_DATA_HOME:-$HOME/.local/share}
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}
APPS=$DATA/applications
ICONS=$DATA/icons/hicolor
AUTOSTART=$CONFIG/autostart
NAUTILUS="$DATA/nautilus/scripts/Send with MultiDrop"
CAJA="$CONFIG/caja/scripts/Send with MultiDrop"
NEMO=$DATA/nemo/actions/multidrop.nemo_action
DOLPHIN=$DATA/kio/servicemenus/multidrop.desktop
PY=/usr/bin/python3
EXEC="$PY $ROOT/src/multidrop/desktop.py --venv $ROOT/.venv"

have() { command -v "$1" >/dev/null 2>&1; }
gnome() { have gsettings && gsettings list-schemas 2>/dev/null | grep -qx org.gnome.shell; }

favorites() { local f; f=$(gsettings get org.gnome.shell favorite-apps 2>/dev/null || echo "[]"); echo "${f#@as }"; }
set_favorites() {  # $1 = add|remove
  gnome || return 0
  local fav; fav=$(favorites)
  gsettings set org.gnome.shell favorite-apps "$($PY -c '
import ast, sys
f = ast.literal_eval(sys.argv[1]); op, app = sys.argv[2], sys.argv[3]
if op == "add" and app not in f: f.append(app)
if op == "remove" and app in f: f.remove(app)
print(f)' "$fav" "$1" "$ID.desktop")"
}

if [ "${1:-}" = --remove ]; then
  set_favorites remove
  rm -f "$APPS/$ID.desktop" "$AUTOSTART/$ID.desktop" "$NAUTILUS" "$CAJA" "$NEMO" "$DOLPHIN" \
        "$ICONS/scalable/apps/$ID.svg" "$ICONS/symbolic/apps/$ID-symbolic.svg"
  echo "removed"
  exit 0
fi

# --- distribution and system packages ---------------------------------------
os=$( . /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}")
family=other
for id in $os; do
  case $id in
    debian|ubuntu|linuxmint|pop|elementary|zorin) family=debian; break ;;
    fedora|rhel|centos|nobara|ultramarine) family=fedora; break ;;
    arch|manjaro|endeavouros|cachyos|garuda) family=arch; break ;;
    opensuse*|suse|sles) family=suse; break ;;
  esac
done

missing=()
$PY -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null \
  || missing+=(gtk)
$PY -c 'import sys; sys.exit(sys.version_info < (3, 11))' || missing+=(python)
$PY -c 'import venv, ensurepip' 2>/dev/null || missing+=(venv)
have nmcli || missing+=(nm)
have pkexec || missing+=(polkit)

pkgs() {  # what -> package names for this family
  case $family:$1 in
    debian:gtk) echo "python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" ;;
    debian:venv) echo "python3-venv" ;;
    debian:nm) echo "network-manager" ;;
    debian:polkit) echo "pkexec" ;;
    fedora:gtk) echo "python3-gobject gtk4 libadwaita" ;;
    fedora:nm) echo "NetworkManager" ;;
    fedora:polkit) echo "polkit" ;;
    arch:gtk) echo "python-gobject gtk4 libadwaita" ;;
    arch:nm) echo "networkmanager" ;;
    arch:polkit) echo "polkit" ;;
    suse:gtk) echo "python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1" ;;
    suse:nm) echo "NetworkManager" ;;
    suse:polkit) echo "polkit" ;;
    *:python) echo "python3 (3.11 or newer)" ;;
    *) echo "" ;;
  esac
}
if [ ${#missing[@]} -gt 0 ]; then
  list=""
  for m in "${missing[@]}"; do list+=" $(pkgs "$m")"; done
  case $family in
    debian) cmd="sudo apt install$list" ;;
    fedora) cmd="sudo dnf install$list" ;;
    arch) cmd="sudo pacman -S --needed$list" ;;
    suse) cmd="sudo zypper install$list" ;;
    *) cmd="install:$list (GTK 4, libadwaita, PyGObject, NetworkManager, polkit)" ;;
  esac
  echo "missing system packages (${missing[*]}). Run:"
  echo "  $cmd"
  # The app itself needs GTK and Python; nmcli/pkexec only for AirDrop.
  case " ${missing[*]} " in *" gtk "*|*" python "*|*" venv "*) exit 1 ;; esac
fi

# --- venv with multidrop ------------------------------------------------------
if [ ! -x "$ROOT/.venv/bin/multidropd" ]; then
  echo "creating $ROOT/.venv"
  $PY -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -e "$ROOT"
fi

# --- launcher, autostart, icons ------------------------------------------------
mkdir -p "$APPS" "$ICONS/scalable/apps" "$ICONS/symbolic/apps" "$AUTOSTART"
install -m 644 "$ROOT/data/icons/multidrop.svg" "$ICONS/scalable/apps/$ID.svg"
install -m 644 "$ROOT/data/icons/multidrop-symbolic.svg" "$ICONS/symbolic/apps/$ID-symbolic.svg"

entry() {  # $1 = extra Exec arguments
  cat <<DESKTOP
[Desktop Entry]
Type=Application
Name=MultiDrop
GenericName=Nearby sharing
Comment=AirDrop, Quick Share, LocalSend and AirPlay on Linux
Exec=$EXEC $1
Icon=$ID
Terminal=false
Categories=Network;FileTransfer;
Keywords=AirDrop;AirPlay;Quick Share;Nearby;LocalSend;share;send;
MimeType=application/octet-stream;inode/directory;
StartupNotify=true
StartupWMClass=$ID
DESKTOP
}
entry "%F" > "$APPS/$ID.desktop"
{ entry "--background"; echo "X-GNOME-Autostart-enabled=true"; echo "NoDisplay=true"; } \
  | grep -v '^MimeType=' > "$AUTOSTART/$ID.desktop"

# --- "send with multidrop" in file managers -----------------------------------
script() {  # path
  mkdir -p "$(dirname "$1")"
  printf '#!/bin/sh\nexec %s "$@"\n' "$EXEC" > "$1"
  chmod +x "$1"
}
script "$NAUTILUS"                                   # Nautilus (GNOME)
have caja && script "$CAJA"                          # Caja (MATE)
mkdir -p "$(dirname "$NEMO")" "$(dirname "$DOLPHIN")"
cat > "$NEMO" <<NEMO
[Nemo Action]
Name=Send with MultiDrop
Comment=Send to a nearby device (AirDrop, Quick Share, LocalSend)
Exec=$EXEC %F
Icon-Name=$ID
Selection=notnone
Extensions=any;
NEMO
cat > "$DOLPHIN" <<DOLPHIN
[Desktop Entry]
Type=Service
MimeType=application/octet-stream;inode/directory;
X-KDE-ServiceTypes=KonqPopupMenu/Plugin
Actions=send

[Desktop Action send]
Name=Send with MultiDrop
Icon=$ID
Exec=$EXEC %F
DOLPHIN
chmod +x "$DOLPHIN"   # KDE only runs executable service menus

have update-desktop-database && update-desktop-database "$APPS" 2>/dev/null || true
have gtk-update-icon-cache && gtk-update-icon-cache -q -t "$ICONS" 2>/dev/null || true
have gtk4-update-icon-cache && gtk4-update-icon-cache -q -t "$ICONS" 2>/dev/null || true
echo "installed: launcher, autostart, icons, file manager action ($family)"

# --- hints ------------------------------------------------------------------
if gnome && have gnome-extensions; then
  if ! gnome-extensions list --enabled 2>/dev/null | grep -qiE 'appindicator'; then
    echo "note: for the top bar icon on GNOME install and enable the extension"
    echo "      'AppIndicator and KStatusNotifierItem Support'"
    case $family in
      debian) echo "      (sudo apt install gnome-shell-extension-appindicator)" ;;
      fedora) echo "      (sudo dnf install gnome-shell-extension-appindicator)" ;;
      arch) echo "      (sudo pacman -S gnome-shell-extension-appindicator)" ;;
      suse) echo "      (sudo zypper install gnome-shell-extension-appindicator)" ;;
    esac
    echo "      Without it the window works, closing it ends the app."
  fi
fi
if [ ! -x /usr/lib/omdrop/omdrop-discoverable ]; then
  echo "note: AirDrop needs omdrop-owl (root install, see docs/INSTALL-ROOT.md)."
  case $family in
    debian) echo "      tools/build-omdrop-owl.sh, then: sudo apt install ./build/airdrop/omdrop-owl_*.deb" ;;
    *) echo "      Build from https://github.com/t4t5/omdrop-owl (its README)." ;;
  esac
  echo "      Quick Share and LocalSend work without it."
fi

# AirPlay screen mirroring engine (download ~10 MB, checksum pinned).
if [ ! -x "$HOME/.local/lib/multidrop/doubletake/doubletake" ] && ! have doubletake; then
  if [ "${NO_AIRPLAY:-}" = 1 ]; then
    echo "note: screen mirroring needs doubletake: tools/fetch-doubletake.sh"
  else
    "$ROOT/tools/fetch-doubletake.sh" || echo "note: doubletake download failed; screen mirroring off"
  fi
fi

if [ "${1:-}" = --pin ]; then
  if gnome; then set_favorites add; echo "pinned to the dock"
  else echo "pin it yourself: right-click the launcher in your panel/dock"; fi
fi
