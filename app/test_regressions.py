import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import service
from recognition import caption_candidates, parse_caption, web_caption_tracks


PROFILE = "https://www.tiktok.com/@sample"
IDS = ["1000000000000002", "1000000000000001"]
CUE = "На лекции обсуждали подготовку к зимней сессии."
GOOD = {"summary": "Автор рассказывает о подготовке к зимней сессии.",
        "topic": "Подготовка к сессии", "category": "Учёба", "mood": "neutral",
        "evidence": ["подготовку к зимней сессии"]}


class ReportRegressions(unittest.TestCase):
    def test_public_widget_survives_profile_403(self):
        video = {"id": IDS[0], "authorUniqueId": "sample", "desc": "Публичный ролик",
                 "coverUrl": "https://p16.tiktokcdn.com/cover.jpg"}
        widget = {"source": {"data": {"/embed/@sample": {"videoList": [
            video, {**video, "id": IDS[1], "privateItem": True},
            {**video, "id": "1234567890123456789", "authorUniqueId": "another"},
        ]}}}}
        html = '<script id="__FRONTITY_CONNECT_STATE__">' + json.dumps(widget) + '</script>'
        with patch("requests.get", side_effect=[Mock(status_code=403), Mock(status_code=200, text=html)]) as fetch:
            rows, notes, complete = service.gather_public(PROFILE, "sample", 41, {})
        self.assertEqual(list(rows), [IDS[0]])
        self.assertEqual(rows[IDS[0]]["description"], "Публичный ролик")
        self.assertEqual(rows[IDS[0]]["thumbnails"][0]["id"], "cover")
        self.assertTrue(any("HTTP 403" in note for note in notes))
        self.assertFalse(complete)
        self.assertEqual(fetch.call_count, 2)

        with patch("requests.get", return_value=Mock(status_code=429)) as fetch:
            self.assertFalse(service.gather_public(PROFILE, "sample", 41, {})[0])
        self.assertEqual(fetch.call_count, 1)

    def test_caption_alternatives_and_clean_only(self):
        video = {"claInfo": {"originalLanguageInfo": {"languageCode": "ru-RU"},
                             "captionInfos": [{"languageCode": "ru-RU", "captionFormat": "webvtt",
                                               "urlList": ["https://example.org/track"]}]}}
        tracks, language = web_caption_tracks(video, {})
        [(_, _, choice)] = list(caption_candidates({"subtitles": tracks, "language": language}))
        self.assertEqual(choice["ext"], "vtt")
        vtt = "\ufeffWEBVTT\n\n00:00:02.000 --> 00:00:04.000\n" + CUE
        cues = parse_caption(vtt, choice["ext"])
        self.assertEqual(service.timed_captions({"clean": cues}), "0:02  " + CUE)
        raw = [{"text": CUE, "accepted": False}]
        self.assertFalse(service.caption_text({"source": "whisper", "raw": raw, "clean": []}))

    def test_rejects_echo_then_checks_quote(self):
        video = service.base_video(service.entry("sample", {"id": IDS[0]}, "test"),
                                   speech={"raw": [{"text": CUE}]})
        for bad_text in ("Короткий отчёт о публикации на русском языке.", "", service.VIDEO_PROMPT):
            model = Mock()
            model.ask.side_effect = [{**GOOD, "summary": bad_text}, dict(GOOD)]
            self.assertEqual(service.digest_video(model, video)["method"], "qwen")
            self.assertEqual(model.ask.call_count, 2)
        model.ask = Mock(return_value={**GOOD, "evidence": ["придуманная цитата"]})
        self.assertIsNone(service.digest_video(model, video))
        self.assertIn(CUE, service.fallback_analysis(video)["summary"])

    def test_all_catalog_and_same_text_across_jobs(self):
        def catalog(db, profile, _cfg):
            for key in IDS:
                service.upsert_video(db, service.entry("sample", {"id": key}, "test"))
            service.put(db, "coverage", {"profile_count": len(IDS)})
            return True

        workers = []
        start_thread = service.Thread.start

        def track_thread(worker):
            workers.append(worker)
            start_thread(worker)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipeline = Mock()
            pipeline.metadata.return_value = {"caption": {"clean": [{"text": CUE}]}, "info": {},
                                              "caption_status": "available", "caption_scan_version": 2}
            local = Mock()
            local.ask.side_effect = lambda prompt, data, *options: dict(GOOD) if "examples" not in data else {
                "summary": "В публикациях речь идёт о подготовке к экзаменам.",
                "findings": [{"text": GOOD["summary"], "ids": IDS}]}
            paths = {"DATA": root, "LEGACY_REPORTS": root / "old", "CATALOGS": root / "catalogs",
                     "EXPORTS": root / "reports", "PREVIEWS": root / "previews"}
            with (patch.multiple(service, **paths),
                  patch.object(service, "local_rows", return_value=({}, {})), patch.object(service, "discover", catalog),
                  patch.object(service, "Pipeline", return_value=pipeline), patch.object(service, "LocalModel", return_value=local),
                  patch.object(service, "collect_images", return_value=(None, [], [])),
                  patch.object(service, "image_text", return_value=([{"path": "preview.jpg", "clean": []}], [])),
                  patch.object(service, "materialize"), patch.object(service.Thread, "start", track_thread)):
                store = service.Reports({})
                first_id = store.start(PROFILE, 1, False, all_posts=True)["id"]
                workers[-1].join(5)
                first = store.get(first_id)
                self.assertEqual(first["selection"], IDS)
                self.assertTrue(first["catalog_complete"])
                self.assertEqual(local.ask.call_count, 3)
                restarted = service.Reports({})
                second_id = restarted.start(PROFILE, 1, False, all_posts=True)["id"]
                workers[-1].join(5)
                second = restarted.get(second_id)
                self.assertEqual(second["reused"], 2)
                self.assertEqual(local.ask.call_count, 3)
                self.assertEqual(second["state"], "complete")

    def test_deleted_reports_stay_deleted_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = {"DATA": root, "LEGACY_REPORTS": root / "old", "CATALOGS": root / "catalogs",
                     "EXPORTS": root / "reports", "PREVIEWS": root / "previews"}
            with patch.multiple(service, **paths):
                store = service.Reports({})
                def create(report_id):
                    report = {"id": report_id, "account": "sample", "created": None, "state": "complete",
                              "quality_version": service.QUALITY_VERSION, "videos": []}
                    store.save(report)
                    return report

                archive = create("archive")
                first = create("a" * 32)
                second = create("b" * 32)
                manually_removed = create("c" * 32)
                service.export_folder(manually_removed).rename(root / "removed-outside-app")
                service.LEGACY_REPORTS.mkdir()
                for report in (first, manually_removed):
                    service.atomic(service.LEGACY_REPORTS / (report["id"] + ".json"), service.encode(report))
                old_export = root / "exports" / service.export_folder(first).name
                old_export.mkdir(parents=True)
                (old_export / "report.html").write_text("legacy", encoding="utf-8")
                store.active = first["id"]
                with self.assertRaises(RuntimeError):
                    store.delete(first["id"])
                store.active = None
                self.assertTrue(store.delete(first["id"]))
                self.assertTrue(store.delete(archive["id"]))
                self.assertFalse(store.delete(first["id"]))
                self.assertFalse(old_export.exists())
                self.assertFalse(service.export_folder(first).exists())
                self.assertTrue((store.trash / first["id"] / "legacy_export" / "report.html").is_file())
                restarted = service.Reports({})
                self.assertEqual([row["id"] for row in restarted.list()], [second["id"]])
                self.assertIsNone(restarted.get(first["id"]))
                self.assertFalse(service.export_folder(manually_removed).exists())


if __name__ == "__main__":
    unittest.main()
