"""AirDrop's HTTP/1.1 peers can omit Host (observed on the real iPhone).

Use aiohttp's strict Python parser on this listener only, supplying a local
authority after header validation. Framing, duplicate-header checks and
body parsing remain aiohttp's responsibility; other listeners are unchanged.
"""

from aiohttp import web
from aiohttp.http_parser import HttpRequestParserPy
from aiohttp.web_protocol import RequestHandler, RequestPayloadError
from multidict import CIMultiDictProxy


class AirDropParser(HttpRequestParserPy):
    def parse_headers(self, lines):
        headers, raw, *rest = super().parse_headers(lines)
        if "Host" not in headers:
            headers = headers.copy()
            headers["Host"] = "airdrop.local"
            headers = CIMultiDictProxy(headers)
            raw = (*raw, (b"Host", b"airdrop.local"))
        return headers, raw, *rest


class AirDropRequestHandler(RequestHandler):
    def __init__(self, manager, *, loop, **kwargs):
        super().__init__(manager, loop=loop, **kwargs)
        options = {}
        if hasattr(self, "_max_msg_queue_size"):
            options["max_msg_queue_size"] = self._max_msg_queue_size
        parser = AirDropParser(
            self, loop, self._read_bufsize,
            max_line_size=self.max_line_size, max_headers=self.max_headers,
            max_field_size=self.max_field_size,
            payload_exception=RequestPayloadError,
            auto_decompress=kwargs.get("auto_decompress", True), **options)
        # aiohttp moved its parser into BaseProtocol in newer releases.
        if hasattr(self, "_request_parser"):
            self._request_parser = parser
        else:
            self._parser = parser


class AirDropHTTPServer(web.Server):
    def __call__(self):
        return AirDropRequestHandler(self, loop=self._loop, **self._kwargs)


class AirDropRunner(web.AppRunner):
    async def _make_server(self):
        server = await super()._make_server()
        return AirDropHTTPServer(
            server.request_handler, request_factory=server.request_factory,
            handler_cancellation=server.handler_cancellation,
            loop=server._loop, **server._kwargs)
