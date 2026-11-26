import json
import logging
import re
from collections import Counter, defaultdict
from datetime import date

from meaning import LIST_FIELDS, SYNTHESIS_FIELDS, combine_batches
from state import atomic, get


STATUS = {
    "ANALYZED": "Содержательных роликов обработано",
    "SKIPPED_NO_CONTENT": "Пропущено без полезного содержания",
    "NEEDS_REVIEW": "Требуют проверки",
    "ERROR": "Технические ошибки",
}
HEADERS = ["ID", "TikTok", "Дата (UTC)", "Длительность, с", "Исходное описание", "Закреплён",
           "Доступность", "Речь / OCR", "Откуда содержание", "Тема", "Кратко", "Уверенность",
           "Статус", "Причина", "Источник ID"]


def list_videos(db):
    rows = db.execute("""SELECT videos.*, results.data AS analysis,
                         speech.data AS speech_data, ocr.data AS ocr_data
                         FROM videos
                         LEFT JOIN stages AS results ON results.video_id=videos.id AND results.stage='result'
                         LEFT JOIN stages AS speech ON speech.video_id=videos.id AND speech.stage='transcribe' AND speech.state='DONE'
                         LEFT JOIN stages AS ocr ON ocr.video_id=videos.id AND ocr.stage='ocr' AND ocr.state='DONE'
                         ORDER BY published DESC,id DESC""")
    return [(row, json.loads(row["analysis"]) if row["analysis"] else {}) for row in rows]


def render_xlsx(root, rows):
    import xlsxwriter

    tmp = root / "videos.xlsx.partial"
    with xlsxwriter.Workbook(tmp, {"strings_to_formulas": False, "strings_to_urls": False}) as workbook:
        sheet = workbook.add_worksheet("Видео")
        header = workbook.add_format({"bold": True, "font_name": "Arial", "font_color": "#FFFFFF",
                                      "bg_color": "#25415B", "valign": "vcenter", "text_wrap": True})
        day = workbook.add_format({"num_format": "yyyy-mm-dd", "font_name": "Arial"})
        link = workbook.add_format({"font_color": "#14558C", "underline": True, "font_name": "Arial"})
        content = workbook.add_format({"font_name": "Arial", "valign": "vcenter"})
        identifier = workbook.add_format({"font_name": "Arial", "valign": "vcenter", "num_format": "@", "quote_prefix": True})
        wrap = workbook.add_format({"font_name": "Arial", "valign": "vcenter", "text_wrap": True})
        issue = workbook.add_format({"bg_color": "#FFF1DA", "font_color": "#702D14"})
        sheet.hide_gridlines(2)
        sheet.freeze_panes(1, 2)
        sheet.set_row(0, 31)
        sheet.write_row(0, 0, HEADERS, header)
        for col, width in enumerate((23, 15, 14, 18, 49, 13, 19, 15, 20, 31, 55, 15, 27, 49, 27)):
            sheet.set_column(col, col, width, content)
        for index, (row, data) in enumerate(rows, 1):
            mode = {"speech": "речь", "screen": "надписи", "mixed": "смешанный"}.get(data.get("modality"), "")
            methods = " / ".join(part for part in (
                json.loads(row["speech_data"]).get("source", "") if row["speech_data"] else "",
                "OCR" if row["ocr_data"] else "") if part)
            values = [row["id"], None, None, row["duration"], row["description"], {
                0: "нет", 1: "да"}.get(row["pinned"]), row["availability"], methods, mode,
                data.get("topic"), data.get("summary"), data.get("confidence"),
                row["status"], row["last_error"] or data.get("reason"), row["source"]]
            sheet.write_row(index, 0, values)
            sheet.write_string(index, 0, row["id"], identifier)
            for col in (4, 10, 13):
                if values[col]:
                    sheet.write_string(index, col, str(values[col])[:32767], wrap)
            sheet.write_url(index, 1, row["url"], link, "Открыть TikTok")
            if row["published"]:
                try:
                    sheet.write_datetime(index, 2, date.fromisoformat(row["published"]), day)
                except ValueError:
                    sheet.write_string(index, 2, row["published"])
        sheet.autofilter(0, 0, max(1, len(rows)), len(HEADERS)-1)
        if rows:
            sheet.conditional_format(1, 12, len(rows), 12, {
                "type": "formula", "criteria": '=OR($M2="NEEDS_REVIEW",$M2="ERROR")', "format": issue})
    try:
        tmp.replace(root / "videos.xlsx")
    except PermissionError as exc:
        logging.getLogger("tiktok").warning("Не удалось обновить открытую книгу Excel, прогресс сохранён: %s", exc)


def links(ids, mapping):
    return ", ".join(f"[{key}]({mapping[key]['url']})" for key in ids if key in mapping)


def coverage_text(db, rows):
    counts = Counter(row["status"] for row, _ in rows)
    coverage = get(db, "coverage", {})
    expected = coverage.get("profile_count")
    analyzed = counts["ANALYZED"]
    terminal = sum(counts[key] for key in STATUS)
    result = [f"- Найдено уникальных ID: **{len(rows)}**.",
              *(f"- {label}: **{counts[status]}**." for status, label in STATUS.items()),
              f"- В очереди: **{len(rows)-terminal}**.",
              f"- Недоступных среди найденных: **{sum(row['availability']=='unavailable' for row, _ in rows)}**."]
    scanned = db.execute("SELECT COUNT(*) FROM stages WHERE stage='caption_scan' AND state='DONE'").fetchone()[0]
    captions = sum(bool(row["speech_data"] and json.loads(row["speech_data"]).get("source", "").startswith("platform"))
                   for row, _ in rows)
    partial = sum(data.get("scope") == "captions_only" and data.get("content") == "informative" for _, data in rows)
    technical = db.execute("""SELECT COUNT(DISTINCT stages.video_id) FROM stages JOIN videos
        ON videos.id=stages.video_id WHERE stages.state='FAILED'
        AND COALESCE(videos.availability,'') <> 'unavailable'""").fetchone()[0]
    if technical:
        result.append(f"- Роликов со сбоями отдельных этапов: **{technical}**.")
    if scanned or captions:
        result.extend([f"- Проверено в режиме субтитров: **{scanned}**.",
                       f"- Сохранены субтитры: **{captions}**.",
                       f"- Содержательные разборы по одним субтитрам: **{partial}**. Они включены в «Требуют проверки»."])
    if expected is not None:
        result.append(f"- Счётчик публикаций профиля: **{expected}**.")
        if expected:
            result.append(f"- Доля найденных от счётчика профиля: **{len(rows)/expected:.2%}**.")
            result.append(f"- Доля успешно обработанных от счётчика профиля: **{analyzed/expected:.2%}**.")
    if rows:
        result.append(f"- Доля успешно обработанных от числа известных ID: **{analyzed/len(rows):.2%}**.")
    result.append(f"- Полнота каталога: **{'подтверждена' if coverage.get('verified') else 'не подтверждена'}**.")
    if coverage.get("observed"):
        result.append("- ID в независимых проходах: " + ", ".join(map(str, coverage["observed"])) + ".")
    if coverage.get("notes"):
        result.extend("- " + note for note in coverage["notes"])
    return result


def markdown_outputs(db, root, rows, batches=(), synthesis_error=None):
    profile = get(db, "profile", "")
    mapping = {row["id"]: row for row, _ in rows}
    useful = [(row, data) for row, data in rows if data.get("content") == "informative"]
    verified = [(row, data) for row, data in useful if row["status"] == "ANALYZED"]
    topic_groups = defaultdict(list)
    topic_labels = {}
    for row, data in useful:
        names = list(dict.fromkeys([data["topic"], *data.get("tags", [])]))
        for label in names:
            clean = re.sub(r"\s+", " ", label.strip())
            if not clean:
                continue
            key = clean.casefold()
            topic_labels.setdefault(key, clean)
            topic_groups[key].append((row, data))
    grouped = sorted(topic_groups, key=lambda key: (-len(topic_groups[key]), key))
    topic_md = ["# Тематический указатель", "", f"[Профиль TikTok]({profile})", ""]
    if not grouped:
        topic_md += ["Речь и надписи пока не дали проверенных тематических записей.", ""]
    for key in grouped:
        topic_md.extend([f"## {topic_labels[key]}", ""])
        for row, data in topic_groups[key]:
            label = f"{row['published'] or 'дата неизвестна'} · {data['summary'].splitlines()[0]}"
            flag = " · требует проверки" if row["status"] != "ANALYZED" else ""
            topic_md.append(f"- {label}{flag}. {links([row['id']], mapping)} · "
                            f"[Разбор](summaries/{row['id']}.md)")
        topic_md.append("")
    atomic(root / "topics.md", "\n".join(topic_md) + "\n")

    knowledge = ["# База знаний", "", f"[Профиль TikTok]({profile}) · [Тематический указатель](topics.md)", "",
                 "Пункты ниже передают содержание роликов. Они снабжены ссылками для сверки.", ""]
    if not useful:
        knowledge.append("Извлечённых тезисов, фактов и инструкций пока нет.")
    for field, title in LIST_FIELDS.items():
        items = {}
        for row, data in useful:
            for item in data[field]:
                key = " ".join(item["text"].casefold().rstrip(".").split())
                source = items.setdefault(key, {"text": item["text"], "ids": [], "review": set()})
                if row["id"] not in source["ids"]:
                    source["ids"].append(row["id"])
                if row["status"] != "ANALYZED":
                    source["review"].add(row["id"])
        if items:
            knowledge.extend([f"## {title}", ""])
            for item in items.values():
                flag = " Требует проверки." if set(item["ids"]) == item["review"] else ""
                knowledge.append(f"- {item['text']}{flag} {links(item['ids'], mapping)}")
            knowledge.append("")
    atomic(root / "knowledge_base.md", "\n".join(knowledge) + "\n")

    summary = combine_batches(batches)
    report = ["# Анализ канала TikTok", "", f"[{profile.rsplit('/', 1)[-1]}]({profile})", "",
              "[Тематический указатель](topics.md) · [База знаний](knowledge_base.md) · [Excel](videos.xlsx)", "",
              "## Покрытие и результаты", "", *coverage_text(db, rows), ""]
    if not useful:
        report.extend(["Нет успешно обработанных роликов, из которых можно сделать выводы о канале.", ""])
    elif not verified:
        report.extend(["Предварительные выводы опираются на доступные субтитры. Экранный текст не проверен.", ""])
    elif not batches:
        report.extend(["В отчёт включены результаты отдельных роликов. Тематический синтез ещё не готов.", ""])
    if synthesis_error:
        report.extend([f"Синтез прерван: {synthesis_error}.", ""])
    if summary:
        for field, title in SYNTHESIS_FIELDS.items():
            report.extend([f"## {title}", ""])
            points = summary.get(field, [])
            for item in points:
                report.append(f"- {item['text']} {links(item['evidence'], mapping)}")
            if not points:
                report.append("Подтверждённых пунктов в обработанной выборке пока нет.")
            report.append("")
    if grouped:
        report.extend(["## Группировка роликов по темам", ""])
        for key in grouped:
            report.append(f"- {topic_labels[key]} · {len(topic_groups[key])} · "
                          f"{links([row['id'] for row, _ in topic_groups[key]], mapping)}")
        report.append("")
        if not batches:
            report.extend(["## Содержание отдельных роликов", ""])
            for row, data in useful:
                report.append(f"- {data['summary']} {links([row['id']], mapping)}")
            report.append("")
    review = [(row, row["last_error"] or data.get("reason") or "Требуется сверка записи")
              for row, data in rows if row["status"] in ("NEEDS_REVIEW", "ERROR")]
    if review:
        reasons = defaultdict(list)
        for row, error in review:
            reasons[error.replace("\n", " ")].append(row["id"])
        report.extend(["## Ошибки и записи для проверки", ""])
        for error, ids in sorted(reasons.items(), key=lambda pair: -len(pair[1])):
            report.append(f"- {error} · {len(ids)} · {links(ids[:10], mapping)}")
        report.append("")
    atomic(root / "report.md", "\n".join(report) + "\n")
