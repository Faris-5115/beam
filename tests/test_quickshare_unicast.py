"""Unicast mDNS answers for phones behind multicast-filtering access points."""

from zeroconf import DNSIncoming, DNSOutgoing, DNSQuestion, const
from zeroconf.asyncio import AsyncServiceInfo

from multidrop.backends.quickshare import frames
from multidrop.backends.quickshare.mdns import UnicastResponder


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


def make_responder():
    instance = frames.mdns_instance_name("AbCd")
    info = AsyncServiceInfo(frames.SERVICE_TYPE, f"{instance}.{frames.SERVICE_TYPE}", port=4321,
                            properties={"n": "BhVj"}, parsed_addresses=["10.0.0.2"],
                            server="multidrop-abcd.local.")
    r = UnicastResponder(info, "10.0.0.2")
    r.transport = FakeTransport()
    return r, info


def query(name: str) -> bytes:
    out = DNSOutgoing(const._FLAGS_QR_QUERY)
    out.add_question(DNSQuestion(name, const._TYPE_PTR, const._CLASS_IN))
    return out.packets()[0]


def test_answers_phone_directly_with_all_records():
    r, info = make_responder()
    r.datagram_received(query(frames.SERVICE_TYPE), ("10.0.0.109", 5353))
    (data, addr), (again, group) = r.transport.sent
    assert addr == ("10.0.0.109", 5353)
    assert group == ("224.0.0.251", 5353) and again == data      # fresh announcement
    msg = DNSIncoming(data)
    assert msg.is_response()
    kinds = {type(a).__name__ for a in msg.answers()}
    assert {"DNSPointer", "DNSService", "DNSText", "DNSAddress"} <= kinds
    srv = next(a for a in msg.answers() if type(a).__name__ == "DNSService")
    assert srv.port == 4321 and srv.name == info.name


def test_ignores_other_services_self_and_floods():
    r, _ = make_responder()
    r.datagram_received(query("_airplay._tcp.local."), ("10.0.0.109", 5353))
    r.datagram_received(query(frames.SERVICE_TYPE), ("10.0.0.2", 5353))       # ourselves
    assert r.transport.sent == []
    r.datagram_received(query(frames.SERVICE_TYPE), ("10.0.0.109", 5353))
    r.datagram_received(query(frames.SERVICE_TYPE), ("10.0.0.109", 5353))     # within 1 s
    assert len(r.transport.sent) == 2       # one unicast answer + one announcement


def legacy_query(name: str, id_: int) -> bytes:
    out = DNSOutgoing(const._FLAGS_QR_QUERY, False, id_)
    out.add_question(DNSQuestion(name, const._TYPE_PTR, const._CLASS_IN))
    return out.packets()[0]


def test_router_gateway_query_gets_legacy_unicast_answer():
    """Seen live: the router (mDNS gateway) asks from a random port for the phone."""
    r, info = make_responder()
    r.datagram_received(legacy_query(frames.SERVICE_TYPE, 9220), ("10.0.0.1", 56211))
    (data, addr), *rest = r.transport.sent
    assert addr == ("10.0.0.1", 56211)
    msg = DNSIncoming(data)
    assert msg.id == 9220
    assert [q.name for q in msg.questions] == [frames.SERVICE_TYPE]
    assert all(a.ttl <= 10 for a in msg.answers())
    assert {type(a).__name__ for a in msg.answers()} >= {"DNSPointer", "DNSService", "DNSText",
                                                         "DNSAddress"}
    assert [a for _, a in rest] == [("10.0.0.1", 5353), ("224.0.0.251", 5353)]
    # every gateway query gets its answer, also within the rate limit
    r.datagram_received(legacy_query(frames.SERVICE_TYPE, 9221), ("10.0.0.1", 56211))
    assert DNSIncoming(r.transport.sent[-1][0]).id == 9221


async def test_publisher_reports_services_heard_between_scans(monkeypatch):
    from multidrop.backends.quickshare import mdns
    pub = mdns.MdnsPublisher(["10.0.0.2"])
    pub._zc = object()
    name = f"{frames.mdns_instance_name('Ph0n')}.{frames.SERVICE_TYPE}"
    old = f"{frames.mdns_instance_name('0ld1')}.{frames.SERVICE_TYPE}"
    pub.heard = {name: mdns.time.monotonic(), old: mdns.time.monotonic() - 1000}

    async def fake_resolve(zc, n, exclude, loopback_ok=False):
        return mdns.FoundService(n[:4], "10.0.0.109", 4000, frames.EndpointInfo("Galaxy"))
    monkeypatch.setattr(mdns, "_resolve", fake_resolve)
    found = await pub.found(None)
    assert [f.info.name for f in found] == ["Galaxy"]      # the stale one is not asked


async def test_loopback_adverts_are_not_peers():
    from zeroconf import IPVersion
    from multidrop.backends.quickshare import mdns

    class Info:
        port = 33575
        properties = {b"n": frames.b64url(frames.EndpointInfo("pc").serialize()).encode()}

        def __init__(self, *a):
            pass

        async def async_request(self, zc, timeout):
            return True

        def parsed_addresses(self, version=IPVersion.V4Only):
            return ["127.0.0.1"]
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(mdns, "AsyncServiceInfo", Info)
    try:
        name = f"{frames.mdns_instance_name('T3st')}.{frames.SERVICE_TYPE}"
        assert await mdns._resolve(type("Z", (), {"zeroconf": None})(), name, None) is None
    finally:
        mp.undo()


async def test_waits_for_a_real_address_and_follows_changes(make_config, monkeypatch):
    """Started while Wi-Fi reconnects (after AirDrop) the daemon announced
    127.0.0.1 and was invisible; now it waits and re-announces on change."""
    from multidrop.backends.quickshare import QuickShareBackend, mdns
    cfg = make_config("pc")
    cfg.quickshare.mdns_addresses = []
    address = {"now": "127.0.0.1"}
    monkeypatch.setattr(mdns, "default_address", lambda: address["now"])
    published = []

    async def fake_publish(self, addresses):
        published.append(addresses)
        self._publisher = type("P", (), {"addresses": addresses,
                                         "stop": staticmethod(lambda: asyncio.sleep(0))})()
    import asyncio
    monkeypatch.setattr(QuickShareBackend, "_publish", fake_publish)
    b = QuickShareBackend(cfg)
    assert b._mdns_addresses() is None
    b._publisher = None
    task = asyncio.ensure_future(b._watch_address(interval=0.01))
    await asyncio.sleep(0.05)
    assert published == []                      # only loopback: stay quiet
    address["now"] = "192.168.1.119"
    await asyncio.sleep(0.05)
    assert published == [["192.168.1.119"]]
    address["now"] = "192.168.1.20"             # another network
    await asyncio.sleep(0.05)
    assert published[-1] == ["192.168.1.20"]
    task.cancel()
