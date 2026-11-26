import datetime as dt
import json
import logging
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlsplit

from state import encode, get, put, upsert_video

LOG = logging.getLogger("tiktok")
ID = re.compile(r"[0-9]{15,24}")
PROFILE = re.compile(r"/@([A-Za-z0-9_.-]+)/?")
PUBLIC_HEADERS = {"User-Agent": "Mozilla/5.0"}
ACCESS_ERROR = re.compile(
    r"captcha|verify (?:that|you|to continue)|security challenge|access denied|"
    r"forbidden|\b403\b|\b429\b|too many requests|log(?:in| in) (?:required|to)|"
    r"private|friends.only|not available in your (?:country|region)|age.restrict", re.I)


def profile_name(url):
    parsed = urlsplit(url)
    match = PROFILE.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"tiktok.com", "www.tiktok.com"} or not match:
        raise ValueError("Ожидается ссылка https://www.tiktok.com/@имя")
    return match[1]


def video_id(url, name):
    parsed = urlsplit(url)
    if parsed.netloc.lower() not in {"", "tiktok.com", "www.tiktok.com"}:
        return None
    match = re.fullmatch(r"/@([^/]+)/video/([0-9]{15,24})/?", parsed.path)
    return match[2] if match and match[1].casefold() == name.casefold() else None


class ProfilePage(HTMLParser):
    def __init__(self, name, script_id="__UNIVERSAL_DATA_FOR_REHYDRATION__"):
        super().__init__()
        self.name = name
        self.script_id = script_id
        self.links = set()
        self.raw = ""
        self.active = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            item = video_id(attrs.get("href") or "", self.name)
            if item:
                self.links.add(item)
        if tag == "script":
            self.active = attrs.get("id") == self.script_id

    def handle_data(self, data):
        if self.active:
            self.raw += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.active = False


def inspect_profile(html, name):
    page = ProfilePage(name)
    page.feed(html)
    scope = json.loads(page.raw) if page.raw else {}
    detail = scope.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {}) or {}
    info = detail.get("userInfo") or {}
    user = info.get("user") or {}
    matches = str(user.get("uniqueId", "")).casefold() == name.casefold()
    stats = info.get("stats") or {}
    return {
        "ids": sorted(page.links),
        "count": stats.get("videoCount") if matches else None,
        "private": user.get("privateAccount") if matches else None,
        "status_code": detail.get("statusCode"),
        "status_message": detail.get("statusMsg"),
    }


def entry(name, item, source):
    key = str(item.get("id") or "")
    if not ID.fullmatch(key):
        raise ValueError("Некорректный TikTok ID")
    timestamp = item.get("timestamp")
    date = item.get("upload_date")
    if isinstance(date, str) and re.fullmatch(r"[0-9]{8}", date):
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    elif not date and isinstance(timestamp, (float, int)):
        date = dt.datetime.fromtimestamp(timestamp, dt.UTC).date().isoformat()
    pinned = item.get("is_pinned", item.get("isPinnedItem", item.get("pinned")))
    return {
        "id": key, "url": f"https://www.tiktok.com/@{name}/video/{key}",
        "description": item.get("description"), "published": date,
        "duration": item.get("duration"), "pinned": None if pinned is None else int(bool(pinned)),
        "availability": item.get("availability"), "source": source,
    }


def request_profile(profile, name):
    request = urllib.request.Request(profile, headers=PUBLIC_HEADERS)
    with urllib.request.urlopen(request, timeout=25) as response:
        body = response.read(4 * 1024 * 1024).decode("utf-8", errors="replace")
    observed = inspect_profile(body, name)
    if observed["private"] or ACCESS_ERROR.search(observed["status_message"] or ""):
        raise PermissionError(observed["status_message"] or "Профиль закрыт")
    return observed


def widget_rows(html, name):
    route = "/embed/@" + name
    page = ProfilePage(name, "__FRONTITY_CONNECT_STATE__")
    page.feed(html)
    state = json.loads(page.raw or "{}")
    profile = state.get("source", {}).get("data", {}).get(route, {})
    rows = {}
    for video in profile.get("videoList") or []:
        if not isinstance(video, dict):
            continue
        key = str(video.get("id", ""))
        if (not ID.fullmatch(key) or video.get("privateItem")
                or str(video.get("authorUniqueId", "")).casefold() != name.casefold()):
            continue
        row = entry(name, {**video, "description": video.get("desc")}, "Официальный виджет TikTok")
        row["thumbnails"] = [{"id": kind, "url": url} for kind in ("cover", "dynamicCover", "originCover")
                             if isinstance(url := video.get(kind + "Url"), str)]
        rows[key] = row
    return rows


class ExtractorLog:
    def __init__(self, interval):
        self.interval = interval
        self.pages = []
        self.warnings = []

    def debug(self, message):
        if m := re.search(r"Downloading page (\d+)", message):
            self.pages.append(int(m[1]))
            if len(self.pages) > 1:
                time.sleep(self.interval)
        LOG.debug("%s", message)

    def warning(self, message):
        self.warnings.append(message)
        LOG.warning("%s", message)

    def error(self, message):
        LOG.error("%s", message)


def discovery_pass(db, profile, name, cfg):
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.tiktok import TikTokUserIE

    tracker = ExtractorLog(cfg["request_interval_seconds"])
    ids, observed = set(), {}
    with db:
        key = db.execute("INSERT INTO discovery DEFAULT VALUES").lastrowid
    outcome, detail = "ERROR", {}
    try:
        observed = request_profile(profile, name)
        for video in observed["ids"]:
            upsert_video(db, entry(name, {"id": video}, "TikTok profile HTML"))
        ids.update(observed["ids"])
        options = {"logger": tracker, "extract_flat": True, "lazy_playlist": True,
                   "skip_download": True, "quiet": True, "cachedir": False,
                   "socket_timeout": 20, "extractor_retries": 0, "retries": 0, "geo_bypass": False}

        class PublicProfile(TikTokUserIE):
            def _download_json(self, *args, **kwargs):
                payload = super()._download_json(*args, **kwargs)
                if not isinstance(payload, dict):
                    raise ValueError("Список TikTok вернул неожиданный формат")
                message = str(payload.get("statusMsg") or payload.get("status_msg") or "")
                if ACCESS_ERROR.search(message):
                    raise PermissionError(message)
                if payload.get("statusCode", payload.get("status_code", 0)) not in (0, "0", None):
                    raise ValueError(f"TikTok status {payload.get('statusCode', payload.get('status_code'))}: {message}")
                detail["last_has_more"] = payload.get("hasMorePrevious")
                detail["last_page_items"] = len(payload.get("itemList") or [])
                return payload

            def _parse_aweme_video_web(self, video, *args, **kwargs):
                result = super()._parse_aweme_video_web(video, *args, **kwargs)
                if "isPinnedItem" in video or "isTop" in video:
                    result["is_pinned"] = video.get("isPinnedItem", video.get("isTop"))
                return result

        with YoutubeDL(options, auto_init=False) as downloader:
            downloader.add_info_extractor(PublicProfile())
            extractor = downloader.get_info_extractor(PublicProfile.ie_key())
            playlist = extractor.extract(profile)
            for item in playlist["entries"]:
                if not item:
                    continue
                record = entry(name, item, "TikTok profile pagination")
                upsert_video(db, record)
                ids.add(record["id"])
        outcome = "END" if detail.get("last_has_more") in (False, 0) else "UNCERTAIN_END"
        if tracker.warnings and any("same page" in item.lower() for item in tracker.warnings):
            outcome = "UNCERTAIN_END"
    except (Exception, KeyboardInterrupt) as exc:
        reason = str(exc)
        outcome = "BLOCKED" if isinstance(exc, PermissionError) or ACCESS_ERROR.search(reason) else "ERROR"
        detail["error"] = reason
        LOG.warning("Обход %s, %s ID: %s", key, len(ids), reason)
        if isinstance(exc, KeyboardInterrupt):
            raise
    finally:
        detail.update({"pages": tracker.pages, "warnings": tracker.warnings, "profile": observed})
        with db:
            db.execute("""UPDATE discovery SET finished=CURRENT_TIMESTAMP,outcome=?,ids=?,
                       expected_count=?,detail=? WHERE id=?""",
                       (outcome, encode(sorted(ids)), observed.get("count"), encode(detail), key))
    return outcome, ids, observed.get("count")


def discover(db, profile, cfg):
    name = profile_name(profile)
    first = discovery_pass(db, profile, name, cfg)
    time.sleep(max(2, cfg["request_interval_seconds"]))
    if first[0] == "BLOCKED":
        put(db, "coverage", {"verified": False, "profile_count": first[2],
                             "pass_outcomes": [first[0]], "observed": [len(first[1])]})
        return False
    second = discovery_pass(db, profile, name, cfg)
    profile_count = second[2] if second[2] is not None else first[2]
    exact = (first[0] == second[0] == "END" and
             first[1] == second[1] and profile_count is not None and
             first[2] == second[2] == len(second[1]))
    previous = get(db, "coverage", {})
    put(db, "coverage", {**previous, "verified": exact, "profile_count": profile_count or previous.get("profile_count"),
                         "pass_outcomes": [first[0], second[0]],
                         "observed": [len(first[1]), len(second[1])],
                         "new_in_second_pass": len(second[1] - first[1])})
    return exact


def import_urls(db, profile, source_file):
    name = profile_name(profile)
    with source_file.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not (line := line.strip()) or line.startswith("#"):
                continue
            if line.startswith("{"):
                item = json.loads(line)
                if video_id(item.get("url", ""), name) != str(item.get("id")):
                    raise ValueError("ID и прямая ссылка владельца профиля должны совпадать")
            else:
                key = video_id(line, name)
                if not key:
                    raise ValueError(f"Строка без прямой ссылки на видео @{name}: {line}")
                item = {"id": key}
            upsert_video(db, entry(name, item, source_file.name))
