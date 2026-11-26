import itertools
import logging
import random
import json
import re
import shutil
import subprocess
import time
from contextlib import closing
from pathlib import Path
from queue import Full, Queue
from threading import Event, Thread

from catalog import ACCESS_ERROR, ExtractorLog, ProfilePage, entry, profile_name
from meaning import LIST_FIELDS, LocalModel, analyze
from recognition import Recognizers, alike, caption_candidates, meaningful, parse_caption, probe, select_format, web_caption_tracks
from state import atomic, connect, encode, get, journal, memory_gib, put, remove_media, saved, upsert_video

LOG = logging.getLogger("tiktok")
STOP = re.compile(r"captcha|verify (?:that|you|to continue)|security challenge|\b429\b|too many requests", re.I)
UNAVAILABLE = re.compile(r"not available|unavailable|removed|deleted|does not exist|\b404\b|\b410\b", re.I)
TEMPORARY = re.compile(r"timed?\s*out|connection|temporary|\b50[0234]\b|reset|unreachable", re.I)
FINISHED = ("ANALYZED", "SKIPPED_NO_CONTENT", "NEEDS_REVIEW", "ERROR")


class RunStopped(RuntimeError):
    pass


def message_for(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        message = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        return message[-600:] or f"Декодер завершился с кодом {exc.returncode}"
    text = re.sub(r"https?://[^\s)]+", "[URL]", str(exc))
    return text[:1200]


def platform_data(info):
    return {"id": info["id"], "description": info.get("description"),
            "duration": info.get("duration"), "timestamp": info.get("timestamp"),
            "upload_date": info.get("upload_date"), "availability": info.get("availability"),
            "is_pinned": info.get("is_pinned"), "track": info.get("track"), "artist": info.get("artist")}


class Pipeline:
    def __init__(self, db, root, cfg, local_media=None):
        self.db, self.root, self.cfg = db, root, cfg
        self.local_media = local_media
        self.models = Recognizers(cfg, Path(__file__).resolve().parents[1] / ".cache")
        self.llm = LocalModel(cfg)
        self.delay_until = 0

    def check_resources(self):
        disk = shutil.disk_usage(self.root).free / 2**30
        ram = memory_gib()
        if disk < self.cfg["minimum_disk_gib"] or ram < self.cfg["minimum_ram_gib"]:
            raise RunStopped(f"Пауза из-за ресурсов: свободно {disk:.1f} ГиБ на диске, {ram:.1f} ГиБ RAM")

    def stage(self, video_id, name, status, func):
        data = saved(self.db, video_id, name)
        if data is not None:
            return data
        row = self.db.execute("SELECT attempts,state FROM stages WHERE video_id=? AND stage=?",
                              (video_id, name)).fetchone()
        used = row["attempts"] if row and row["state"] == "FAILED" else 0
        if used >= self.cfg["retries"]:
            return None
        for attempt in range(used, self.cfg["retries"]):
            self.check_resources()
            journal(self.db, video_id, name, "RUNNING")
            try:
                data = func()
            except RunStopped:
                raise
            except Exception as exc:
                error = message_for(exc)
                limited = name in ("metadata", "download") and bool(STOP.search(error))
                inaccessible = name in ("metadata", "download") and (
                    isinstance(exc, PermissionError) or bool(ACCESS_ERROR.search(error) or UNAVAILABLE.search(error)))
                journal(self.db, video_id, name, "FAILED", error=error,
                        status="NEEDS_REVIEW" if inaccessible or name.startswith(("transcribe", "ocr", "meaning", "caption")) else "ERROR",
                        attempt=True)
                if inaccessible:
                    with self.db:
                        self.db.execute("UPDATE videos SET availability='unavailable' WHERE id=?", (video_id,))
                LOG.warning("%s %s, попытка %s: %s", video_id, name, attempt+1, error)
                if limited:
                    raise RunStopped("TikTok ограничил запросы или запросил проверку. Состояние сохранено.") from exc
                if inaccessible or not TEMPORARY.search(error) or attempt+1 == self.cfg["retries"]:
                    return None
                time.sleep(3 * (2 ** attempt))
            else:
                journal(self.db, video_id, name, "DONE", data=data, status=status, attempt=True)
                return data
        return None

    def local_file(self, video_id):
        if self.local_media:
            for extension in (".mp4", ".mkv", ".webm", ".mov"):
                path = self.local_media / (video_id + extension)
                if path.is_file():
                    return path
        return None

    def youtube(self):
        from yt_dlp import YoutubeDL
        return YoutubeDL({"skip_download": True, "quiet": True, "noplaylist": True, "cachedir": False,
                          "geo_bypass": False, "socket_timeout": 25, "retries": 0, "extractor_retries": 0,
                          "writesubtitles": True, "writeautomaticsub": True, "logger": ExtractorLog(0)})

    def public_metadata(self, downloader, video):
        from yt_dlp.extractor.tiktok import TikTokIE
        from yt_dlp.networking import Request

        name = profile_name(get(self.db, "profile"))
        page = ProfilePage(name)
        with downloader.urlopen(Request(video["url"], headers={"User-Agent": "Mozilla/5.0"})) as response:
            page.feed(response.read(6*2**20).decode("utf-8", errors="replace"))
        if not page.raw:
            raise ValueError("Публичная страница ролика не вернула данные")
        scope = json.loads(page.raw).get("__DEFAULT_SCOPE__", {})
        detail = scope.get("webapp.video-detail") or {}
        code = detail.get("statusCode")
        if code not in (0, "0", None):
            message = str(detail.get("statusMsg") or "")
            if ACCESS_ERROR.search(message) or UNAVAILABLE.search(message) or code in (10204, 10222):
                raise PermissionError(f"TikTok status {code}: {message or 'ролик недоступен'}")
            raise ValueError(f"TikTok status {code}: {message or 'не удалось прочитать страницу ролика'}")
        item = (detail.get("itemInfo") or {}).get("itemStruct") or {}
        if str(item.get("id")) != video["id"]:
            raise ValueError("Публичная страница ролика не вернула запрошенный ID")
        if item.get("privateItem") or item.get("forFriend") or item.get("isProhibited"):
            raise PermissionError("Ролик опубликован с ограничением доступа")
        data = TikTokIE(downloader)._parse_aweme_video_web(item, video["url"], video["id"])
        data["subtitles"], language = web_caption_tracks(item.get("video") or {}, data.get("subtitles"))
        if language:
            data["language"] = language
        if "isPinnedItem" in item:
            data["is_pinned"] = item["isPinnedItem"]
        return data

    def metadata(self, video):
        from yt_dlp.networking import Request
        video_id = video["id"]
        if self.local_media:
            local = self.local_file(video_id)
            if not local:
                raise ValueError("Для ID нет локального файла")
            return {"info": {"id": video_id, "duration": video["duration"]}, "local": str(local), "caption": None}
        time.sleep(max(0, self.delay_until-time.monotonic()))
        self.delay_until = time.monotonic() + self.cfg["request_interval_seconds"]
        with self.youtube() as downloader:
            info = self.public_metadata(downloader, video)
            if str(info.get("id")) != video_id:
                raise ValueError("ID открытой публикации отличается от запрошенного")
            data = platform_data(info)
            name = profile_name(get(self.db, "profile"))
            upsert_video(self.db, entry(name, data, "TikTok video"))
            if info.get("availability") in {"private", "needs_auth", "subscriber_only", "premium_only"}:
                raise PermissionError(str(info["availability"]))
            caption = None
            caption_attempts = 0
            for source, lang, choice in itertools.islice(caption_candidates(info), 12):
                caption_attempts += 1
                try:
                    raw = choice.get("data")
                    if raw is None:
                        headers = {**(info.get("http_headers") or {}), **(choice.get("http_headers") or {})}
                        with downloader.urlopen(Request(choice["url"], headers=headers)) as response:
                            raw = response.read(4 * 2**20).decode("utf-8-sig", errors="replace")
                    parsed = parse_caption(raw, choice["ext"])
                    clean = []
                    for row in parsed:
                        if meaningful(row["text"]) and not (clean and alike(clean[-1]["text"], row["text"])):
                            clean.append(row)
                    if clean:
                        caption = {"source": source, "language": lang, "raw": parsed,
                                   "original": raw, "clean": clean, "uncertain": False}
                        break
                except Exception as exc:
                    if STOP.search(str(exc)):
                        raise
                    LOG.info("%s субтитры %s: %s", video_id, lang, message_for(exc))
            try:
                selected = select_format(info, self.cfg["maximum_video_height"], self.cfg["maximum_media_mib"]*2**20)
                if selected.get("acodec") == "none" and any(item.get("vcodec") == "none" for item in info.get("formats", [])):
                    raise ValueError("Видео и аудио опубликованы раздельно и требуют проверки формата")
                fmt = {"url": selected["url"], "ext": selected.get("ext", "mp4"),
                       "headers": {**(info.get("http_headers") or {}), **(selected.get("http_headers") or {})}}
                format_error = None
            except ValueError as exc:
                fmt, format_error = None, str(exc)
            return {"info": data, "caption": caption, "format": fmt, "format_error": format_error,
                    "caption_scan_version": 2,
                    "caption_status": "available" if caption else "unreadable" if caption_attempts else "unavailable",
                    "thumbnails": info.get("thumbnails") or [], "fetched": time.time()}

    def download(self, video, metadata):
        from yt_dlp.networking import Request
        path = self.root / "media" / f"{video['id']}.mp4"
        if metadata.get("local"):
            path = Path(metadata["local"])
        elif not path.is_file():
            if time.time() - metadata["fetched"] > 1200:
                metadata = self.metadata(video)
                journal(self.db, video["id"], "metadata", "DONE", data=metadata)
            fmt = metadata["format"]
            if not fmt:
                raise ValueError(metadata["format_error"])
            max_bytes = self.cfg["maximum_media_mib"] * 2**20
            tmp = path.with_suffix(".mp4.partial")
            try:
                with self.youtube() as downloader, downloader.urlopen(
                    Request(fmt["url"], headers=fmt["headers"])
                ) as response, tmp.open("wb") as handle:
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > max_bytes:
                        raise ValueError("Медиафайл больше настроенного лимита")
                    for block in iter(lambda: response.read(256*1024), b""):
                        if handle.tell() + len(block) > max_bytes:
                            raise ValueError("Медиафайл больше настроенного лимита")
                        handle.write(block)
                probe(tmp)
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
        streams = probe(path)
        has_video = any(s.get("codec_type") == "video" for s in streams["streams"])
        if not has_video:
            raise ValueError("Нет декодируемой видеодорожки")
        duration = float(streams.get("format", {}).get("duration") or video["duration"] or 0)
        if not 0 < duration < 86400:
            raise ValueError("Длительность медиа не определена")
        has_audio = any(s.get("codec_type") == "audio" for s in streams["streams"])
        with self.db:
            self.db.execute("UPDATE videos SET duration=COALESCE(duration,?),availability='public' WHERE id=?",
                            (duration, video["id"]))
        return {"path": str(path), "duration": duration, "has_audio": has_audio}

    def ensure_media(self, video):
        video_id = video["id"]
        cached = saved(self.db, video_id, "download")
        if cached and Path(cached["path"]).is_file():
            return cached
        if cached:
            with self.db:
                self.db.execute("DELETE FROM stages WHERE video_id=? AND stage='download'", (video_id,))
        meta = self.stage(video_id, "metadata", None, lambda: self.metadata(video))
        return self.stage(video_id, "download", "DOWNLOADED", lambda: self.download(video, meta)) if meta else None

    def process_media(self, video, thorough=False):
        key = video["id"]
        speech, screen = saved(self.db, key, "transcribe"), saved(self.db, key, "ocr")
        if speech is not None and screen is not None:
            if not thorough:
                return speech, screen
        metadata = self.stage(key, "metadata", None, lambda: self.metadata(video))
        if metadata is None:
            return None
        if speech is None and metadata.get("caption"):
            speech = self.stage(key, "transcribe", "TRANSCRIBED", lambda: metadata["caption"])
        media = self.ensure_media(video)
        if media is None:
            return None
        path = Path(media["path"])
        if speech is None:
            speech = self.stage(key, "transcribe", "TRANSCRIBED", lambda: self.models.transcribe(path, media["has_audio"]))
        if screen is None:
            screen = self.stage(key, "ocr", "OCR_COMPLETED" if speech is not None else None,
                                lambda: self.models.read_screen(path, media["duration"],
                                                                bool(speech and speech["clean"]), thorough))
        if speech is not None and screen is not None:
            self.save_transcript(video, speech, screen)
            return speech, screen
        if speech is not None or screen is not None:
            self.save_transcript(video, speech, screen)
        return None

    def save_transcript(self, video, speech, screen):
        key = video["id"]
        path = self.root / "transcripts" / (key + ".json")
        complete = {"id": key, "url": video["url"], "speech": speech, "screen": screen}
        text = encode(complete)
        atomic(path, text)
        # Check readable UTF-8 output before allowing video cleanup.
        if path.read_text(encoding="utf-8") != text:
            raise RunStopped(f"Повторное чтение расшифровки {key} не совпало с записанными данными")
        render = [f"# {key}", "", f"[TikTok]({video['url']})", "", "## Речь", ""]
        for seg in (speech or {}).get("raw", []):
            flag = " · требует проверки" if not seg.get("accepted", True) else ""
            render.append(f"- {seg['start']:.2f}–{seg['end']:.2f} с{flag} · {seg['text']}")
        render.extend(["", "## Надписи", ""])
        for frame in (screen or {}).get("clean", []):
            render.append(f"- {frame['time']:.2f} с · " + " / ".join(line["text"] for line in frame["lines"]))
        atomic(path.with_suffix(".md"), "\n".join(render) + "\n")

    def save_summary(self, video, data, speech, screen, status):
        video = dict(self.db.execute("SELECT * FROM videos WHERE id=?", (video["id"],)).fetchone())
        video.update(status=status, analysis=data, full_speech=speech["raw"], full_screen_text=screen["raw"],
                     clean_speech=speech["clean"], clean_screen_text=screen["clean"])
        key = video["id"]
        path = self.root / "summaries" / (key + ".json")
        atomic(path, encode(video))
        render = [f"# {data.get('topic') or key}", "", f"[TikTok]({video['url']})",
                  f" · {video['published'] or 'дата неизвестна'} · {status}", "",
                  data.get("summary") or data.get("reason") or "", "", data.get("detail", ""), ""]
        sources = {row["ref"]: row for row in data.get("evidence", [])}
        for field, label in LIST_FIELDS.items():
            values = data.get(field) or []
            if values:
                render.extend([f"## {label}", ""])
                for item in values:
                    times = ", ".join(f"{ref} · {sources[ref]['time']:.2f} с" for ref in item["evidence"])
                    render.append(f"- {item['text']} ({times})")
                render.append("")
        if data.get("uncertainties"):
            render.extend(["## Требует проверки", "", *("- " + item for item in data["uncertainties"]), ""])
        render.append(f"[Временные метки и расшифровка](../transcripts/{key}.md)")
        atomic(path.with_suffix(".md"), "\n".join(render) + "\n")

    def process_captions(self, video, prefetched=False):
        key = video["id"]
        metadata = saved(self.db, key, "metadata") if prefetched else self.stage(
            key, "metadata", None, lambda: self.metadata(video))
        if metadata is None:
            return
        speech = metadata.get("caption")
        if speech is None:
            note = "Доступные субтитры не найдены, речь и кадры не проверены"
            journal(self.db, key, "caption_scan", "DONE", data={"result": "no_captions"},
                    status="NEEDS_REVIEW", error=video["last_error"] or note)
            return
        screen = {"raw": [], "clean": [], "uncertain": True, "ocr_frames": 0, "decoded_frames": 0}
        if saved(self.db, key, "transcribe") is None:
            journal(self.db, key, "transcribe", "DONE", data=speech, status="TRANSCRIBED")
        self.save_transcript(video, speech, screen)
        data = self.stage(key, "caption_meaning", None, lambda: analyze(self.llm, video, speech, screen))
        if data is None:
            return
        data["scope"] = "captions_only"
        self.save_summary(video, data, speech, screen, "NEEDS_REVIEW")
        note = video["last_error"] or "Смысл разобран по субтитрам, экранный текст не проверен"
        journal(self.db, key, "result", "DONE", data=data, status="NEEDS_REVIEW", error=note)
        journal(self.db, key, "caption_scan", "DONE", data={"result": "analyzed_captions"})

    def prefetch_captions(self, pending):
        results = Queue(maxsize=self.cfg["batch_size"])
        stopped = Event()

        def deliver(entry):
            while not stopped.is_set():
                try:
                    results.put(entry, timeout=0.25)
                    return
                except Full:
                    continue

        def fetch():
            try:
                with closing(connect(self.root)) as db:
                    worker = Pipeline(db, self.root, self.cfg, self.local_media)
                    for row in pending:
                        if stopped.is_set():
                            break
                        worker.stage(row["id"], "metadata", None, lambda: worker.metadata(row))
                        deliver(row)
            except Exception as exc:
                deliver(exc)
            finally:
                deliver(None)

        thread = Thread(target=fetch, name="public-captions", daemon=True)
        thread.start()
        try:
            for item in iter(results.get, None):
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stopped.set()
            thread.join(timeout=2)

    def process(self, video, thorough=False, keep_media=False):
        key = video["id"]
        try:
            results = self.process_media(video, thorough)
            if results is None:
                metadata = saved(self.db, key, "metadata")
                if metadata and metadata.get("caption") and saved(self.db, key, "result") is None:
                    self.process_captions(video)
                return
            speech, screen = results
            if not (self.root / "transcripts" / (key + ".json")).exists():
                self.save_transcript(video, speech, screen)
            data = self.stage(key, "meaning", None, lambda: analyze(self.llm, video, speech, screen))
            if data is None:
                return
            if data["content"] != "informative" and speech["clean"] and screen["ocr_frames"] < 3 and not thorough:
                media = self.ensure_media(video)
                if media is None:
                    return
                new_screen = self.stage(key, "ocr_extended", None, lambda: self.models.read_screen(
                    Path(media["path"]), media["duration"], False))
                if new_screen is None:
                    return
                screen = new_screen
                self.save_transcript(video, speech, screen)
                data = self.stage(key, "meaning_extended", None, lambda: analyze(self.llm, video, speech, screen))
                if data is None:
                    return
            status = {"informative": "ANALYZED", "none": "SKIPPED_NO_CONTENT",
                      "uncertain": "NEEDS_REVIEW"}[data["content"]]
            if status == "ANALYZED" and data["confidence"] == "low":
                status = "NEEDS_REVIEW"
            self.save_summary(video, data, speech, screen, status)
            journal(self.db, key, "result", "DONE", data=data, status=status)
        finally:
            # Keep a pilot's source for spot checks, otherwise reclaim even failed downloads.
            if not keep_media and (saved(self.db, key, "result") is not None or
                                   (self.root / "transcripts" / (key + ".json")).is_file()):
                remove_media(self.root, key)

    def reset(self, selected, stages):
        for video_id in selected:
            with self.db:
                self.db.executemany("DELETE FROM stages WHERE video_id=? AND stage=?",
                                    [(video_id, stage) for stage in stages])
                self.db.execute("UPDATE videos SET status='DISCOVERED',last_error=NULL WHERE id=?", (video_id,))

    def retry_failed(self):
        with self.db:
            errors = self.db.execute("""SELECT video_id,stage FROM stages JOIN videos ON video_id=videos.id
                WHERE state IN ('FAILED','RUNNING') AND (COALESCE(availability,'') <> 'unavailable'
                    OR stage NOT IN ('metadata','download'))""").fetchall()
            self.db.executemany("DELETE FROM stages WHERE video_id=? AND stage=?",
                                [(row["video_id"], row["stage"]) for row in errors])
            for key in {row["video_id"] for row in errors}:
                self.db.execute("UPDATE videos SET status='DISCOVERED',last_error=NULL WHERE id=?", (key,))

    def resume_media(self, selected):
        where = """SELECT id FROM videos WHERE status='NEEDS_REVIEW' AND
                   EXISTS(SELECT 1 FROM stages WHERE video_id=videos.id AND
                          ((stage='caption_scan' AND state='DONE') OR
                           (stage='download' AND state='FAILED')))"""
        candidates = [row[0] for row in self.db.execute(where) if not selected or row[0] in selected]
        with self.db:
            for key in candidates:
                self.db.execute("""DELETE FROM stages WHERE video_id=? AND state='FAILED' AND
                                   stage IN ('metadata','download')""", (key,))
                self.db.execute("UPDATE videos SET status='DISCOVERED',last_error=NULL WHERE id=?", (key,))

    def run(self, pilot=False, ids=None, thorough=False, captions_only=False, checkpoint=None):
        rows = list(self.db.execute("SELECT * FROM videos ORDER BY published DESC,id DESC"))
        if captions_only:
            scanned = {row[0] for row in self.db.execute("""SELECT video_id FROM stages WHERE
                (stage='caption_scan' AND state='DONE') OR (stage='metadata' AND state='FAILED')""")}
            pending = [row for row in rows if row["id"] not in scanned
                       and row["status"] not in ("ANALYZED", "SKIPPED_NO_CONTENT")]
        elif self.local_media:
            pending = [row for row in rows if self.local_file(row["id"]) and
                       row["status"] not in ("ANALYZED", "SKIPPED_NO_CONTENT")]
            for row in pending:
                existing = saved(self.db, row["id"], "metadata") or {}
                if existing.get("local") != str(self.local_file(row["id"])):
                    stages = ["metadata", "download", "ocr", "ocr_extended", "meaning", "meaning_extended", "result"]
                    speech = saved(self.db, row["id"], "transcribe") or {}
                    if not speech.get("source", "").startswith("platform"):
                        stages.append("transcribe")
                    self.reset([row["id"]], stages)
        else:
            pending = [row for row in rows if row["status"] not in FINISHED]
        if ids:
            pending = [row for row in pending if row["id"] in ids]
        if pilot:
            pilot_ids = get(self.db, "pilot_ids")
            if pilot_ids is None:
                count = min(len(pending), self.cfg["pilot_size"])
                pilot_ids = [pending[i*len(pending)//count]["id"] for i in range(count)]
                put(self.db, "pilot_ids", pilot_ids)
            pending = [row for row in pending if row["id"] in pilot_ids]
        if not pending:
            LOG.info("Нет записей в очереди")
            return
        self.llm.check()
        done = len(rows)-len(pending) if captions_only and not ids else (
            len(scanned) if captions_only else sum(row["status"] in FINISHED for row in rows)
        )
        denied_in_a_row = 0
        source = self.prefetch_captions(pending) if captions_only else iter(pending)
        try:
            for i, row in enumerate(source, 1):
                if captions_only:
                    self.process_captions(row, prefetched=True)
                else:
                    self.process(row, thorough=thorough, keep_media=pilot)
                LOG.info("Готово %s/%s в этой очереди · %s/%s по базе · %.2f%% · %s", i, len(pending),
                         done+i, len(rows), 100*(done+i)/len(rows), row["id"])
                current = self.db.execute("SELECT status,last_error FROM videos WHERE id=?", (row["id"],)).fetchone()
                if current["status"] not in FINISHED:
                    raise RunStopped(f"Обработка {row['id']} не завершилась, проверьте журнал")
                denied_in_a_row = denied_in_a_row+1 if "403" in (current["last_error"] or "") else 0
                if not (pilot or captions_only) and denied_in_a_row >= 5:
                    raise RunStopped("Пять подряд отказов HTTP 403 на медиа. Дальнейшие запросы к медиа остановлены.")
                if checkpoint and i % self.cfg["batch_size"] == 0:
                    checkpoint()
        finally:
            if captions_only:
                source.close()


def qa_ids(db, seed=8323):
    randomizer = random.Random(seed)
    skipped = [row[0] for row in db.execute("SELECT id FROM videos WHERE status='SKIPPED_NO_CONTENT' ORDER BY id")]
    done = [row[0] for row in db.execute("SELECT id FROM videos WHERE status='ANALYZED' ORDER BY id")]
    return randomizer.sample(skipped, min(12, len(skipped))) + randomizer.sample(done, min(4, len(done)))
