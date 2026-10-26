"""Portable server tests: temporary bytes only, with all socket creation forbidden."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import tempfile
import unittest
from unittest import mock

import local_server as player


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="course-player-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config_path = self.root / "config" / "courses.json"
        self.data_root = self.root / "videos"
        self.config_path.parent.mkdir()
        (self.root / "dist").mkdir()
        (self.root / "dist" / "index.html").write_text("<!doctype html><p>fixture</p>", encoding="utf-8")
        self.default_course = {"id": "course-A_2", "name": "Synthetic course", "directory": "sample"}
        self.write_config([self.default_course])
        guard = mock.patch("socket.socket", side_effect=AssertionError("Network is forbidden in these tests"))
        guard.start()
        self.addCleanup(guard.stop)

    def write_config(self, courses, *, path=None, data_root="../videos"):
        selected = path or self.config_path
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text(json.dumps({"data_root": data_root, "courses": courses}, ensure_ascii=False), encoding="utf-8")
        return selected

    def video(self, folder, name="Lecture 1.mp4", content=None):
        directory = self.data_root / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(content if content is not None else b"\x00\x00\x00\x18ftypmp42" + bytes(range(128)))
        return path

    def library(self, cache_seconds=0):
        return player.CourseLibrary(player.load_config(self.root), cache_seconds)

    def dispatch(self, path, *, method="GET", headers=None, library=None):
        handler = object.__new__(player.PlayerHandler)
        handler.server = SimpleNamespace(
            server_port=8767, static_root=self.root / "dist", library=library or self.library(),
            instance=player.project_instance(self.root),
        )
        handler.headers = Message()
        handler.headers["Host"] = "127.0.0.1:8767"
        for key, value in (headers or {}).items():
            if key in handler.headers:
                handler.headers.replace_header(key, value)
            else:
                handler.headers[key] = value
        handler.path = path
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.close_connection = False
        handler.wfile = io.BytesIO()
        handler._dispatch(method == "HEAD")
        head, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
        status_line, raw_headers = head.split(b"\r\n", 1)
        return int(status_line.split()[1]), BytesParser().parsebytes(raw_headers + b"\r\n\r\n"), body

    def test_default_example_fallback_and_missing_directories(self):
        self.config_path.unlink()
        example = self.write_config([self.default_course], path=self.config_path.with_name("courses.example.json"))
        config = player.load_config(self.root)
        self.assertEqual(config.path, example)
        self.assertEqual(config.data_root, self.data_root)
        self.assertEqual(json.loads(player.CourseLibrary(config).snapshot())["courses"][0]["lessons"], [])

    def test_private_default_config_precedes_example(self):
        self.write_config([{"id": "example", "name": "Example", "directory": "unused"}],
                          path=self.config_path.with_name("courses.example.json"))
        self.assertEqual(player.load_config(self.root).courses[0].id, "course-A_2")

    def test_custom_config_paths_and_upstream_siblings_without_directory(self):
        custom = self.root / "settings" / "custom.json"
        self.write_config([{"id": "upstream", "name": "Upstream", "camera_directory": "course-video",
                            "screen_directory": "course-screen"}], path=custom, data_root="../recordings")
        config = player.load_config(self.root, custom)
        self.assertEqual(config.data_root, self.root / "recordings")
        self.assertEqual(config.courses[0].camera, self.root / "recordings" / "course-video")
        self.assertEqual(config.courses[0].screen, self.root / "recordings" / "course-screen")
        self.assertEqual(config.courses[0].teacher, "")

    def test_config_errors_are_explicit(self):
        with self.assertRaisesRegex(player.ConfigError, "配置"):
            player.load_config(self.root, self.root / "missing.json")
        self.config_path.write_text("{invalid", encoding="utf-8")
        with self.assertRaisesRegex(player.ConfigError, "配置"):
            player.load_config(self.root)
        invalid = [None, [], {}, {"data_root": "../videos", "courses": "wrong"},
                   {"data_root": "", "courses": []}]
        for value in invalid:
            with self.subTest(value=value):
                self.config_path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(player.ConfigError):
                    player.load_config(self.root)

    def test_course_schema_and_duplicate_ids(self):
        invalid = [None, {}, {"id": "bad/id", "name": "Name", "directory": "sample"},
                   {"id": "ok", "name": "", "directory": "sample"},
                   {"id": "ok", "name": "Name", "teacher": 1, "directory": "sample"},
                   {"id": "ok", "name": "Name", "camera_directory": "camera"}]
        for course in invalid:
            with self.subTest(course=course):
                self.write_config([course])
                with self.assertRaises(player.ConfigError):
                    player.load_config(self.root)
        self.write_config([self.default_course, self.default_course])
        with self.assertRaisesRegex(player.ConfigError, "重复"):
            player.load_config(self.root)

    def test_directory_traversal_absolute_and_drive_relative_are_rejected(self):
        for value in ("../../outside", str(self.root / "outside"), "C:\\outside", "C:outside", "\\\\host\\share"):
            with self.subTest(value=value):
                self.write_config([{**self.default_course, "camera_directory": value}])
                with self.assertRaises(player.ConfigError):
                    player.load_config(self.root)

    def test_ordinary_names_pair_by_stem_and_natural_sort(self):
        for number in (10, 2, 1):
            self.video("sample", f"Lecture {number}.MP4")
            self.video("sample/电脑屏幕", f"Lecture {number}.mp4")
        library = self.library()
        lessons = json.loads(library.snapshot())["courses"][0]["lessons"]
        self.assertEqual([lesson["title"] for lesson in lessons], ["Lecture 1", "Lecture 2", "Lecture 10"])
        self.assertEqual([lesson["index"] for lesson in lessons], [1, 2, 3])
        self.assertTrue(all(lesson["camera"] and lesson["screen"] for lesson in lessons))
        original_ids = {lesson["title"]: lesson["id"] for lesson in lessons}
        self.video("sample", "Lecture 0.mp4")
        after = json.loads(library.snapshot())["courses"][0]["lessons"]
        self.assertEqual({lesson["title"]: lesson["id"] for lesson in after if lesson["title"] in original_ids}, original_ids)
        self.assertTrue(all(lesson["id"].startswith("file-") for lesson in after))

    def test_explicit_sibling_directories_pair_case_insensitive_stems(self):
        self.write_config([{"id": "COURSE_a-1", "name": "Sibling", "camera_directory": "a-video",
                            "screen_directory": "a-screen"}])
        self.video("a-video", "Chapter 2.mp4")
        self.video("a-screen", "CHAPTER 2.MP4")
        self.video("a-screen", "Chapter 3.mp4")
        lessons = json.loads(self.library().snapshot())["courses"][0]["lessons"]
        self.assertEqual(len(lessons), 2)
        self.assertIsNotNone(lessons[0]["camera"])
        self.assertIsNotNone(lessons[0]["screen"])
        self.assertIsNone(lessons[1]["camera"])
        self.assertIn("/media/COURSE_a-1/", lessons[0]["camera"]["url"])

    def test_standard_filename_retains_id_index_title_and_verified_duration(self):
        filename = "07_课程标题_12345_part2.mp4"
        camera = self.video("sample", filename)
        self.video("sample/电脑屏幕", filename)
        camera.with_suffix(".verified.json").write_text(
            json.dumps({"bytes": camera.stat().st_size, "duration_seconds": 42.5}), encoding="utf-8")
        lesson = json.loads(self.library().snapshot())["courses"][0]["lessons"][0]
        self.assertEqual((lesson["id"], lesson["index"], lesson["title"]), ("12345-part2", 7, "课程标题 · 第 2 段"))
        self.assertEqual(lesson["camera"]["duration"], 42.5)
        self.assertIsInstance(lesson["camera"]["lastModified"], int)
        self.assertEqual(set(lesson), {"id", "index", "title", "camera", "screen"})

    def test_only_nonempty_direct_mp4_files_are_indexed(self):
        self.video("sample", "empty.mp4", b"")
        self.video("sample", "notes.txt")
        self.video("sample/nested", "hidden.mp4")
        self.video("sample", "visible.mp4")
        lessons = json.loads(self.library().snapshot())["courses"][0]["lessons"]
        self.assertEqual([lesson["title"] for lesson in lessons], ["visible"])

    def test_media_lookup_is_whitelisted_not_a_disk_path(self):
        self.video("sample")
        library = self.library()
        lesson = json.loads(library.snapshot())["courses"][0]["lessons"][0]
        self.assertIsNotNone(library.media("course-A_2", lesson["id"], "camera"))
        self.assertIsNone(library.media("not-configured", lesson["id"], "camera"))
        self.assertIsNone(library.media("course-A_2", "../private", "camera"))
        self.assertIsNone(library.media("course-A_2", lesson["id"], "screen"))

    def test_config_edits_reload_even_during_long_cache_window(self):
        library = self.library(cache_seconds=3600)
        self.assertEqual(json.loads(library.snapshot())["courses"][0]["id"], "course-A_2")
        self.write_config([{"id": "new-course", "name": "New", "directory": "other"}])
        self.assertEqual(json.loads(library.snapshot())["courses"][0]["id"], "new-course")

    def test_creating_private_config_switches_away_from_example(self):
        self.config_path.unlink()
        self.write_config([self.default_course], path=self.config_path.with_name("courses.example.json"))
        library = self.library(cache_seconds=3600)
        library.snapshot()
        self.write_config([{"id": "custom-course", "name": "New", "directory": "other"}])
        self.assertEqual(json.loads(library.snapshot())["courses"][0]["id"], "custom-course")
        self.config_path.unlink()
        self.assertEqual(json.loads(library.snapshot())["courses"][0]["id"], "course-A_2")

    def test_invalid_live_config_never_serves_stale_catalog_or_media_and_recovers(self):
        self.video("sample")
        library = self.library(cache_seconds=3600)
        lesson = json.loads(library.snapshot())["courses"][0]["lessons"][0]
        self.config_path.write_text("{bad", encoding="utf-8")
        with redirect_stderr(io.StringIO()) as errors:
            status, _, body = self.dispatch("/api/library", library=library)
            self.assertEqual(status, 500)
            self.assertNotIn(b"course-A_2", body)
            self.assertNotIn(str(self.root).encode(), body)
            media_status, _, media_body = self.dispatch(lesson["camera"]["url"], library=library)
            self.assertEqual(media_status, 500)
            self.assertNotIn(b"ftypmp42", media_body)
        self.assertIn("配置", errors.getvalue())
        self.write_config([self.default_course])
        self.assertEqual(self.dispatch("/api/library", library=library)[0], 200)

    def test_deleted_live_config_cannot_reuse_old_whitelist(self):
        self.video("sample")
        library = self.library(cache_seconds=3600)
        lesson = json.loads(library.snapshot())["courses"][0]["lessons"][0]
        self.config_path.unlink()
        with self.assertRaises(player.ConfigError):
            library.media("course-A_2", lesson["id"], "camera")

    def test_symlink_escape_is_rejected_by_config_and_file_lookup(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "escape.mp4").write_bytes(b"synthetic outside fixture")
        self.data_root.mkdir()
        link = self.data_root / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symlinks unavailable: {error}")
        self.write_config([{**self.default_course, "directory": "linked"}])
        with self.assertRaises(player.ConfigError):
            player.load_config(self.root)
        self.assertIsNone(player.safe_file(link / "escape.mp4", self.data_root))

    def test_reparse_point_is_rejected_without_platform_privileges(self):
        candidate = self.video("sample")
        original = Path.lstat
        folder = candidate.parent
        def lstat(path):
            if path == folder:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original(path)
        with mock.patch.object(Path, "lstat", lstat):
            self.assertIsNone(player.safe_file(candidate, self.data_root))
            with self.assertRaises(player.ConfigError):
                player.load_config(self.root)

    def test_static_traversal_and_unapproved_extensions_are_blocked(self):
        (self.root / "dist" / "private.txt").write_bytes(b"temporary private fixture")
        for url in ("/auth.txt", "/private.txt", "/../local_server.py", "/%2e%2e/local_server.py",
                    "/%5c..%5clocal_server.py", "/media/unknown/fake/camera", "/config/courses.json"):
            with self.subTest(url=url):
                self.assertEqual(self.dispatch(url)[0], 404)
        self.assertEqual(self.dispatch("/")[0], 200)

    def test_host_origin_and_cross_site_protections(self):
        for headers in ({"Host": "evil.test:8767"}, {"Origin": "http://evil.test:8767"},
                        {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.dispatch("/api/health", headers=headers)[0], 403)
        self.assertEqual(self.dispatch("/api/health", headers={"Host": "localhost:8767", "Origin": "http://localhost:8767"})[0], 200)

    def test_range_head_and_if_range_use_only_requested_fixture_bytes(self):
        path = self.video("sample")
        library = self.library()
        lesson = json.loads(library.snapshot())["courses"][0]["lessons"][0]
        url = lesson["camera"]["url"]
        status, headers, body = self.dispatch(url, headers={"Range": "bytes=10-19"}, library=library)
        self.assertEqual((status, body), (206, path.read_bytes()[10:20]))
        self.assertEqual(headers["Content-Range"], f"bytes 10-19/{path.stat().st_size}")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        status, headers, body = self.dispatch(url, method="HEAD", headers={"Range": "bytes=-4"}, library=library)
        self.assertEqual((status, body, headers["Content-Length"]), (206, b"", "4"))
        status, _, body = self.dispatch(url, headers={"Range": "bytes=10-19", "If-Range": '"stale"'}, library=library)
        self.assertEqual((status, body), (200, path.read_bytes()))
        _, original_headers, _ = self.dispatch(url, method="HEAD", library=library)
        status, _, body = self.dispatch(url, headers={"Range": "bytes=10-19", "If-Range": original_headers["ETag"]}, library=library)
        self.assertEqual((status, body), (206, path.read_bytes()[10:20]))
        for value in ("bytes=99999-", "bytes=10-1", "bytes=0-1,5-6", "bytes=-0"):
            with self.subTest(value=value):
                status, headers, body = self.dispatch(url, headers={"Range": value}, library=library)
                self.assertEqual((status, body), (416, b""))
                self.assertEqual(headers["Content-Range"], f"bytes */{path.stat().st_size}")

    def test_health_instance_is_specific_to_project_root(self):
        status, headers, body = self.dispatch("/api/health")
        self.assertEqual(status, 200)
        health = json.loads(body)
        expected = hashlib.sha256(str(self.root.resolve()).lower().encode("utf-8")).hexdigest()[:16]
        self.assertEqual(health["instance"], expected)
        self.assertNotEqual(expected, player.project_instance(self.root / "another-clone"))
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_server_initialization_uses_loopback_config_and_port_without_binding(self):
        with mock.patch.object(player.ThreadingHTTPServer, "__init__", return_value=None) as initialize:
            server = player.PlayerServer(8767, project_root=self.root)
        initialize.assert_called_once_with(("127.0.0.1", 8767), player.PlayerHandler)
        self.assertEqual(server.instance, player.project_instance(self.root))
        self.assertEqual(server.config.path, self.config_path)

    def test_cli_accepts_config_and_defaults_to_public_port_without_network(self):
        fake = SimpleNamespace(server_port=8767, serve_forever=mock.Mock(side_effect=KeyboardInterrupt), server_close=mock.Mock())
        with mock.patch.object(player, "PROJECT_ROOT", self.root), mock.patch.object(player, "PlayerServer", return_value=fake) as create:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(player.main(["--config", str(self.config_path)]), 0)
        create.assert_called_once_with(8767, project_root=self.root, config_path=self.config_path)
        fake.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
