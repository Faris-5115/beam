# Research: upstream projects and protocols

Collected October 2026. Upstream sources were read as shallow clones; nothing
from them is built into MultiDrop unless noted.

## Overview

| Project | License | Language | Send | Receive | Root / radio changes | Role for MultiDrop |
|---|---|---|---|---|---|---|
| [t4t5/omdrop-owl](https://github.com/t4t5/omdrop-owl) | no LICENSE file; PKGBUILD names GPL-3.0-or-later (OWL), GPL-2.0-only (omdrop-awdl tools), ISC (radiotap) | shell/Python, C patches | yes (via omdrop) | yes (via omdrop) | **yes**: root helper via pkexec/polkit, monitor interface `mon0`, frame injection | AirDrop radio backend, used as installed |
| [jedbillyb/owl](https://github.com/jedbillyb/owl) (fork of seemoo-lab/owl, "airdrop-mt7921") | GPL-3.0 | C | – | – | **yes** (monitor mode, raw injection, netlink) | AWDL implementation, separate program |
| [seemoo-lab/owl](https://github.com/seemoo-lab/owl) | GPL-3.0 | C | – | – | yes | original, reference |
| brentkearney/omdrop-awdl | GPL-2.0-only | C/shell | – | – | yes (brcmfmac) | its radio-independent tools are used by omdrop-owl only |
| [brentkearney/omdrop-plugin](https://github.com/brentkearney/omdrop-plugin) | **MIT** | Python + QML | yes, to "Everyone"; to "Contacts Only" only with an Apple identity extracted from a Mac | yes, "Everyone" and "Contacts Only" | receiver runs as a user on `awdl0` TCP 8771 | basis of the AirDrop backend |
| [seemoo-lab/opendrop](https://github.com/seemoo-lab/opendrop) | GPL-3.0 | Python | yes ("Everyone") | yes | needs AWDL (OWL) | AirDrop protocol reference |
| [Martichou/rquickshare](https://github.com/Martichou/rquickshare) | GPL-3.0 | Rust (Tauri) | yes | yes | **no root**; BLE advertising via BlueZ | Quick Share reference incl. BLE trigger and re-announce |
| [grishka/NearDrop](https://github.com/grishka/NearDrop) | **Unlicense** | Swift | yes (QR code) | yes | no root | `PROTOCOL.md` + `.proto` files |
| [localsend/protocol](https://github.com/localsend/protocol) | specification (app: Apache-2.0) | – | – | – | no root | open REST specification v2.2 |
| [omarroth/doubletake](https://github.com/omarroth/doubletake) | LGPL-3.0 | Go | AirPlay mirroring | – | no root | screen mirroring engine (separate process) |

## AirDrop (iPhone/Mac ↔ Linux)

- AirDrop runs only over **AWDL** (Apple Wireless Direct Link). On Linux that
  means either Broadcom firmware with AWDL (omdrop-awdl) or **OWL** in software:
  monitor interface + frame injection, always with **root** and changes to the
  Wi-Fi radio.
- omdrop-owl modes:
  - **Shared** (Wi-Fi stays connected): tested on **MediaTek MT7925 (mt7925e)**.
    The access point must be on **channel 6, 44 or 149**.
  - **Exclusive**: Wi-Fi is off while visible (Intel cards).
- MT7925 quirk (owl-profiles): *"Without a reconnect, the station stops
  receiving 3–165 s after a monitor interface is deleted."* omdrop-owl reconnects
  on `stop`.
- Speed on MT7925 (omdrop-owl README): send ≈ 490 kB/s, receive ≈ 1.3 MB/s.
- Many networks run on 6 GHz or other channels. MultiDrop's `--switch-channel`
  moves the station to the same network's access point on an AirDrop channel for
  the session (temporary NetworkManager profile, BSSID pinned) and back.
- iPhone side:
  - Since iOS 16.2 "**Everyone for 10 Minutes**"; afterwards the iPhone falls
    back to "Contacts Only". Sending to an iPhone needs that setting, unless a
    real Apple identity extracted from a Mac is used.
  - Receiving from an iPhone works with a self-signed identity; the PC shows up
    under "Other people".
  - iPhones listen only in short intervals; a BLE announcement wakes them.
    opendrop has no BLE trigger, so Apple receivers often do not show up there.

### omdrop-awdl: announcement and wake-up

- **License:** GPL-2.0-only – not combinable with GPL-3.0-or-later. No code is
  taken; MultiDrop implements the protocol facts itself. omdrop-plugin (MIT) and
  opendrop (GPL-3.0) are compatible.
- **mDNS announcement:** unsolicited responses (PTR `_airdrop._tcp.local` →
  `<id>._airdrop._tcp.local`, SRV port 8771, TXT `flags=<n>`, AAAA link-local),
  bursts of 3 copies every 3 s, flags 0x89; sharingd drops nodes without
  PIPELINING (0x04) and MIXED_TYPES (0x08). MultiDrop repeats the same records
  through python-zeroconf's sockets (no raw socket, no root), flags 0x8B.
- **BLE:** Apple Continuity advertisement (company 0x004C, TLV 0x05, 18 bytes).
  A Mac publishes `_airdrop._tcp` only ~1 s after seeing it. Shapes: "os26"
  `40 <3 tag bytes> 00000000 03 <4×2 hash> 00` and the published
  `00×8 01 <4×2 hash> 00` (Celosia/Cunche, PETS 2020). Without an Apple identity
  the hashes are zero → "Everyone" only.
- **Ask form** (macOS/iOS 26): `TransferID`, `TransferType {files: {}}` or
  `{links: {}}`, `Items`, per file `FileSize`, `FileIcon`. Upload:
  `application/x-dvzip` with `TotalBytes` and `TransferID` headers.

## Quick Share (Samsung Quick Share = Google Nearby Share)

- Protocol per NearDrop `PROTOCOL.md`: the receiver is the server. It publishes
  `_FC9F5ED42C8A._tcp` (name = base64 of PCP 0x23, 4-byte endpoint ID, service ID
  FC9F5E, 2 zero bytes; TXT `n` = endpoint info with the device name), then TCP,
  UKEY2 handshake (ECDH P-256), AES-256-CBC + HMAC-SHA256, protobuf frames and a
  **keep-alive every 10 s** (Android drops the connection otherwise).
- **Wi-Fi LAN only**: both devices on the same (V)LAN, without client isolation.
  Wi-Fi Direct, hotspot, Bluetooth transfer and WebRTC via Google are not
  available in any free implementation.
- **Sending PC → Android:** Android publishes its mDNS service only after seeing
  a BLE advertisement (service UUID `FE2C`, service data
  `fc128e0142000000000000000000` + 10 random bytes). Linux can send it (BlueZ, as
  a normal user). **Samsung phones do not react to it** (NearDrop) – the QR code
  (`https://quickshare.google/qrcode#key=…`) is the reliable way there.
- **Receiving Android → PC:** Android sees the PC once its mDNS service runs.
  Observed in the field:
  - a router's mDNS gateway asked for `_FC9F5ED42C8A._tcp` with one-shot
    ("legacy unicast") queries from random ports on behalf of the phone; answers
    must repeat the query id and question (RFC 6762 §6.7);
  - the phone answered such gateway queries about once a minute, so a short scan
    window misses it – MultiDrop keeps a long-lived browser;
  - a service started while Wi-Fi was still reconnecting picked `127.0.0.1` and
    was invisible – MultiDrop now waits for a real address.
  - rquickshare re-announces when an Android device starts sharing; Android can
    miss services announced before its discovery started.
- **No root needed:** the BlueZ D-Bus policy lets normal users use
  `org.bluez.LEAdvertisingManager1`; `RegisterAdvertisement` works without
  changing Powered/Discoverable/Alias.

## LocalSend

- Open REST specification v2.2: UDP multicast `224.0.0.167:53317`, HTTP(S) on
  TCP 53317, `POST /api/localsend/v2/{register,prepare-upload,upload,cancel}`,
  optional `prepare-download`/`download` (browser download without an app).
- HTTPS with a self-signed certificate; fingerprint = SHA-256 over the DER
  certificate, upper-case hex.
- LocalSend 1.18 (Rust core) **requires a client certificate** while browser
  sharing is off – not in the v2.2 specification. MultiDrop presents its own
  certificate and pins the server certificate to the announced fingerprint.
- `localsend-cli` listens only on interface addresses, not 127.0.0.1, and
  announces on all interfaces; MultiDrop prefers the default-route network.

## Interop findings

- **Quick Share vs rquickshare** in both directions: identical PIN on both sides
  – UKEY2, key derivation, SecureMessage, payload layer and PIN algorithm match
  an implementation that works with Android.
- rquickshare sends no chunk at all for 0-byte files; MultiDrop still creates
  the empty file.
- After a receive error MultiDrop sends `CANCEL`, otherwise the sender would see
  the plain disconnect as success.

## Other candidates

- **KDE Connect**: open, Android and iOS apps; on GNOME **GSConnect** exists. A
  re-implementation is not worth it; at most a backend delegating to GSConnect.
- **Wi-Fi Direct** (Quick Share medium upgrade): needs P2P interfaces through
  wpa_supplicant (root) and is not openly documented. Not realistic.
- **Google Cast**: possible via the Cast protocol (pychromecast + an HTTP stream
  of the screen or audio, several seconds of latency); not implemented.
- **AirDrop on Android**: not possible without root and driver changes (AWDL).
