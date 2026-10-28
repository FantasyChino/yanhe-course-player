"""HTTP route tests with FakeOnline and BytesIO; no sockets or credentials."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from email.message import Message
from email.parser import BytesParser
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from local_server import PlayerHandler
from online_service import OnlineError


NONCE = "synthetic_resource_1234567890"
TOKEN = "a" * 32


class Stream:
    def __init__(self, chunks, fail=False):
        self.chunks = chunks
        self.fail = fail
        self.closed = False
        self.iterated = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.iterated += 1
            yield chunk
        if self.fail:
            raise OnlineError(503, "upstream_failed", "Synthetic remote details must not be appended")

    def close(self):
        self.closed = True


class FakeOnline:
    def __init__(self):
        self.calls = []
        self.proxy_body = b"fixture body"
        self.proxy_error = None

    def status(self, check=False):
        self.calls.append(("status", check))
        return {"configured": True, "authenticated": check}

    def search(self, query, page=1):
        self.calls.append(("search", query, page))
        return {"courses": [{"id": "123", "name": "Fixture", "teacher": "Teacher"}], "page": int(page), "has_more": False}

    def course(self, course_id):
        self.calls.append(("course", course_id))
        return {"id": course_id, "name": "Fixture", "lessons": [{"id": "456", "title": "Lesson"}]}

    def session(self, course_id, session_id):
        self.calls.append(("session", course_id, session_id))
        return {"camera": {"url": "/online-media/" + NONCE, "kind": "hls"}, "screen": None}

    def set_auth(self, token):
        self.calls.append(("auth", token))
        return {"configured": True, "authenticated": None}

    def clear_auth(self):
        self.calls.append(("logout",))
        return {"configured": False, "authenticated": False}

    def proxy(self, resource, requested_range=None, head_only=False):
        self.calls.append(("proxy", resource, requested_range, head_only))
        if self.proxy_error:
            raise self.proxy_error
        return 206, {"Content-Type": "video/mp2t", "Content-Length": "12", "Content-Range": "bytes 0-11/20",
                     "Accept-Ranges": "bytes", "Location": "https://fixture.invalid/hidden",
                     "Set-Cookie": "synthetic=hidden"}, self.proxy_body


class DisconnectWriter(io.BytesIO):
    def __init__(self):
        super().__init__()
        self.headers_sent = False

    def write(self, value):
        if self.headers_sent:
            raise BrokenPipeError("Synthetic client disconnect")
        written = super().write(value)
        if b"\r\n\r\n" in value:
            self.headers_sent = True
        return written


class OnlineRouteTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name in ("socket.socket", "socket.getaddrinfo", "builtins.open", "io.open"):
            stack.enter_context(mock.patch(name, side_effect=AssertionError("No network or disk access in route tests")))
        self.logs = io.StringIO()
        stack.enter_context(redirect_stdout(self.logs))
        stack.enter_context(redirect_stderr(self.logs))
        self.online = FakeOnline()

    def tearDown(self):
        self.assertEqual(self.logs.getvalue(), "")

    def request(self, path, method="GET", body=None, headers=None, writer=None):
        handler = object.__new__(PlayerHandler)
        handler.server = SimpleNamespace(server_port=8768, online=self.online, instance="fixture", static_root=Path("unused"))
        handler.headers = Message()
        defaults = {"Host": "127.0.0.1:8768"}
        if method == "POST":
            defaults.update({"Origin": "http://127.0.0.1:8768", "X-Requested-With": "duo-player", "Content-Type": "application/json"})
        raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
        if method == "POST":
            defaults["Content-Length"] = str(len(raw))
        defaults.update(headers or {})
        for key, value in defaults.items():
            if value is not None:
                handler.headers[key] = value
        handler.path, handler.command = path, method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.close_connection = False
        handler.rfile = io.BytesIO(raw)
        handler.wfile = writer if writer is not None else io.BytesIO()
        if method == "POST":
            handler.do_POST()
        else:
            handler._dispatch(method == "HEAD")
        head, content = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        status, raw_headers = head.split(b"\r\n", 1)
        parsed = BytesParser().parsebytes(raw_headers + b"\r\n\r\n")
        return int(status.split()[1]), parsed, content, handler

    def test_get_status_search_course_and_session_are_forwarded(self):
        status, _, body, _ = self.request("/api/online/status?check=1")
        self.assertEqual((status, json.loads(body)), (200, {"configured": True, "authenticated": True}))
        status, _, body, _ = self.request("/api/online/search?q=Synthetic&page=2")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["page"], 2)
        self.assertIn(("search", "Synthetic", "2"), self.online.calls)
        self.assertEqual(json.loads(self.request("/api/online/course?id=123")[2])["id"], "123")
        session = json.loads(self.request("/api/online/session?course_id=123&session_id=456")[2])
        self.assertTrue(session["camera"]["url"].startswith("/online-media/"))
        self.assertIn(("session", "123", "456"), self.online.calls)

    def test_same_origin_auth_and_logout_never_reflect_the_token(self):
        status, _, body, _ = self.request("/api/online/auth", "POST", {"token": TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["configured"])
        self.assertNotIn(TOKEN.encode(), body)
        self.assertIn(("auth", TOKEN), self.online.calls)
        status, _, body, _ = self.request("/api/online/logout", "POST", {})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["configured"])
        self.assertIn(("logout",), self.online.calls)

    def test_cross_origin_or_missing_requested_with_cannot_change_auth(self):
        for headers in ({"Origin": "https://outside.invalid"}, {"Host": "outside.invalid:8768"},
                        {"X-Requested-With": None}, {"X-Requested-With": "wrong"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                status, _, body, _ = self.request("/api/online/auth", "POST", {"token": TOKEN}, headers)
                self.assertEqual(status, 403)
                self.assertNotIn(TOKEN.encode(), body)
        self.assertEqual(self.online.calls, [])

    def test_auth_requires_json_object_and_bounded_content_length(self):
        for headers, body in (({"Content-Type": "text/plain"}, {"token": TOKEN}),
                              ({"Content-Length": "0"}, {}), ({"Content-Length": "-1"}, {}),
                              ({"Content-Length": "16385"}, {}), ({"Content-Length": "invalid"}, {}),
                              ({}, []), ({}, b"{invalid"), ({}, b"\xff")):
            with self.subTest(headers=headers, body_type=type(body).__name__):
                status, _, content, _ = self.request("/api/online/auth", "POST", body, headers)
                self.assertEqual(status, 400)
                self.assertNotIn(TOKEN.encode(), content)
        self.assertEqual(self.online.calls, [])

    def test_proxy_bytes_and_head_filter_upstream_headers(self):
        status, headers, body, _ = self.request("/online-media/" + NONCE, headers={"Range": "bytes=0-11"})
        self.assertEqual((status, body), (206, b"fixture body"))
        self.assertEqual(headers["Content-Range"], "bytes 0-11/20")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIsNone(headers.get("Location"))
        self.assertIsNone(headers.get("Set-Cookie"))
        self.assertIn(("proxy", NONCE, "bytes=0-11", False), self.online.calls)
        stream = Stream([b"not read"])
        self.online.proxy_body = stream
        status, _, body, _ = self.request("/online-media/" + NONCE, "HEAD")
        self.assertEqual((status, body), (206, b""))
        self.assertEqual(stream.iterated, 0)
        self.assertTrue(stream.closed)
        self.assertIn(("proxy", NONCE, None, True), self.online.calls)

    def test_iterator_closes_on_success_disconnect_and_midstream_failure(self):
        for fail, writer in ((False, None), (False, DisconnectWriter()), (True, None)):
            with self.subTest(fail=fail, disconnected=writer is not None):
                stream = Stream([b"first", b"second"], fail=fail)
                self.online.proxy_body = stream
                status, _, body, handler = self.request("/online-media/" + NONCE, writer=writer)
                self.assertEqual(status, 206)
                self.assertTrue(stream.closed)
                self.assertNotIn(b"Synthetic remote", body)
                if fail or writer is not None:
                    self.assertTrue(handler.close_connection)

    def test_invalid_paths_and_upstream_exceptions_do_not_expose_urls(self):
        self.assertEqual(self.request("/online-media/%2e%2e/private")[0], 404)
        self.assertEqual(self.request("/online-media/short")[0], 404)
        self.online.proxy_error = ValueError("Synthetic detail at https://fixture.invalid/private")
        status, _, body, _ = self.request("/online-media/" + NONCE)
        self.assertEqual(status, 400)
        self.assertNotIn(b"https://", body)
        self.assertNotIn(b"fixture.invalid", body)


if __name__ == "__main__":
    unittest.main()
