"""Quick Share QR code fallback and the fixed default port, on loopback."""

import asyncio
import logging
import socket

import pytest

from multidrop.backends.quickshare import QuickShareBackend, frames
from multidrop.backends.quickshare.qr import URL_PREFIX, QrScan, QrSender, terminal_qr
from multidrop.cli import _pick
from multidrop.config import Config
from multidrop.core import Device


def test_url_and_derived_keys_match():
    sender = QrSender()
    assert sender.url.startswith(URL_PREFIX)
    assert len(sender.key_data) == 35 and sender.key_data[2] in (2, 3)
    scan = QrScan(sender.url)
    assert scan.advertising_token == sender.advertising_token
    assert scan.name_key == sender.name_key
    with pytest.raises(ValueError):
        QrScan("https://example.org/#key=AAAA")


def test_match_visible_and_hidden_device():
    sender = QrSender()
    scan = QrScan(sender.url)
    visible = frames.EndpointInfo("Pixel 9", frames.DEVICE_PHONE,
                                  qr_data=scan.tlv_payload(None))
    hidden = frames.EndpointInfo(None, frames.DEVICE_PHONE, visible=False,
                                 qr_data=scan.tlv_payload("Galaxy S25"))
    for info, name in ((visible, "Pixel 9"), (hidden, "Galaxy S25")):
        parsed = frames.EndpointInfo.parse(info.serialize())
        assert sender.match(parsed) == name
    other = QrSender()
    assert other.match(frames.EndpointInfo.parse(visible.serialize())) is None
    assert other.match(frames.EndpointInfo.parse(hidden.serialize())) is None
    assert sender.match(frames.EndpointInfo("plain")) is None


def test_endpoint_info_tlv_records():
    # A vendor TLV (Samsung) before the QR record must be skipped.
    raw = frames.EndpointInfo("S", frames.DEVICE_PHONE).serialize()
    raw += bytes([frames.TLV_VENDOR, 1, 1, frames.TLV_QR_CODE, 3]) + b"abc"
    assert frames.EndpointInfo.parse(raw).qr_data == b"abc"
    # Truncated record is ignored, not an error.
    assert frames.EndpointInfo.parse(raw[:-1]).qr_data is None


def test_signature_roundtrip():
    sender = QrSender()
    scan = QrScan(sender.url)
    sig = sender.sign(b"auth" * 8)
    assert len(sig) == 64
    assert scan.verify(b"auth" * 8, sig)
    assert not scan.verify(b"other" * 8, sig)
    assert not scan.verify(b"auth" * 8, sig[:63])
    assert not QrScan(QrSender().url).verify(b"auth" * 8, sig)


def test_terminal_qr():
    text = terminal_qr(QrSender().url)
    assert len(text.splitlines()) > 10


async def test_send_via_qr_skips_prompt(make_config, tmp_path):
    """The 'phone' starts advertising only after it scanned the code, rejects
    every prompt, and still gets the file because the sender proves the QR key."""
    pc_cfg, phone_cfg = make_config("pc"), make_config("phone")
    for cfg in (pc_cfg, phone_cfg):
        cfg.quickshare.bind = "127.0.0.1"
    pc, phone = QuickShareBackend(pc_cfg), QuickShareBackend(phone_cfg)
    events, phone_events = [], []
    asked = []

    async def reject(offer):
        asked.append(offer)
        return set()

    def on_event(event, d):
        events.append((event, d))
        if event == "qr":   # "scan" the code
            phone.qr_scan = QrScan(d["url"])
            asyncio.get_running_loop().create_task(
                phone.start(reject, lambda e, x: phone_events.append((e, x))))

    f = tmp_path / "foto.jpg"
    f.write_bytes(b"jpeg" * 1000)
    try:
        device = await pc.send_qr([f], on_event, timeout=15)
    finally:
        await phone.stop()
    assert device.name == "phone"
    assert ("qr_verified" in [e for e, _ in phone_events]) and not asked
    assert (phone_cfg.download_dir / "foto.jpg").read_bytes() == f.read_bytes()


async def test_send_qr_times_out(make_config):
    from multidrop.core import TransferError
    pc = QuickShareBackend(make_config("pc"))
    with pytest.raises(TransferError, match="nobody scanned"):
        await pc.send_qr([], timeout=1.0, text="hi")


async def test_wrong_qr_key_still_asks(make_config, tmp_path):
    """A sender with another key is not trusted: the normal prompt decides."""
    cfg_a, cfg_b = make_config("a"), make_config("b")
    for cfg in (cfg_a, cfg_b):
        cfg.quickshare.bind = "127.0.0.1"
        cfg.quickshare.mdns = False
    b = QuickShareBackend(cfg_b)
    b.qr_scan = QrScan(QrSender().url)
    asked = []

    async def reject(offer):
        asked.append(offer)
        return set()

    await b.start(reject, lambda e, d: None)
    try:
        from multidrop.backends.quickshare import outbound
        from multidrop.core import TransferError
        with pytest.raises(TransferError):
            await outbound.send("127.0.0.1", b.port, our_name="a", text="hi",
                                on_event=lambda e, d: None, qr=QrSender())
    finally:
        await b.stop()
    assert asked


async def test_fixed_port_and_fallback(make_config, caplog):
    assert Config().quickshare.port == frames.DEFAULT_PORT == 53318
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        taken = blocker.getsockname()[1]
        cfg = make_config("x")
        cfg.quickshare.bind = "127.0.0.1"
        cfg.quickshare.mdns = False
        cfg.quickshare.port = taken
        b = QuickShareBackend(cfg)
        with caplog.at_level(logging.WARNING):
            await b.start(None, lambda e, d: None)
        try:
            assert b.port not in (taken, 0)
            assert f"port {taken} is in use" in caplog.text
        finally:
            await b.stop()


def test_prefer_quick_share_by_default():
    both = [Device("localsend", "1", "Pixel", "10.0.0.2", 53317),
            Device("quickshare", "2", "Pixel", "10.0.0.2", 40000)]
    assert _pick(both, None, Config().prefer).protocol == "quickshare"
    assert _pick(both, None, ["localsend", "quickshare"]).protocol == "localsend"
    cfg = Config.from_dict({"prefer": ["localsend"]})
    assert _pick(both, "pix", cfg.prefer).protocol == "localsend"
