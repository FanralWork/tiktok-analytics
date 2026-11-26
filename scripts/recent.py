import argparse
import hashlib
import io
import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np
import requests
from PIL import Image

from catalog import ProfilePage, entry, inspect_profile, profile_name, video_id
from recognition import Recognizers, alike
from state import atomic, connect, encode, get


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "recent"
LOG = logging.getLogger("recent")
LIMIT = 2 * 2**20
TYPES = {"cover": "обложка", "originCover": "исходное превью", "dynamicCover": "дополнительное превью"}


def public_image(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    domains = ("tiktokcdn.com", "tiktokcdn-eu.com", "tiktokcdn-us.com", "tiktokcdn-asia.com")
    return parsed.scheme == "https" and any(host == d or host.endswith("." + d) for d in domains)


def read_jsonl(path):
    if not path.exists():
        return []
    result = []
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("Пропущена неполная строка %s", path.name)
                continue
            if isinstance(item, dict):
                result.append(item)
    return result


def recent_rows(db, start, end, name):
    rows = [dict(row) for row in db.execute("""
        SELECT v.*,s.data AS speech,r.data AS analysis
        FROM videos v
        LEFT JOIN stages s ON s.video_id=v.id AND s.stage='transcribe' AND s.state='DONE'
        LEFT JOIN stages r ON r.video_id=v.id AND r.stage='result' AND r.state='DONE'
        WHERE v.published BETWEEN ? AND ? ORDER BY v.published DESC,v.id DESC
        """, (start, end))]
    archive = read_jsonl(ROOT / "logs/discovery_inputs/initial_playlist.jsonl")
    second = read_jsonl(OUT / "verification.jsonl")

    def records(items):
        found = {}
        for item in items:
            key = video_id(item.get("url") or "", name)
            if not key or key != str(item.get("id")):
                continue
            record = entry(name, item, "public playlist")
            if record["published"]:
                found[key] = (record, item)
        return found

    first_map, second_map = records(archive), records(second)
    prior = {key for key, (item, _) in first_map.items() if start <= item["published"] <= end}
    repeat = {key for key, (item, _) in second_map.items() if start <= item["published"] <= end}
    by_id = {row["id"]: row for row in rows}
    for key in sorted((prior | repeat) - by_id.keys()):
        record = (second_map.get(key) or first_map[key])[0]
        by_id[key] = {**record, "speech": None, "analysis": None}
    rows = sorted(by_id.values(), key=lambda row: (row["published"], row["id"]), reverse=True)
    materials = {key: item[1] for key, item in (first_map | second_map).items()}
    repeat_reached = any(item["published"] < start for item, _ in second_map.values())
    errors = OUT / "verification.log"
    repeat_clean = errors.exists() and "ERROR:" not in errors.read_text(encoding="utf-8-sig", errors="replace")
    embed = OUT / "profile_embed.html"
    embedded_ids = set(inspect_profile(embed.read_text(encoding="utf-8-sig"), name)["ids"]) if embed.exists() else set()
    coverage = {"first": len(prior), "second": len(repeat), "new": len(repeat-prior),
                "first_reached_boundary": any(item["published"] < start for item, _ in first_map.values()),
                "second_reached_boundary": repeat_reached, "second_clean": repeat_clean,
                "matched": len(prior & repeat), "embed": len(embedded_ids),
                "embed_matched": len(embedded_ids & by_id.keys()),
                "repeat_verified": bool(prior and prior == repeat and repeat_reached and repeat_clean)}
    return rows, materials, coverage


def fresh_images(session, row):
    response = session.get(row["url"], timeout=18)
    if response.status_code != 200:
        raise ValueError(f"страница TikTok HTTP {response.status_code}")
    page = ProfilePage(profile_name(row["url"].split("/video/", 1)[0]))
    page.feed(response.text)
    data = json.loads(page.raw or "{}").get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail", {})
    item = (data.get("itemInfo") or {}).get("itemStruct") or {}
    if str(item.get("id")) != row["id"] or item.get("privateItem") or item.get("forFriend") or item.get("isProhibited"):
        raise ValueError("Страница не подтвердила доступность ID")
    video = item.get("video") or {}
    return [{"id": kind, "url": video[kind]} for kind in TYPES if isinstance(video.get(kind), str)]


def collect_images(row, public_archive, destination):
    result, errors, digests = [], [], set()
    deferred = False
    image_items = public_archive.get("thumbnails") or []
    with requests.Session() as session:
        def fetch(items, kinds):
            nonlocal deferred
            by_kind = {item.get("id"): item.get("url") for item in items}
            for kind in kinds:
                url = by_kind.get(kind)
                if not url or not public_image(url):
                    continue
                try:
                    with session.get(url, timeout=14, stream=True, allow_redirects=False) as response:
                        if response.status_code == 429:
                            deferred = True
                            errors.append(f"{TYPES[kind]} HTTP 429")
                            break
                        if response.status_code != 200 or not response.headers.get("Content-Type", "").startswith("image/"):
                            errors.append(f"{TYPES[kind]} HTTP {response.status_code}")
                            continue
                        raw = response.raw.read(LIMIT+1)
                    if len(raw) > LIMIT:
                        errors.append(f"{TYPES[kind]} превышает 2 МиБ")
                        continue
                    digest = hashlib.sha256(raw).hexdigest()
                    if digest in digests:
                        continue
                    with Image.open(io.BytesIO(raw)) as source:
                        frames = sorted({0, source.n_frames//2, source.n_frames-1}) if getattr(source, "is_animated", False) else [0]
                        for index in frames:
                            source.seek(index)
                            image = source.convert("RGB")
                            image.thumbnail((800, 800))
                            file = destination / f"{row['id']}_{kind}_{index}.jpg"
                            image.save(file, quality=84, optimize=True)
                            result.append({"kind": kind, "frame": index, "path": file.name})
                    digests.add(digest)
                except (requests.RequestException, OSError, ValueError) as exc:
                    errors.append(f"{TYPES[kind]}: {type(exc).__name__}")

        fetch(image_items, ("cover", "dynamicCover"))
        if not result and not deferred:
            fetch(image_items, ("originCover",))
        if not result and not deferred and (not errors or any("HTTP 403" not in problem for problem in errors)):
            try:
                fetch(fresh_images(session, row), ("cover", "dynamicCover", "originCover"))
            except (requests.RequestException, OSError, ValueError) as exc:
                errors.append(f"Публичные метаданные: {type(exc).__name__}")
    return row, result, errors


def image_text(models, directory, images):
    output, accepted = [], []
    for item in images:
        raw = np.frombuffer((directory / item["path"]).read_bytes(), dtype=np.uint8)
        picture = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if picture is None:
            continue
        original, clean, uncertain = models._ocr_frame(picture)
        output.append({**item, "ocr": original, "clean": clean, "uncertain": uncertain})
        for line in clean:
            if not any(alike(line["text"], previous) for previous in accepted):
                accepted.append(line["text"])
    return output, accepted


def field(value):
    return " ".join(str(value or "").split()).replace("|", "\\|").replace("<", "&lt;")


def listing_preview(previews):
    if not previews:
        return None
    def score(item):
        return sum(min(len(line["text"]), 50) * line["confidence"] for line in item["clean"])
    return max(previews, key=score)


def report(rows, coverage, start, end, name):
    count = len(rows)
    editions = [row for row in rows if row["speech"]]
    preview_ok = [row for row in rows if row["previews"]]
    text_ok = [row for row in rows if row["text"]]
    speech_and_ocr = sum(bool(row["speech"] and row["text"]) for row in rows)
    with_text = sum(bool(row["speech"] or row["text"]) for row in rows)
    closed = [row for row in rows if row.get("download_error")]
    by_month = Counter(row["published"][:7] for row in rows)
    body = [
        f"# Отчёт по @{name} за выбранный период", "",
        f"Период с **{start} по {end} включительно**. Даты публикаций взяты из каталога TikTok в UTC.", "",
        "## Покрытие", "",
        "| Проверка | Результат |", "|---|---:|",
        f"| Известные ID в периоде | {count} |",
        f"| Есть субтитры | {len(editions)} |",
        f"| Получено хотя бы одно превью | {len(preview_ok)} |",
        f"| Превью с распознанным текстом | {len(text_ok)} |",
        f"| Есть и субтитры, и распознанный текст превью | {speech_and_ocr} |",
        f"| Есть извлечённый текст хотя бы из одного источника | {with_text} |",
        f"| Есть лишь описание и/или превью без распознанного текста | {count-with_text} |",
        f"| Зафиксирован отказ доступа к видеодорожке в пробе | {len(closed)} |",
        "", "Описания отражают подписи к публикациям. OCR охватывает доступные изображения предпросмотра. "
        "Субтитры могут содержать ошибки и пропуски. Полные аудио и видеоряды в этом отчёте не проверены.", "",
        "## Сверка списка", "",
        f"Первый проход дал **{coverage['first']} ID** за период. Повторный дал **{coverage['second']} ID**, "
        f"из которых **{coverage['matched']}** совпали. Официальный виджет подтвердил "
        f"**{coverage['embed_matched']} из {coverage['embed']}** карточек последней выдачи.", "",
        ("Первый проход достиг публикаций старше нижней границы периода."
         if coverage["first_reached_boundary"] else "В первом проходе нижняя граница периода осталась непроверенной."), "",
        ("Повторный проход достиг публикаций старше начала периода, и наборы ID совпали."
         if coverage["repeat_verified"] else
         "Независимо подтвердить полноту списка за весь период не удалось."), "",
        "Число публикаций по частям периода. " + ", ".join(f"{month} — {n}" for month, n in sorted(by_month.items())) + ".", "",
    ]
    editorial = OUT / "findings.md"
    if editorial.exists():
        body.extend([editorial.read_text(encoding="utf-8").strip(), ""])
    notes_path = OUT / "caption_notes.json"
    notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else {}
    body.extend(["## Содержание по субтитрам", ""])
    for row in editions:
        analysis = row["analysis"] or {}
        if row["id"] in notes:
            text = notes[row["id"]]
        elif analysis.get("content") == "informative":
            text = analysis.get("summary", "")
        else:
            text = "Субтитры сохранены в записи источников. Уверенный смысловой вывод отсутствует."
        body.append(f"- {row['published']} · {field(text)} [{row['id']}]({row['url']})")
    body.extend(["", "## Реестр всех известных публикаций", "",
                 "В строке указан OCR одного наиболее читаемого превью. "
                 "Полные субтитры и OCR всех проверенных изображений доступны в [JSON](recent/evidence.json).", "",
                 "| Дата | Ролик | Источник | Описание | Текст на превью |",
                 "|---|---|---|---|---|"])
    for row in rows:
        source = []
        if row["speech"]:
            source.append("субтитры")
        preview = listing_preview(row["previews"])
        if preview:
            source.append(f"[превью](recent/previews/{preview['path']})")
        body.append(f"| {row['published']} | [{row['id']}]({row['url']}) | "
                    f"{', '.join(source) or 'описание'} | {field(row.get('description'))} | "
                    f"{field(' / '.join(line['text'] for line in preview['clean']) if preview else '')} |")
    atomic(ROOT / "report_recent.md", "\n".join(body) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()
    start, end = args.start.isoformat(), args.end.isoformat()
    if args.start > args.end:
        raise SystemExit("Начало периода позже конца")
    if not (ROOT / "database.sqlite").is_file():
        raise SystemExit("Сначала соберите каталог командой discover или import.")
    OUT.mkdir(exist_ok=True)
    folder = OUT / "previews"
    folder.mkdir(exist_ok=True)
    samples = OUT / "records"
    samples.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(OUT / "run.log", encoding="utf-8")])
    data = {}
    with closing(connect(ROOT, read_only=True)) as db:
        profile = get(db, "profile")
        if not profile:
            raise SystemExit("Сначала соберите каталог командой discover или import.")
        name = profile_name(profile)
        rows, materials, coverage = recent_rows(db, start, end, name)
        for row in rows:
            key = row["id"]
            row["speech"] = json.loads(row["speech"]) if row["speech"] else None
            row["analysis"] = json.loads(row["analysis"]) if row["analysis"] else None
            fail = db.execute("SELECT error FROM stages WHERE video_id=? AND stage='download' AND state='FAILED'",
                              (key,)).fetchone()
            row["download_error"] = fail[0] if fail else None
            stored = samples / (key + ".json")
            if stored.exists():
                data[key] = json.loads(stored.read_text(encoding="utf-8"))
    if not args.report_only:
        cfg = json.loads((ROOT / "config/settings.json").read_text(encoding="utf-8"))
        models = Recognizers(cfg, ROOT / ".cache")
        missing = [row for row in rows if row["id"] not in data]
        LOG.info("Известных ID %s, ожидают OCR %s", len(rows), len(missing))
        with ThreadPoolExecutor(max_workers=3) as pool:
            for offset in range(0, len(missing), 12):
                batch = missing[offset:offset+12]
                futures = [pool.submit(collect_images, row, materials.get(row["id"], {}), folder) for row in batch]
                throttled = False
                for future in futures:
                    row, images, errors = future.result()
                    checked, text = image_text(models, folder, images)
                    key = row["id"]
                    record = {"previews": checked, "text": text, "preview_errors": errors}
                    if not any("HTTP 429" in problem for problem in errors):
                        atomic(samples / (key + ".json"), encode(record))
                    else:
                        throttled = True
                    data[key] = record
                LOG.info("Готово %s/%s, превью %s, с текстом %s", len(data), len(rows),
                         sum(bool(v["previews"]) for v in data.values()), sum(bool(v["text"]) for v in data.values()))
                if throttled:
                    LOG.warning("Получен HTTP 429. Остановка запросов изображений до следующего запуска.")
                    break
    else:
        LOG.info("Перестраиваю отчёт из %s ранее проверенных изображений", len(data))
    for row in rows:
        row.update(data.get(row["id"], {"previews": [], "text": [], "preview_errors": ["не проверено"]}))
    atomic(OUT / "evidence.json", encode({"start": start, "end": end, "coverage": coverage, "videos": rows}))
    report(rows, coverage, start, end, name)
    LOG.info("Отчёт сохранён, строк %s", len(rows))


if __name__ == "__main__":
    main()
