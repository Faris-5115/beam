"""Channel pinning for AirDrop sessions, with a fake nmcli (no Wi-Fi touched)."""

import pytest

from multidrop import wifi_channel as wc
from multidrop.core import TransferError

SCAN = "\n".join([
    r"no:HomeWiFi:AA\:BB\:CC\:00\:00\:B6:2437 MHz:70",
    r"no:HomeWiFi-IOT:AE\:BB\:CC\:00\:00\:B6:2437 MHz:72",
    r"no:HomeWiFi:AA\:BB\:CC\:00\:00\:B7:5300 MHz:80",
    r"yes:HomeWiFi:AA\:BB\:CC\:00\:00\:B8:6375 MHz:90",
])
ACTIVE = "11111111-2222-3333-4444-555555555555:802-11-wireless:wlan0\nx:bridge:docker0\n"


class FakeRun:
    def __init__(self, scan=SCAN, fail=()):
        self.scan, self.fail, self.calls = scan, fail, []

    async def __call__(self, argv, timeout):
        self.calls.append(argv)
        joined = " ".join(argv)
        if any(f in joined for f in self.fail):
            return 1, "Error: nope"
        if "wifi list" in joined:
            return 0, self.scan
        if "--active" in joined:
            return 0, ACTIVE
        return 0, ""


def test_split_terse_unescapes_colons():
    assert wc.split_terse(r"yes:a\:b:9C\:05:2437 MHz:5") == ["yes", "a:b", "9C:05", "2437 MHz", "5"]


def test_choose_same_network_on_airdrop_channel():
    current, target = wc.choose(wc.parse_scan(SCAN))
    assert current.freq == 6375
    assert (target.bssid, target.freq) == ("AA:BB:CC:00:00:B6", 2437)   # not the -IOT network


def test_choose_nothing_to_do_on_good_channel():
    scan = SCAN.replace("yes:", "no:").replace(r"no:HomeWiFi:AA\:BB\:CC\:00\:00\:B6", r"yes:HomeWiFi:AA\:BB\:CC\:00\:00\:B6")
    assert wc.choose(wc.parse_scan(scan))[1] is None


def test_choose_explains_when_no_channel_fits():
    scan = "\n".join(line for line in SCAN.splitlines() if "B6:2437" not in line or "IOT" in line)
    with pytest.raises(TransferError, match="channels 6, 44 or 149"):
        wc.choose(wc.parse_scan(scan))


async def test_prepare_and_restore_sequence():
    run = FakeRun()
    pin = wc.WifiPin(run)
    assert await pin.prepare(10) == "Wi-Fi temporarily on channel 6"
    cmds = [" ".join(c) for c in run.calls]
    timer = next(i for i, c in enumerate(cmds) if c.startswith("systemd-run"))
    clone = next(i for i, c in enumerate(cmds) if "connection clone" in c)
    assert timer < clone, "safety timer must exist before anything changes"
    assert "--on-active=13m" in cmds[timer]
    assert any("802-11-wireless.bssid AA:BB:CC:00:00:B6" in c and "channel 6" in c
               and "band bg" in c and "autoconnect no" in c for c in cmds)
    run.calls.clear()
    await pin.restore()
    cmds = [" ".join(c) for c in run.calls]
    assert cmds[0].endswith("connection up uuid 11111111-2222-3333-4444-555555555555")
    assert any("delete id multidrop-airdrop-session" in c for c in cmds)
    assert any("stop multidrop-wifi-restore.timer" in c for c in cmds)


async def test_failed_switch_restores_and_raises():
    run = FakeRun(fail=("connection up multidrop-airdrop-session",))
    pin = wc.WifiPin(run)
    with pytest.raises(TransferError):
        await pin.prepare(10)
    assert any(" ".join(c).endswith("up uuid 11111111-2222-3333-4444-555555555555")
               for c in run.calls)


async def test_failed_restore_keeps_safety_timer():
    run = FakeRun()
    pin = wc.WifiPin(run)
    await pin.prepare(10)
    run.fail = ("up uuid",)
    run.calls.clear()
    await pin.restore()
    assert not any("stop multidrop-wifi-restore.timer" in " ".join(c) for c in run.calls)


async def test_no_change_when_already_on_channel_6():
    scan = SCAN.replace("yes:HomeWiFi:AA\\:BB\\:CC\\:00\\:00\\:B8", "no:HomeWiFi:AA\\:BB\\:CC\\:00\\:00\\:B8") \
               .replace("no:HomeWiFi:AA\\:BB\\:CC\\:00\\:00\\:B6", "yes:HomeWiFi:AA\\:BB\\:CC\\:00\\:00\\:B6")
    run = FakeRun(scan)
    assert await wc.WifiPin(run).prepare(10) is None
    assert len(run.calls) == 1
