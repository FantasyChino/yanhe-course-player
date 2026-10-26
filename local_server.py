"""Serve the offline player and explicitly configured courses on loopback only.

The media API exposes stable course/session IDs, never user-supplied file paths.
No third-party modules, network requests, credentials, or video copies are used.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import sys
import threading
import time
import unicodedata
from urllib.parse import quote, unquote, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent
APP_ID = "yanhe-duo-player"
APP_VERSION = "1.2"
DEFAULT_PORT = 8767
FILE_PATTERN = re.compile(
    r"^(?P<index>\d+)_(?P<title>.+)_(?P<session>\d+)(?:_part(?P<part>\d+))?\.mp4$",
    re.IGNORECASE,
)
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
MEDIA_PATTERN = re.compile(r"^/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/(camera|screen)$")
STATIC_EXTENSIONS = {".html", ".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def project_instance(project_root: Path) -> str:
    """Match the launcher's UTF-8 SHA-256 of the resolved, lowercase project path."""
    return hashlib.sha256(str(project_root.resolve()).lower().encode("utf-8")).hexdigest()[:16]


INSTANCE_ID = project_instance(PROJECT_ROOT)


class ConfigError(ValueError):
    """A readable error in an explicitly selected course configuration."""


@dataclass(frozen=True)
class CourseDefinition:
    id: str
    name: str
    teacher: str
    camera: Path
    screen: Path


@dataclass(frozen=True)
class LibraryConfig:
    path: Path
    data_root: Path
    courses: tuple[CourseDefinition, ...]
    project_root: Path
    requested_path: Path | None


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def has_unsafe_link(path: Path) -> bool:
    """Check existing ancestors too, even if the final directory does not exist yet."""
    current = absolute_path(path)
    while True:
        try:
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
                return True
        except FileNotFoundError:
            pass
        except OSError:
            return True
        if current.parent == current:
            return False
        current = current.parent


def config_directory(value, data_root: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ConfigError(f"{label} 必须是非空的相对目录。")
    relative = Path(value)
    # Reject drive-relative paths and Windows UNC paths on other platforms as well.
    if relative.is_absolute() or PureWindowsPath(value).drive or PureWindowsPath(value).root:
        raise ConfigError(f"{label} 必须相对于 data_root，不能使用绝对路径。")
    folder = absolute_path(data_root / relative)
    try:
        folder.relative_to(data_root)
    except ValueError as error:
        raise ConfigError(f"{label} 不能位于 data_root 之外。") from error
    if has_unsafe_link(folder):
        raise ConfigError(f"{label} 含符号链接、重解析点或无法安全访问的目录。")
    return folder


def selected_config_path(project_root: Path, config_path: Path | None) -> Path:
    if config_path is None:
        selected = project_root / "config" / "courses.json"
        if not selected.exists():
            selected = selected.with_name("courses.example.json")
    else:
        selected = config_path
    return absolute_path(Path(selected).expanduser())


def load_config(project_root: Path = PROJECT_ROOT, config_path: Path | None = None) -> LibraryConfig:
    selected = selected_config_path(project_root, config_path)
    try:
        data = json.loads(selected.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"无法读取课程配置 {selected}：{error}") from error
    if not isinstance(data, dict):
        raise ConfigError("课程配置顶层必须是 JSON 对象。")
    root_value = data.get("data_root")
    if not isinstance(root_value, str) or not root_value.strip() or "\x00" in root_value:
        raise ConfigError("data_root 必须是非空的目录字符串。")
    root_path = Path(root_value).expanduser()
    data_root = absolute_path(root_path if root_path.is_absolute() else selected.parent / root_path)
    if has_unsafe_link(data_root):
        raise ConfigError("data_root 含符号链接、重解析点或无法安全访问的目录。")
    configured = data.get("courses")
    if not isinstance(configured, list):
        raise ConfigError("courses 必须是课程数组。")
    courses = []
    seen_ids = set()
    for index, course in enumerate(configured, 1):
        label = f"courses[{index}]"
        if not isinstance(course, dict):
            raise ConfigError(f"{label} 必须是课程对象。")
        course_id = course.get("id")
        if not isinstance(course_id, str) or not IDENTIFIER_PATTERN.fullmatch(course_id):
            raise ConfigError(f"{label}.id 只允许字母、数字、下划线和连字符。")
        if course_id in seen_ids:
            raise ConfigError(f"课程 id 重复：{course_id}")
        seen_ids.add(course_id)
        name = course.get("name")
        teacher = course.get("teacher", "")
        if not isinstance(name, str) or not name.strip() or not isinstance(teacher, str):
            raise ConfigError(f"{label}.name 必须是非空字符串，teacher 必须是字符串。")
        directory = course.get("directory")
        camera_value = course.get("camera_directory", directory)
        if "screen_directory" in course:
            screen_value = course["screen_directory"]
        elif isinstance(directory, str) and directory.strip():
            screen_value = str(Path(directory) / "电脑屏幕")
        else:
            screen_value = None
        # Both explicit role directories can be supplied without a common directory.
        camera = config_directory(camera_value, data_root, f"{label}.camera_directory/directory")
        screen = config_directory(screen_value, data_root, f"{label}.screen_directory/directory")
        if directory is not None:
            config_directory(directory, data_root, f"{label}.directory")
        courses.append(CourseDefinition(course_id, name, teacher, camera, screen))
    requested = absolute_path(Path(config_path).expanduser()) if config_path is not None else None
    return LibraryConfig(selected, data_root, tuple(courses), absolute_path(project_root), requested)


def safe_file(path: Path, boundary: Path) -> Path | None:
    """Refuse symlinks/junctions and files outside an explicitly allowed folder."""
    try:
        absolute = absolute_path(path)
        boundary = absolute_path(boundary)
        absolute.relative_to(boundary)
        if has_unsafe_link(absolute):
            return None
        resolved = absolute.resolve(strict=True)
        resolved.relative_to(boundary)
        return resolved if resolved.is_file() else None
    except (OSError, ValueError, RuntimeError):
        return None


@dataclass(frozen=True)
class MediaFile:
    path: Path
    boundary: Path


class CourseLibrary:
    def __init__(self, config: LibraryConfig, cache_seconds: float = 3.0):
        self.config = config
        self.data_root = config.data_root
        self.courses = config.courses
        self.cache_seconds = cache_seconds
        self._lock = threading.RLock()
        self._scanned_at = float("-inf")
        self._body = b""
        self._media: dict[tuple[str, str, str], MediaFile] = {}
        self._signature = None

    def _config_signature(self) -> tuple:
        path = selected_config_path(self.config.project_root, self.config.requested_path)
        try:
            info = path.stat()
        except OSError as error:
            raise ConfigError(f"无法读取课程配置 {path}：{error}") from error
        return path, info.st_mtime_ns, info.st_size

    @staticmethod
    def _metadata(path: Path, boundary: Path, info) -> dict:
        metadata = {}
        record = safe_file(path.with_suffix(".verified.json"), boundary)
        if record is None:
            return metadata
        try:
            if record.stat().st_size > 65536:
                return metadata
            data = json.loads(record.read_text(encoding="utf-8-sig"))
            duration = data.get("duration_seconds")
            if data.get("bytes") == info.st_size and isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
                metadata["duration"] = duration
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return metadata

    def _scan(self):
        courses = []
        media = {}
        for course in self.courses:
            course_id = course.id
            lessons = {}
            for role, folder in (("camera", course.camera), ("screen", course.screen)):
                try:
                    candidates = sorted((item for item in folder.iterdir() if item.suffix.lower() == ".mp4"),
                                        key=lambda item: (natural_key(item.stem), item.name))
                except OSError:
                    candidates = []
                for candidate in candidates:
                    match = FILE_PATTERN.fullmatch(candidate.name)
                    resolved = safe_file(candidate, self.data_root)
                    if resolved is None:
                        continue
                    # Only direct children of the two explicitly configured role directories.
                    if candidate.parent != folder:
                        continue
                    try:
                        info = resolved.stat()
                    except OSError:
                        continue
                    if info.st_size <= 0:
                        continue
                    if match:
                        part = int(match["part"]) if match["part"] else 0
                        lesson_id = match["session"] + (f"-part{part}" if part else "")
                        lesson_index = int(match["index"])
                        title = match["title"] + (f" · 第 {part} 段" if part else "")
                    else:
                        stem = unicodedata.normalize("NFC", candidate.stem).casefold()
                        lesson_id = "file-" + hashlib.sha256(stem.encode("utf-8")).hexdigest()[:16]
                        lesson_index = None
                        title = candidate.stem
                    lesson = lessons.setdefault(lesson_id, {
                        "id": lesson_id, "index": lesson_index, "title": title,
                        "camera": None, "screen": None, "_sort": natural_key(candidate.stem),
                    })
                    # Refuse ambiguous names rather than letting a second filename replace a stream.
                    if lesson[role] is not None:
                        continue
                    lesson[role] = {
                        "url": f"/media/{course_id}/{lesson_id}/{role}",
                        "name": candidate.name, "size": info.st_size,
                        "lastModified": info.st_mtime_ns // 1_000_000,
                        **self._metadata(resolved, self.data_root, info),
                    }
                    media[(course_id, lesson_id, role)] = MediaFile(candidate, self.data_root)
            ordered = sorted(lessons.values(), key=lambda item: (item["_sort"], item["id"]))
            for position, lesson in enumerate(ordered, 1):
                lesson.pop("_sort")
                if lesson["index"] is None:
                    lesson["index"] = position
            courses.append({"id": course_id, "name": course.name, "teacher": course.teacher, "lessons": ordered})
        self._media = media
        self._body = json.dumps({"courses": courses}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._scanned_at = time.monotonic()

    def snapshot(self) -> bytes:
        with self._lock:
            signature = self._config_signature()
            if signature != self._signature or time.monotonic() - self._scanned_at >= self.cache_seconds:
                # Invalid edits propagate instead of silently returning the previous library.
                config = load_config(self.config.project_root, self.config.requested_path)
                self.config = config
                self.data_root = config.data_root
                self.courses = config.courses
                self._scan()
                self._signature = signature
            return self._body

    def media(self, course_id: str, lesson_id: str, role: str) -> MediaFile | None:
        with self._lock:
            self.snapshot()
            return self._media.get((course_id, lesson_id, role))


def natural_key(value: str) -> tuple:
    parts = re.split(r"([0-9]+)", unicodedata.normalize("NFC", value).casefold())
    return tuple(int(part) if index % 2 else part for index, part in enumerate(parts))


def byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    """Return an inclusive single range; invalid/unsatisfiable ranges raise ValueError."""
    if value is None:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip(), re.IGNORECASE)
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError("Invalid byte range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Empty suffix range")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable byte range")
    return start, end


class PlayerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, port: int, *, project_root: Path = PROJECT_ROOT, config_path: Path | None = None):
        self.project_root = project_root.resolve()
        self.instance = project_instance(self.project_root)
        self.static_root = self.project_root / "dist"
        self.config = load_config(self.project_root, config_path)
        self.library = CourseLibrary(self.config)
        super().__init__(("127.0.0.1", port), PlayerHandler)


class PlayerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "YanheDuo/1.2"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, _format, *_args):
        # Browsers cancel previous ranges during seeks; these are ordinary operations.
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def _allowed_request(self) -> bool:
        port = self.server.server_port
        authorities = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "").lower() not in authorities:
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in authorities}:
            return False
        return self.headers.get("Sec-Fetch-Site", "") != "cross-site"

    def _bytes(self, body: bytes, content_type: str, head_only: bool, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _error(self, status: int, head_only: bool):
        body = json.dumps({"error": {403: "Forbidden", 404: "Not found", 405: "Method not allowed"}.get(status, "Request failed")}).encode("utf-8")
        self._bytes(body, "application/json; charset=utf-8", head_only, status)

    def _stream(self, path: Path, head_only: bool, *, media: bool):
        try:
            stream = path.open("rb")
        except OSError:
            self._error(404, head_only)
            return
        with stream:
            info = os.fstat(stream.fileno())
            size = info.st_size
            etag = f'"{info.st_mtime_ns:x}-{size:x}"'
            requested_range = self.headers.get("Range") if media else None
            if_range = self.headers.get("If-Range")
            if requested_range and if_range and if_range != etag:
                try:
                    modified = parsedate_to_datetime(if_range).timestamp()
                    if int(info.st_mtime) > modified:
                        requested_range = None
                except (TypeError, ValueError, OverflowError):
                    requested_range = None
            try:
                selected = byte_range(requested_range, size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            start, end = selected if selected is not None else (0, size - 1)
            length = max(0, end - start + 1)
            self.send_response(206 if selected is not None else 200)
            types = {".js": "application/javascript", ".css": "text/css", ".html": "text/html"}
            content_type = "video/mp4" if media else types.get(path.suffix.lower(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            if content_type in ("text/html", "text/css", "application/javascript"):
                content_type += "; charset=utf-8"
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Last-Modified", formatdate(info.st_mtime, usegmt=True))
            self.send_header("ETag", etag)
            if media:
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + quote(path.name))
            if selected is not None:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if path.suffix.lower() == ".html":
                self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self' blob:; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if head_only:
                return
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(512 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _dispatch(self, head_only: bool):
        try:
            if not self._allowed_request():
                self._error(403, head_only)
                return
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                self._error(404, head_only)
                return
            path = unquote(parsed.path)
            if "\x00" in path or "\\" in path or any(part in (".", "..") for part in path.split("/")):
                self._error(404, head_only)
                return
            if path == "/api/health":
                body = json.dumps({"app": APP_ID, "version": APP_VERSION, "status": "ok", "port": self.server.server_port,
                                   "instance": self.server.instance}).encode("utf-8")
                self._bytes(body, "application/json; charset=utf-8", head_only)
            elif path == "/api/library":
                self._bytes(self.server.library.snapshot(), "application/json; charset=utf-8", head_only)
            elif path.startswith("/media/"):
                match = MEDIA_PATTERN.fullmatch(path)
                item = self.server.library.media(*match.groups()) if match else None
                resolved = safe_file(item.path, item.boundary) if item else None
                if resolved is None:
                    self._error(404, head_only)
                else:
                    self._stream(resolved, head_only, media=True)
            else:
                relative = "index.html" if path == "/" else path.lstrip("/")
                candidate = self.server.static_root / relative
                resolved = safe_file(candidate, self.server.static_root)
                if resolved is None or resolved.suffix.lower() not in STATIC_EXTENSIONS:
                    self._error(404, head_only)
                else:
                    self._stream(resolved, head_only, media=False)
        except ConfigError as error:
            print(f"课程配置错误：{error}", file=sys.stderr)
            self._error(500, head_only)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            self.close_connection = True

    def do_GET(self):
        self._dispatch(False)

    def do_HEAD(self):
        self._dispatch(True)

    def do_POST(self):
        self.close_connection = True
        self._error(405, False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在本机打开双视频课程库；仅监听 127.0.0.1。")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--config", type=Path, help="课程配置 JSON；默认 config/courses.json，不存在时读取 courses.example.json")
    parser.add_argument("--no-browser", action="store_true", help="兼容启动器参数；服务本身始终不自动打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if not (PROJECT_ROOT / "dist" / "index.html").is_file():
        print("未找到播放器文件 dist/index.html。", file=sys.stderr)
        return 1
    try:
        server = PlayerServer(args.port, project_root=PROJECT_ROOT, config_path=args.config)
    except ConfigError as error:
        print(f"课程配置错误：{error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"无法启动本地播放器（端口 {args.port}）：{error}", file=sys.stderr)
        return 1
    print(f"{APP_ID} {APP_VERSION}: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
