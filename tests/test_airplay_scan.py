from multidrop.airplay_scan import describe, parse_features


def test_parse_features():
    assert parse_features("0x5A7FFFF7,0x1E") == 0x1E_5A7FFFF7
    assert parse_features("0x77") == 0x77
    assert parse_features("nonsense") is None
    assert parse_features("0x1,0x2,0x3") is None


def test_describe_screen_and_audio():
    apple_tv = describe({b"features": b"0x5A7FFFF7,0x1E", b"model": b"AppleTV6,2",
                         b"deviceid": b"56:dc:90:fe:41:bc"})
    assert apple_tv == {"device_id": "56:DC:90:FE:41:BC", "model": "AppleTV6,2",
                        "screen": True, "audio": True}
    sonos = describe({b"features": b"0x445F8A00,0x1C340", b"model": b"Bookshelf"})
    assert (sonos["screen"], sonos["audio"]) == (False, True)
    assert describe({})["screen"] is None
