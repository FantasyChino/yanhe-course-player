"""Synthetic online relay tests: no real credentials, sockets, DNS, or disk I/O."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from email.message import Message
import io
import json
import re
import socket
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from online_service import OnlineError, OnlineService, ProxyBody


AUTH = "a" * 32  # Deliberately synthetic; never read a user's auth file.
NOW = 1700000000
MEDIA_ROOT = "https://cvideo.yanhekt.cn/vod/fixture/Video1/Video1.m3u8"
SCREEN_ROOT = "https://cvideo.yanhekt.cn/vod/fixture/VGA/VGA.m3u8"
MASTER = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio/index.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100000,AUDIO="audio"
levels/main.m3u8
'''
PLAYLIST = b'''#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-KEY:METHOD=AES-128,URI="keys/key.bin"
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4,
seg1.ts
#EXT-X-ENDLIST
'''


class Clock:
    value = 0.0

    def __call__(self):
        return self.value

    def wall(self):
        return NOW + self.value


class Response(io.BytesIO):
    def __init__(self, url, body=b"", status=200, headers=None):
        super().__init__(body)
        self.url = url
        self.status = status
        self.headers = Message()
        self.read_sizes = []
        for key, value in (headers or {}).items():
            self.headers[key] = str(value)

    def geturl(self):
        return self.url

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("An upstream read must be bounded")
        self.read_sizes.append(size)
        return super().read(size)


class FakeOpener:
    def __init__(self, clock):
        self.clock = clock
        self.requests = []
        self.responses = []
        self.token_calls = 0
        self.search_calls = 0
        self.media_calls = 0
        self.cdn_failures = 0
        self.always_deny = False
        self.search_unauthorized = False
        self.user_data = {"id": 999999, "private_user_field": "omitted"}
        self.redirect = None
        self.final_url = None
        self.v2_empty = False
        self.main_url = MEDIA_ROOT
        self.screen_url = SCREEN_ROOT
        self.token_started = None
        self.release_token = None
        self.lock = threading.Lock()

    def response(self, request, body=b"", status=200, headers=None):
        result = Response(self.final_url or request.full_url, body, status, headers)
        self.responses.append(result)
        return result

    def envelope(self, request, data, status=200):
        return self.response(request, json.dumps({"code": 0, "data": data}).encode(), status,
                             {"Content-Type": "application/json"})

    @staticmethod
    def course_row():
        return {"id": 123, "name_zh": "Synthetic course", "professors": [{"name": "Teacher"}],
                "school_year": "2025-2026", "semester": 1, "college_name": "College",
                "internal_fixture_field": "must not reach the frontend"}

    def open(self, request, timeout=20):
        parsed = urlsplit(request.full_url)
        with self.lock:
            self.requests.append(request)
        if parsed.hostname == "cbiz.yanhekt.cn":
            if parsed.path == "/v1/user":
                return self.envelope(request, self.user_data)
            if parsed.path == "/v1/auth/video/token":
                with self.lock:
                    self.token_calls += 1
                    number = self.token_calls
                if self.token_started is not None:
                    self.token_started.set()
                    if not self.release_token.wait(2):
                        raise AssertionError("Synthetic token gate timed out")
                return self.envelope(request, {"token": f"synthetic-short-{number}", "now": self.clock.wall(),
                                                "expired_at": self.clock.wall() + 600})
            if parsed.path == "/v2/course/list":
                self.search_calls += 1
                if self.search_unauthorized:
                    return self.response(request, b"synthetic auth failure", 401)
                page = int(parse_qs(parsed.query)["page"][0])
                return self.envelope(request, {"current_page": page, "last_page": 2, "total": 21,
                                                "data": [self.course_row()]})
            if parsed.path == "/v1/course":
                return self.envelope(request, self.course_row())
            if parsed.path in ("/v1/course/session/list", "/v2/course/session/list"):
                if parsed.path.startswith("/v2/") and self.v2_empty:
                    return self.envelope(request, [])
                return self.envelope(request, [{"id": 456, "title": "Lesson", "started_at": "2025-01-01 09:00",
                    "videos": [{"id": 789, "main": self.main_url, "vga": self.screen_url, "duration": "100"}]}])
            raise AssertionError("Unexpected synthetic API route")
        if parsed.hostname != "cvideo.yanhekt.cn":
            raise AssertionError("An unapproved destination reached the opener")
        self.media_calls += 1
        if self.redirect:
            return self.response(request, status=302, headers={"Location": self.redirect})
        if self.always_deny or self.cdn_failures:
            self.cdn_failures = max(0, self.cdn_failures - 1)
            return self.response(request, b"synthetic access failure", 401)
        if parsed.path.endswith("Video1.m3u8"):
            return self.response(request, MASTER, headers={"Content-Type": "application/vnd.apple.mpegurl"})
        if parsed.path.endswith(".m3u8"):
            return self.response(request, PLAYLIST, headers={"Content-Type": "application/vnd.apple.mpegurl"})
        payload = b"0123456789abcdef" if parsed.path.endswith("key.bin") else b"0123456789"
        headers = {"Content-Type": "application/octet-stream", "Content-Length": len(payload), "Accept-Ranges": "bytes"}
        requested = request.get_header("Range")
        status = 200
        if requested == "bytes=2-5":
            headers["Content-Range"] = f"bytes 2-5/{len(payload)}"
            payload = payload[2:6]
            headers["Content-Length"] = len(payload)
            status = 206
        return self.response(request, payload, status, headers)


class OnlineServiceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket.socket", "socket.getaddrinfo", "builtins.open", "io.open",
                     "pathlib.Path.write_text", "pathlib.Path.write_bytes", "pathlib.Path.mkdir"):
            self.stack.enter_context(mock.patch(name, side_effect=AssertionError("Test forbids network and disk I/O")))
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.errors))
        self.clock = Clock()
        self.opener = FakeOpener(self.clock)
        self.service = OnlineService(opener=self.opener, clock=self.clock, wall_clock=self.clock.wall)
        self.service.set_auth(AUTH)

    def tearDown(self):
        self.assertEqual(self.output.getvalue(), "", "The relay must not log upstream details")
        self.assertEqual(self.errors.getvalue(), "", "The relay must not log upstream details")

    @staticmethod
    def resource(descriptor):
        value = descriptor["url"]
        if not re.fullmatch(r"/online-media/[A-Za-z0-9_-]{20,80}", value):
            raise AssertionError("Frontend descriptor must use an opaque same-origin resource")
        return value.rsplit("/", 1)[1]

    @staticmethod
    def references(body):
        return re.findall(r"/online-media/([A-Za-z0-9_-]{20,80})", body.decode())

    def session(self):
        return self.service.session("123", "456")

    def media_playlist(self):
        session = self.session()
        status, _, body = self.service.proxy(self.resource(session["camera"]))
        self.assertEqual(status, 200)
        refs = self.references(body)
        self.assertEqual(len(refs), 2)
        _, _, media = self.service.proxy(refs[-1])
        return media

    def test_search_pagination_and_sanitized_course_schema(self):
        first = self.service.search("Synthetic", 1)
        second = self.service.search("Synthetic", 2)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual(second["page"], 2)
        self.assertEqual(set(first["courses"][0]), {"id", "name", "teacher", "term", "school"})
        self.assertEqual(first["courses"][0]["id"], "123")
        self.assertEqual(first["courses"][0]["teacher"], "Teacher")
        search = [r for r in self.opener.requests if urlsplit(r.full_url).path == "/v2/course/list"]
        self.assertEqual(parse_qs(urlsplit(search[1].full_url).query)["page_size"], ["20"])
        self.assertEqual(parse_qs(urlsplit(search[1].full_url).query)["keyword"], ["Synthetic"])
        self.assertNotIn("private_user_field", json.dumps(first))

    def test_course_fallback_schema_and_session_opaque_descriptors(self):
        self.opener.v2_empty = True
        course = self.service.course("123")
        self.assertEqual(course["name"], "Synthetic course")
        self.assertEqual(course["lessons"], [{"id": "456", "index": 1, "title": "Lesson", "camera": True,
                                               "screen": True, "duration": 100.0}])
        self.assertTrue(any(urlsplit(r.full_url).path == "/v1/course/session/list" for r in self.opener.requests))
        result = self.session()
        for role in ("camera", "screen"):
            self.resource(result[role])
            self.assertEqual(result[role]["kind"], "hls")
        frontend = json.dumps(result)
        self.assertNotIn("https://", frontend)
        self.assertNotIn(AUTH, frontend)
        self.assertNotIn("synthetic-short", frontend)
        self.assertNotIn("internal_fixture_field", frontend)

    def test_nested_hls_key_map_media_rewrite_and_bearer_scope(self):
        body = self.media_playlist()
        self.assertIn(b"#EXT-X-KEY:", body)
        self.assertIn(b"#EXT-X-MAP:", body)
        refs = self.references(body)
        self.assertEqual(len(refs), 3)
        self.assertNotIn(b"https://", body)
        self.assertNotIn(b"synthetic-short", body)
        for resource in refs:
            status, _, stream = self.service.proxy(resource)
            self.assertEqual(status, 200)
            self.assertIsInstance(stream, ProxyBody)
            try:
                self.assertTrue(b"".join(stream))
            finally:
                stream.close()
        for request in self.opener.requests:
            if urlsplit(request.full_url).hostname == "cvideo.yanhekt.cn":
                self.assertIsNone(request.get_header("Authorization"))
                self.assertNotIn(AUTH, repr(request.header_items()))
            else:
                self.assertEqual(request.get_header("Authorization"), "Bearer " + AUTH)
        self.assertTrue(all(response.closed for response in self.opener.responses))

    def test_video_token_refresh_window_and_cdn_retry_only_once(self):
        resource = self.resource(self.session()["camera"])
        self.service.proxy(resource)
        self.assertEqual(self.opener.token_calls, 1)
        self.clock.value = 539
        self.service.proxy(resource)
        self.assertEqual(self.opener.token_calls, 1)
        self.clock.value = 541
        self.service.proxy(resource)
        self.assertEqual(self.opener.token_calls, 2)
        self.opener.cdn_failures = 1
        before = self.opener.media_calls
        self.service.proxy(resource)
        self.assertEqual(self.opener.media_calls - before, 2)
        self.assertEqual(self.opener.token_calls, 3)
        self.opener.always_deny = True
        before = self.opener.media_calls
        with self.assertRaises(OnlineError) as error:
            self.service.proxy(resource)
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.opener.media_calls - before, 2)
        self.assertNotIn("https://", str(error.exception))

    def test_api_login_expiry_is_not_retried_and_invalidates_resources(self):
        resource = self.resource(self.session()["camera"])
        self.opener.search_unauthorized = True
        with self.assertRaises(OnlineError) as error:
            self.service.search("Expired")
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(self.opener.search_calls, 1)
        self.assertFalse(self.service.status()["authenticated"])
        with self.assertRaises(OnlineError):
            self.service.proxy(resource)

    def test_empty_user_data_does_not_authenticate_or_mint_video_token(self):
        for invalid_user in (None, {}, [], ["not a user"], ""):
            with self.subTest(user_type=type(invalid_user).__name__):
                self.opener.user_data = invalid_user
                self.service.set_auth(AUTH)
                with self.assertRaises(OnlineError) as error:
                    self.service.status(check=True)
                self.assertEqual(error.exception.status, 401)
                self.assertFalse(self.service.status()["authenticated"])
                self.assertEqual(self.opener.token_calls, 0)
                self.assertEqual(urlsplit(self.opener.requests[-1].full_url).path, "/v1/user")

    def test_bad_sources_unregistered_paths_and_redirects_are_rejected(self):
        for bad in ("http://cvideo.yanhekt.cn/fixture.m3u8", "https://outside.invalid/fixture.m3u8",
                    "https://user:password@cvideo.yanhekt.cn/fixture.m3u8", "https://cvideo.yanhekt.cn:444/fixture.m3u8",
                    "https://cvideo.yanhekt.cn/fixture.m3u8#fragment", "https://cvideo.yanhekt.cn/\x00bad.m3u8"):
            with self.subTest(kind="bad source"):
                opener = FakeOpener(self.clock)
                opener.main_url = opener.screen_url = bad
                service = OnlineService(opener=opener, clock=self.clock, wall_clock=self.clock.wall)
                service.set_auth(AUTH)
                self.assertEqual(service.course("123")["lessons"], [])
        with self.assertRaises(OnlineError) as error:
            self.service.proxy("../arbitrary-path")
        self.assertEqual(error.exception.status, 404)
        resource = self.resource(self.session()["camera"])
        self.opener.redirect = "https://outside.invalid/private"
        before = self.opener.media_calls
        with self.assertRaises(OnlineError) as error:
            self.service.proxy(resource)
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.opener.media_calls - before, 1)
        self.assertTrue(self.opener.responses[-1].closed)
        self.opener.redirect = None
        self.assertEqual(self.service.proxy(resource)[0], 200)

    def test_range_head_bounded_stream_and_explicit_close(self):
        refs = self.references(self.media_playlist())
        segment = refs[-1]
        status, headers, stream = self.service.proxy(segment, "bytes=2-5")
        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        response = self.opener.responses[-1]
        try:
            self.assertEqual(next(stream), b"2345")
        finally:
            stream.close()
        self.assertTrue(response.closed)
        self.assertTrue(all(0 < size <= 256 * 1024 for size in response.read_sizes))
        status, headers, body = self.service.proxy(segment, "bytes=2-5", head_only=True)
        self.assertEqual((status, body), (206, b""))
        self.assertEqual(self.opener.requests[-1].get_method(), "HEAD")
        self.assertEqual(self.opener.responses[-1].read_sizes, [])
        with self.assertRaises(OnlineError) as error:
            self.service.proxy(segment, "bytes=0-1,3-4")
        self.assertEqual(error.exception.status, 416)

    def test_clear_auth_invalidates_resources_and_inflight_stream(self):
        refs = self.references(self.media_playlist())
        _, _, stream = self.service.proxy(refs[-1])
        response = self.opener.responses[-1]
        result = self.service.clear_auth()
        self.assertFalse(result["configured"])
        self.assertEqual(list(stream), [])
        self.assertTrue(response.closed)
        count = len(self.opener.requests)
        with self.assertRaises(OnlineError) as error:
            self.service.proxy(refs[-1])
        self.assertEqual(error.exception.status, 401)
        self.assertEqual(len(self.opener.requests), count)

    def test_concurrent_status_checks_singleflight_short_token(self):
        started, release = threading.Event(), threading.Event()
        self.opener.token_started, self.opener.release_token = started, release
        barrier = threading.Barrier(4)
        def check():
            barrier.wait(timeout=2)
            return self.service.status(check=True)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(check) for _ in range(4)]
            self.assertTrue(started.wait(2))
            release.set()
            statuses = [future.result(timeout=3) for future in futures]
        self.assertEqual(self.opener.token_calls, 1)
        self.assertTrue(all(status["authenticated"] for status in statuses))
        self.assertNotIn(AUTH, json.dumps(statuses))
        self.assertNotIn("synthetic-short", json.dumps(statuses))


if __name__ == "__main__":
    unittest.main()
