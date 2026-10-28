# What needs root

MultiDrop itself runs without root. Only these one-time steps need
administrator rights.

## AirDrop (Linux)

AirDrop uses the radio helper from [omdrop-owl](https://github.com/t4t5/omdrop-owl)
(OWL: Apple's AWDL in software). It runs through polkit (`pkexec`) and restores
Wi-Fi itself after every session.

Debian/Ubuntu – build the package without root, then install it:

```bash
tools/build-omdrop-owl.sh
sudo apt install ./build/airdrop/omdrop-owl_0.1.0-1_amd64.deb   # pulls in iw, libpcap, libnl
```

Other distributions: build and install omdrop-owl from source following its
README (helper at `/usr/lib/omdrop/omdrop-discoverable`).

Check (no root): `multidrop airdrop check`. Supported Wi-Fi cards and channels
are listed in omdrop-owl's hardware profiles (e.g. MediaTek MT7925 in "shared"
mode, Intel in "exclusive" mode).

## System packages for the desktop app

`tools/install-desktop.sh` lists missing packages with the right command, e.g.
for Debian/Ubuntu:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 network-manager pkexec
sudo apt install gnome-shell-extension-appindicator          # top bar icon on GNOME
# screen mirroring (doubletake):
sudo apt install gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
     gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav \
     gstreamer1.0-pipewire pulseaudio-utils
```

## Firewall

With a firewall enabled, allow these on the local network:

| Port | Protocol | Purpose |
|---|---|---|
| 5353/udp | mDNS | finding devices (Quick Share, AirDrop, AirPlay) |
| 53317/tcp+udp | LocalSend | send, receive, discovery |
| 53318/tcp | Quick Share | receive |
| 53320/tcp | browser download | only while `multidrop share` runs |

Example with `ufw`: `sudo ufw allow from 192.168.0.0/16 to any port 53317:53318 proto tcp`
(and likewise for the other rows). On Windows the firewall asks when
MultiDrop.exe starts for the first time – allow "Private networks".
