"""Authenticated Yanhe Classroom discovery and an in-memory, same-origin HLS relay.

Only session URLs returned to the signed-in account can become media resources.
No recordings, bearer tokens, or signed CDN URLs are written to disk or logged.
Protocol reference: BITNP/BIT_yanhe_download (MIT, AuYang261); see the notices.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


API_HOST = "cbiz.yanhekt.cn"
MEDIA_HOSTS = frozenset({"cvideo.yanhekt.cn"})
API_BASE = "https://" + API_HOST
SITE_ORIGIN = "https://www.yanhekt.cn"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
# Public protocol constants, not account credentials.
VIDEO_MAGIC = "1138b69dfef641d9d7ba49137d2d4875"
PATH_MARKER = hashlib.md5((VIDEO_MAGIC + "_100").encode()).hexdigest()
CLIENT_SIGNATURE = hashlib.md5((VIDEO_MAGIC + "_v1_undefined").encode()).hexdigest()
SIGNED_FIELDS = frozenset({"xvideo_token", "xclient_timestamp", "xclient_signature", "xclient_version", "platform"})
MAX_JSON = 3 * 1024 * 1024
MAX_PLAYLIST = 2 * 1024 * 1024
PAGE_SIZE = 20


class OnlineError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class _Resource:
    url: str
    kind: str
    initial: bool
    generation: int


class ProxyBody:
    """A bounded-read response body; callers must close it on client disconnect."""
    def __init__(self, response, release, valid):
        self._response = response
        self._release = release
        self._valid = valid
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        if not self._valid():
            self.close()
            raise StopIteration
        try:
            chunk = self._response.read(256 * 1024)
        except Exception:
            self.close()
            raise OnlineError(503, "stream_interrupted", "在线视频连接中断，请重试播放。") from None
        if not chunk:
            self.close()
            raise StopIteration
        return chunk

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._release()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class OnlineService:
    def __init__(self, auth_file: Path | None = None, opener=None, *,
                 clock=time.monotonic, wall_clock=time.time,
                 search_endpoint: str = "/v2/course/list"):
        self._clock = clock
        self._wall_clock = wall_clock
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self._lock = threading.RLock()
        self._token_lock = threading.Lock()
        self._course_lock = threading.Lock()
        self._api_gate = threading.BoundedSemaphore(2)
        self._media_gate = threading.BoundedSemaphore(4)
        self._token = None
        self._authenticated = None
        self._checked_at = float("-inf")
        self._generation = 0
        self._video_token = None
        self._video_deadline = 0.0
        self._video_server_now = 0.0
        self._video_obtained_at = 0.0
        self._search_cache = {}
        self._courses = {}
        self._resources = OrderedDict()
        self._resource_keys = {}
        if search_endpoint != "/v2/course/list":
            raise ValueError("Unsupported search endpoint")
        self._search_endpoint = search_endpoint
        # An explicitly configured file is read once. Logout never re-reads it.
        if auth_file is not None:
            try:
                path = Path(auth_file)
                if path.stat().st_size <= 4096:
                    self.set_auth(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, OnlineError):
                pass

    @staticmethod
    def _normalize_auth(token):
        if not isinstance(token, str):
            raise OnlineError(401, "invalid_auth", "请输入延河课堂的登录令牌。")
        token = re.sub(r"^Bearer\s+", "", token.strip(), flags=re.IGNORECASE).strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1].strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", token):
            raise OnlineError(401, "invalid_auth", "登录令牌应为 32 位十六进制字符，请从自己的已登录页面重新复制。")
        return token

    def _reset_locked(self):
        self._generation += 1
        self._checked_at = float("-inf")
        self._video_token = None
        self._video_deadline = 0.0
        self._search_cache.clear()
        self._courses.clear()
        self._resources.clear()
        self._resource_keys.clear()

    def set_auth(self, token):
        normalized = self._normalize_auth(token)
        with self._lock:
            self._reset_locked()
            self._token = normalized
            self._authenticated = None
        return self.status()

    def clear_auth(self):
        with self._lock:
            self._reset_locked()
            self._token = None
            self._authenticated = False
        return self.status()

    def _credential(self):
        with self._lock:
            if not self._token:
                raise OnlineError(401, "login_required", "请先使用自己的延河课堂登录令牌连接。")
            return self._token, self._generation

    def _current_generation(self, generation):
        with self._lock:
            return self._token is not None and generation == self._generation

    def _require_generation(self, generation):
        if not self._current_generation(generation):
            raise OnlineError(401, "auth_changed", "登录状态已改变，请重新选择课程。")

    def status(self, check=False):
        if check:
            _, generation = self._credential()
            self._validate_login(generation, force=True)
            self._video_credentials()
        with self._lock:
            result = {"configured": bool(self._token), "authenticated": self._authenticated}
            if self._video_token:
                result["video_token_expires_in"] = max(0, int(self._video_deadline - self._clock()))
            return result

    def _validate_login(self, generation, force=False):
        with self._lock:
            valid = self._authenticated is True and self._clock() - self._checked_at < 120
        if force or not valid:
            user = self._api("/v1/user")
            if not isinstance(user, dict) or not user:
                self._mark_expired(generation)
                raise OnlineError(401, "invalid_user_identity", "无法确认当前登录账号，请重新连接延河课堂。")
            with self._lock:
                self._require_generation(generation)
                self._authenticated = True
                self._checked_at = self._clock()

    @staticmethod
    def _validate_url(url, hosts):
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in hosts or parsed.port not in (None, 443)
                    or parsed.username or parsed.password or parsed.fragment
                    or "\\" in parsed.netloc or any(ord(char) < 32 for char in url)):
                raise ValueError()
            return parsed
        except (ValueError, TypeError):
            raise OnlineError(403, "invalid_media_host", "视频来源不在允许的平台地址中。") from None

    @staticmethod
    def _response_headers(response):
        return {str(key).lower(): str(value) for key, value in response.headers.items()}

    def _open(self, request, hosts, gate):
        if not gate.acquire(timeout=5):
            raise OnlineError(503, "upstream_busy", "当前在线视频请求较多，请稍后重试。")
        released = False
        def release():
            nonlocal released
            if not released:
                released = True
                gate.release()
        try:
            for _ in range(4):
                self._validate_url(request.full_url, hosts)
                try:
                    fn = self._opener.open if hasattr(self._opener, "open") else self._opener
                    response = fn(request, timeout=20)
                except HTTPError as error:
                    response = error
                except Exception:
                    raise OnlineError(503, "platform_unavailable", "暂时无法连接延河课堂，请稍后重试。") from None
                try:
                    final_url = response.geturl()
                    self._validate_url(final_url, hosts)
                    status = int(response.status)
                    if status not in (301, 302, 303, 307, 308):
                        return response, release
                    location = self._response_headers(response).get("location")
                    if not location:
                        raise OnlineError(503, "invalid_redirect", "平台返回了无法处理的跳转。")
                    next_url = urljoin(final_url, location)
                    self._validate_url(next_url, hosts)
                    request = Request(next_url, headers=dict(request.header_items()), method=request.get_method())
                except Exception:
                    response.close()
                    raise
                response.close()
            raise OnlineError(503, "too_many_redirects", "平台跳转次数过多，请稍后重试。")
        except OnlineError:
            release()
            raise
        except Exception:
            release()
            raise OnlineError(503, "platform_unavailable", "暂时无法连接延河课堂，请稍后重试。") from None

    def _api(self, path, params=None):
        token, generation = self._credential()
        url = API_BASE + path + ("?" + urlencode(params) if params else "")
        headers = {"Origin": SITE_ORIGIN, "Referer": SITE_ORIGIN + "/", "User-Agent": USER_AGENT,
                   "Xdomain-Client": "web_user", "Xclient-Version": "v1",
                   "Xclient-Signature": CLIENT_SIGNATURE, "Xclient-Timestamp": str(int(self._wall_clock())),
                   "Authorization": "Bearer " + token, "Accept": "application/json"}
        response, release = self._open(Request(url, headers=headers), {API_HOST}, self._api_gate)
        try:
            status = int(response.status)
            try:
                raw = response.read(MAX_JSON + 1)
            except Exception:
                raise OnlineError(503, "platform_unavailable", "平台连接暂时中断，请稍后重试。") from None
            self._require_generation(generation)
            if status == 401:
                self._mark_expired(generation)
                raise OnlineError(401, "login_expired", "延河课堂登录已失效，请重新连接。")
            if status == 403:
                raise OnlineError(403, "access_denied", "当前账号没有访问该课程的权限。")
            if status != 200 or len(raw) > MAX_JSON:
                raise OnlineError(503, "platform_unavailable", "平台暂时无法返回课程信息。")
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except (ValueError, UnicodeError):
                raise OnlineError(503, "invalid_platform_response", "平台返回了无法识别的数据。") from None
            if not isinstance(payload, dict):
                raise OnlineError(503, "invalid_platform_response", "平台返回了无法识别的数据。")
            if str(payload.get("code")) != "0":
                code = str(payload.get("code", ""))
                message = str(payload.get("message", "")).lower()
                if code in {"61101113", "61101114"} or any(word in message for word in ("登录", "过期", "认证", "token")):
                    self._mark_expired(generation)
                    raise OnlineError(401, "login_expired", "延河课堂登录已失效，请重新连接。")
                if any(word in message for word in ("权限", "禁止", "无权", "permission", "forbidden")):
                    raise OnlineError(403, "access_denied", "当前账号没有访问该课程的权限。")
                raise OnlineError(503, "platform_error", "平台暂时无法完成请求，请核对课程后重试。")
            return payload.get("data")
        finally:
            try:
                response.close()
            finally:
                release()

    def _mark_expired(self, generation):
        with self._lock:
            if generation == self._generation:
                self._reset_locked()
                self._authenticated = False

    @staticmethod
    def _course_summary(row):
        professors = row.get("professors") or []
        if isinstance(professors, (str, dict)):
            professors = [professors]
        elif not isinstance(professors, (list, tuple)):
            professors = []
        teachers = []
        for item in professors:
            if isinstance(item, str):
                name = item
            elif isinstance(item, dict):
                name = item.get("name") or item.get("name_zh")
            else:
                continue
            if isinstance(name, str) and name.strip() and name.strip() not in teachers:
                teachers.append(name.strip())
        teacher = "、".join(teachers) or str(row.get("professor_name") or "").strip()
        year = str(row.get("school_year") or "").strip()
        semester = str(row.get("semester") or "").strip()
        semester = {"1": "第一学期", "2": "第二学期"}.get(semester, semester)
        if semester.isdigit():
            semester = f"第{semester}学期"
        term = year + semester
        return {"id": str(row.get("id", "")), "name": str(row.get("name_zh") or row.get("name") or "未命名课程"),
                "teacher": teacher, "term": term.strip(),
                "school": str(row.get("college_name") or row.get("university_name") or "")}

    def search(self, query, page=1):
        _, generation = self._credential()
        query = str(query).strip()
        try:
            page = int(page)
        except (ValueError, TypeError):
            raise OnlineError(400, "invalid_page", "课程页码无效。") from None
        if not query or len(query) > 100 or not 1 <= page <= 1000:
            raise OnlineError(400, "invalid_search", "请输入 1 至 100 字的课程名称。")
        self._validate_login(generation)
        key = (query, page)
        with self._lock:
            self._require_generation(generation)
            cached = self._search_cache.get(key)
            if cached and self._clock() - cached[0] < 30:
                return cached[1]
        data = self._api(self._search_endpoint, {"keyword": query, "page": page, "page_size": PAGE_SIZE})
        if isinstance(data, dict):
            rows = data.get("data", [])
            try:
                has_more = int(data.get("current_page", page)) < int(data.get("last_page", page))
            except (TypeError, ValueError):
                has_more = bool(data.get("next_page_url"))
        elif isinstance(data, list):
            rows, has_more = data, len(data) == PAGE_SIZE
        else:
            raise OnlineError(503, "invalid_catalog", "平台课程列表格式暂不可用。")
        if not isinstance(rows, list):
            raise OnlineError(503, "invalid_catalog", "平台课程列表格式暂不可用。")
        courses = [self._course_summary(row) for row in rows if isinstance(row, dict) and str(row.get("id", "")).isdigit()]
        result = {"courses": courses, "page": page, "has_more": bool(has_more)}
        with self._lock:
            self._require_generation(generation)
            if len(self._search_cache) > 100:
                self._search_cache.clear()
            self._search_cache[key] = (self._clock(), result)
        return result

    @staticmethod
    def _identifier(value):
        value = str(value)
        if not re.fullmatch(r"\d{1,12}", value):
            raise OnlineError(400, "invalid_id", "课程或节次编号无效。")
        return value

    @staticmethod
    def _duration(value):
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError):
            return None

    def course(self, course_id):
        course_id = self._identifier(course_id)
        _, generation = self._credential()
        self._validate_login(generation)
        with self._course_lock:
            with self._lock:
                self._require_generation(generation)
                cached = self._courses.get(course_id)
                if cached and self._clock() - cached[0] < 120:
                    return cached[1]
            row = self._api("/v1/course", {"id": course_id, "with_professor_badges": "true"})
            if not isinstance(row, dict):
                raise OnlineError(403, "course_unavailable", "当前账号无法读取这门课程。")
            sessions = self._api("/v2/course/session/list", {"course_id": course_id})
            if sessions == []:
                sessions = self._api("/v1/course/session/list", {"course_id": course_id})
            if not isinstance(sessions, list):
                raise OnlineError(503, "invalid_sessions", "平台课节列表格式暂不可用。")
            summary = self._course_summary({**row, "id": course_id})
            lessons, raw_lessons = [], {}
            ordered = sorted((item for item in sessions if isinstance(item, dict)),
                             key=lambda item: (str(item.get("started_at") or ""), str(item.get("id", ""))))
            for item in ordered:
                session_id = str(item.get("id", ""))
                if not re.fullmatch(r"\d{1,12}", session_id):
                    continue
                videos = item.get("videos") or []
                if not isinstance(videos, list):
                    continue
                for part, video in enumerate(videos, 1):
                    if not isinstance(video, dict):
                        continue
                    streams = {}
                    for role, field in (("camera", "main"), ("screen", "vga")):
                        url = video.get(field)
                        if not isinstance(url, str) or not url:
                            continue
                        try:
                            self._validate_url(url, MEDIA_HOSTS)
                        except OnlineError:
                            continue
                        streams[role] = url
                    if not streams:
                        continue
                    lesson_id = session_id + (f"-part{part}" if len(videos) > 1 else "")
                    title = str(item.get("title") or "课堂录像") + (f" · 第 {part} 段" if len(videos) > 1 else "")
                    lesson = {"id": lesson_id, "index": len(lessons) + 1, "title": title,
                              "camera": "camera" in streams, "screen": "screen" in streams}
                    duration = self._duration(video.get("duration"))
                    if duration is not None:
                        lesson["duration"] = duration
                    lessons.append(lesson)
                    raw_lessons[lesson_id] = (lesson, streams)
            summary["lessons"] = lessons
            with self._lock:
                self._require_generation(generation)
                self._courses[course_id] = (self._clock(), summary, raw_lessons)
            return summary

    def session(self, course_id, session_id):
        course_id = self._identifier(course_id)
        session_id = str(session_id)
        if not re.fullmatch(r"\d{1,12}(?:-part\d{1,4})?", session_id):
            raise OnlineError(400, "invalid_session", "课节编号无效。")
        _, generation = self._credential()
        summary = self.course(course_id)
        with self._lock:
            self._require_generation(generation)
            entry = self._courses.get(course_id)
            selected = entry[2].get(session_id) if entry else None
        if selected is None:
            raise OnlineError(403, "session_unavailable", "这一节不在当前账号可访问的课节列表中。")
        lesson, streams = selected
        result = {"course": {key: value for key, value in summary.items() if key != "lessons"}, "lesson": lesson,
                  "camera": None, "screen": None}
        for role, url in streams.items():
            resource_id = self._register(url, "playlist", True, generation)
            descriptor = {"url": "/online-media/" + resource_id, "name": lesson["title"] + (" · 课堂" if role == "camera" else " · PPT"),
                          "kind": "hls", "identity": f"online:{course_id}:{session_id}:{role}"}
            if "duration" in lesson:
                descriptor["duration"] = lesson["duration"]
            result[role] = descriptor
        return result

    @staticmethod
    def _unsigned_url(url):
        parsed = urlsplit(url)
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in SIGNED_FIELDS]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))

    def _register(self, url, kind, initial, generation):
        self._validate_url(url, MEDIA_HOSTS)
        url = self._unsigned_url(url)
        key = (url, kind, initial, generation)
        with self._lock:
            self._require_generation(generation)
            existing = self._resource_keys.get(key)
            if existing in self._resources:
                self._resources.move_to_end(existing)
                return existing
            resource_id = secrets.token_urlsafe(24)
            self._resources[resource_id] = _Resource(url, kind, initial, generation)
            self._resource_keys[key] = resource_id
            while len(self._resources) > 8192:
                old_id, old = self._resources.popitem(last=False)
                self._resource_keys.pop((old.url, old.kind, old.initial, old.generation), None)
            return resource_id

    def _video_credentials(self, force=False):
        _, generation = self._credential()
        with self._token_lock:
            with self._lock:
                self._require_generation(generation)
                if not force and self._video_token and self._clock() < self._video_deadline:
                    return self._video_token, int(self._video_server_now + self._clock() - self._video_obtained_at)
            self._validate_login(generation)
            data = self._api("/v1/auth/video/token", {"id": 0})
            if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
                raise OnlineError(401, "video_auth_failed", "无法取得视频播放授权，请重新连接延河课堂。")
            try:
                now = float(data["now"])
                expires = float(data["expired_at"])
                if not math.isfinite(now) or not math.isfinite(expires) or expires <= now:
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise OnlineError(503, "invalid_video_expiry", "平台的视频授权有效期无效。") from None
            obtained = self._clock()
            with self._lock:
                self._require_generation(generation)
                self._video_token = data["token"]
                self._video_server_now = now
                self._video_obtained_at = obtained
                self._video_deadline = obtained + max(1.0, expires - now - min(60, (expires - now) / 2))
            return data["token"], int(now)

    def _signed_url(self, resource, force=False):
        token, timestamp = self._video_credentials(force)
        self._require_generation(resource.generation)
        parsed = urlsplit(resource.url)
        path = parsed.path
        if resource.initial:
            prefix, separator, filename = path.rpartition("/")
            if not separator or not filename:
                raise OnlineError(503, "invalid_playlist", "平台视频地址无效。")
            if prefix.rsplit("/", 1)[-1] != PATH_MARKER:
                path = prefix + "/" + PATH_MARKER + "/" + filename
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in SIGNED_FIELDS]
        query.extend((("Xvideo_Token", token), ("Xclient_Timestamp", str(timestamp)),
                      ("Xclient_Signature", hashlib.md5((VIDEO_MAGIC + "_v1_" + str(timestamp)).encode()).hexdigest()),
                      ("Xclient_Version", "v1"), ("Platform", "yhkt_user")))
        return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))

    def _rewrite_playlist(self, raw, base_url, generation):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError:
            raise OnlineError(503, "invalid_playlist", "平台播放清单编码无效。") from None
        if not text.lstrip().startswith("#EXTM3U"):
            raise OnlineError(503, "invalid_playlist", "平台未返回有效的视频播放清单。")
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if any(re.fullmatch(r"#EXT-X-TARGETDURATION:\s*0", line.strip()) for line in lines):
            raise OnlineError(503, "empty_recording", "平台这节录像的转码尚未完成，暂时无法播放。")
        def local_uri(uri, kind=None):
            if uri.startswith("data:") and kind == "key":
                return uri
            url = urljoin(base_url, uri)
            resolved_kind = kind or ("playlist" if urlsplit(url).path.lower().endswith(".m3u8") else "segment")
            return "/online-media/" + self._register(url, resolved_kind, False, generation)
        output = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                output.append("")
            elif not stripped.startswith("#"):
                output.append(local_uri(stripped))
            else:
                kind = "key" if stripped.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")) else None
                if stripped.startswith(("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-RENDITION-REPORT:")):
                    kind = "playlist"
                line = re.sub(r"\bURI=(['\"])(.*?)\1", lambda match: "URI=" + match[1] + local_uri(match[2], kind) + match[1], line)
                output.append(line)
        return ("\n".join(output).rstrip() + "\n").encode("utf-8")

    def proxy(self, resource_id, range_header=None, head_only=False):
        self._credential()
        if not isinstance(resource_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,80}", resource_id):
            raise OnlineError(404, "resource_not_found", "视频资源已失效，请重新选择课节。")
        with self._lock:
            resource = self._resources.get(resource_id)
            if resource is not None:
                self._resources.move_to_end(resource_id)
        if resource is None:
            raise OnlineError(404, "resource_not_found", "视频资源已失效，请重新选择课节。")
        self._require_generation(resource.generation)
        if range_header is not None and not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header.strip()):
            raise OnlineError(416, "invalid_range", "不支持的视频分段范围。")
        for attempt in range(2):
            signed = self._signed_url(resource)
            headers = {"Origin": SITE_ORIGIN, "Referer": SITE_ORIGIN + "/", "User-Agent": USER_AGENT,
                       "Accept": "*/*", "Accept-Encoding": "identity"}
            if range_header and resource.kind != "playlist":
                headers["Range"] = range_header.strip()
            method = "HEAD" if head_only and resource.kind != "playlist" else "GET"
            response, release = self._open(Request(signed, headers=headers, method=method), MEDIA_HOSTS, self._media_gate)
            status = int(response.status)
            if status in (401, 403):
                response.close(); release()
                if attempt == 0:
                    rejected = dict(parse_qsl(urlsplit(signed).query)).get("Xvideo_Token")
                    with self._lock:
                        # Coalesce simultaneous expired-token responses from the two players.
                        if self._video_token == rejected:
                            self._video_deadline = 0.0
                    continue
                raise OnlineError(403, "video_access_denied", "平台未允许播放这段录像，请核对账号权限或重新连接。")
            if status not in (200, 206, 416):
                response.close(); release()
                raise OnlineError(503, "stream_unavailable", "平台视频暂时不可用，请稍后重试。")
            remote_headers = self._response_headers(response)
            content_type = remote_headers.get("content-type", "")
            is_playlist = resource.kind == "playlist" or "mpegurl" in content_type.lower()
            result_headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
            if is_playlist and status == 200:
                try:
                    try:
                        raw = response.read(MAX_PLAYLIST + 1)
                    except Exception:
                        raise OnlineError(503, "platform_unavailable", "平台播放清单暂时无法读取。") from None
                    if len(raw) > MAX_PLAYLIST:
                        raise OnlineError(503, "playlist_too_large", "平台播放清单过大，无法处理。")
                    body = self._rewrite_playlist(raw, response.geturl(), resource.generation)
                finally:
                    response.close(); release()
                result_headers.update({"Content-Type": "application/vnd.apple.mpegurl", "Content-Length": str(len(body))})
                return 200, result_headers, b"" if head_only else body
            result_headers["Content-Type"] = ("application/octet-stream" if resource.kind == "key" else content_type or "video/mp2t")
            for name in ("content-length", "content-range", "accept-ranges"):
                if name in remote_headers:
                    result_headers["-".join(part.title() for part in name.split("-"))] = remote_headers[name]
            if head_only or status == 416:
                response.close(); release()
                if status == 416:
                    result_headers["Content-Length"] = "0"
                return status, result_headers, b""
            if "Content-Length" not in result_headers:
                result_headers["Connection"] = "close"
            return status, result_headers, ProxyBody(response, release, lambda: self._current_generation(resource.generation))
        raise OnlineError(503, "stream_unavailable", "平台视频暂时不可用。")
