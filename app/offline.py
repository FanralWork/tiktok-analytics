import base64
import io
from html import escape

from service import AUTHOR, MOODS, caption_message, image_file, stats, timed_captions

STYLE = """<style>
:root{color-scheme:light;--ink:#211e32;--muted:#615b75;--line:#e8e4f0;--accent:#6142ce}
*{box-sizing:border-box} body{margin:0;background:#f5f3fa;color:var(--ink);font:16px/1.6 system-ui,sans-serif}
main{width:min(1120px,92vw);margin:48px auto 80px} a{color:#5434bf;text-underline-offset:4px}
header,section{background:#fff;border:1px solid var(--line);border-radius:18px;padding:24px 30px;margin-bottom:18px}
header{background:#242039;color:white;border:0;padding:34px} header p{color:#e3dcf4} header a{color:#dac8ff}
h1,h2,h3,p{margin:0 0 10px} h1{font-size:clamp(29px,5vw,52px);line-height:1.12}
h2{font-size:23px}h3{font-size:17px} small,.muted{color:var(--muted)}
.counts{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.metric{padding:13px 17px;background:#f8f6ff;border-radius:11px;color:var(--muted)}
.metric strong{display:block;color:var(--ink);font-size:30px}.videos{padding:0 26px}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:24px}.legend{margin:4px 0 8px}.legend small{display:flex;justify-content:space-between}
.track{height:6px;background:#f1ecf7;border-radius:99px;margin-top:3px}.track i{display:block;height:100%;border-radius:99px;background:#896de4}
article{display:grid;grid-template-columns:120px 1fr;gap:20px;padding:22px 0;border-top:1px solid var(--line)}
article:first-of-type{border-top:0} img{width:110px;aspect-ratio:4/5;object-fit:cover;border-radius:12px;background:#eae4f4}
details{margin-top:8px}details summary{cursor:pointer;color:var(--accent)}details p{white-space:pre-wrap}li{margin-bottom:8px}
@media(max-width:550px){main{margin-top:20px}header,section{padding:18px}article{grid-template-columns:72px 1fr;gap:12px}img{width:68px}.charts{grid-template-columns:1fr}}
@media print{body{background:white}main{width:100%;margin:0} header{color:black;background:white}header p{color:black}article{break-inside:avoid}}
</style>"""


def thumbnail(url):
    from PIL import Image
    path = image_file(url or "")
    if not path or not path.is_file():
        return ""
    try:
        with Image.open(path) as image:
            image.thumbnail((240, 300))
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=68, optimize=True)
            data = base64.b64encode(output.getvalue()).decode()
    except OSError:
        return ""
    return f'<img loading="lazy" alt="Превью ролика" src="data:image/jpeg;base64,{data}">'


def export_html(report):
    title = f"@{report['account']}"
    count = stats(report)
    labels = {"selected": "Ролики", "captions": "Субтитры", "previews": "Превью",
              "ocr": "Текст на превью", "summaries": "Выводы"}
    overview = report.get("overview") or {}
    links = {v["id"]: v["url"] for v in report["videos"]}
    findings = []
    for point in overview.get("findings", []):
        sources = ", ".join(f'<a href="{escape(links[key], quote=True)}">{escape(key)}</a>'
                            for key in point["ids"] if key in links)
        findings.append(f"<li>{escape(point['text'])} {sources}</li>")
    cards = []
    for v in report["videos"]:
        analysis = v.get("analysis") or {}
        body = []
        for label, val in (("Описание", v["description"]), ("Субтитры и расшифровка", timed_captions(v["speech"])
                            or caption_message(v.get("caption_status"))),
                           ("Текст на превью", " / ".join(v["text"])), ("Ограничения", " / ".join(v["errors"]))):
            if val:
                body.append(f"<p><strong>{label}</strong><br>{escape(val)}</p>")
        image = thumbnail(v["preview"])
        date = v.get("published") or "Дата неизвестна"
        summary = analysis.get("summary") or ("Для вывода недостаточно исходного текста." if v["state"] == "no_text"
                                             else "Вывод ещё не готов. Доступные источники приведены ниже.")
        mood = MOODS.get(analysis.get("mood"), MOODS["unknown"])
        cards.append(f'<article><div>{image}</div><div><small>{escape(date)} · {escape(mood)}</small>'
                     f'<h3>{escape(analysis.get("topic") or f"Ролик {v["id"]}")}</h3>'
                     f'<p>{escape(summary)}</p><a href="{escape(v["url"], quote=True)}">Открыть TikTok</a>'
                     f'<details><summary>Исходные данные</summary>{"".join(body)}</details></div></article>')
    notes = "".join(f"<li>{escape(x)}</li>" for x in report["notes"])
    metrics = "".join(f'<div class="metric"><strong>{count[k]}</strong>{label}</div>' for k, label in labels.items())
    summary = escape(overview.get("summary") or "Общий вывод пока отсутствует.")

    def bars(values, denominator):
        return "".join(f'<div class="legend"><small><span>{escape(label)}</span><b>{n}</b></small>'
                       f'<div class="track"><i style="width:{100*n/max(denominator,1):.1f}%"></i></div></div>'
                       for label, n in values)

    topics = bars(count["topics"].items(), count["with_text"]) or "<p>Темы ещё не определены.</p>"
    moods = bars(((MOODS[key], n) for key, n in count["moods"].items()), count["selected"])
    return (f'<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{escape(title)} · TikTok Аналитика</title>{STYLE}<main>"
            f"<header><p>Отчёт TikTok · Автор приложения {AUTHOR}</p><h1>{escape(title)}</h1><a href=\"{escape(report['profile'], quote=True)}\">Профиль</a></header>"
            f'<section class="counts">{metrics}</section><section><h2>Общий вывод</h2><p>{summary}</p>'
            f'<ul>{"".join(findings)}</ul><ul class="muted">{notes}</ul></section>'
            f'<section class="charts"><div><h2>Темы</h2>{topics}</div><div><h2>Настроение текста</h2>{moods}</div></section>'
            f'<section class="videos"><h2>Публикации</h2>{"".join(cards)}</section></main></html>').encode()


def export_excel(report):
    import xlsxwriter
    from datetime import date

    buffer = io.BytesIO()
    columns = ["ID", "TikTok", "Дата (UTC)", "Секунды", "Тема", "Настроение",
               "Вывод или фрагмент", "Субтитры / расшифровка", "Надписи", "Описание", "Состояние"]
    with xlsxwriter.Workbook(buffer, {"in_memory": True, "strings_to_formulas": False,
                                      "strings_to_urls": False}) as book:
        book.set_properties({"author": AUTHOR, "title": f"TikTok Аналитика · @{report['account']}"})
        sheet = book.add_worksheet("Публикации")
        header = book.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#372951",
                                  "valign": "vcenter", "text_wrap": True})
        dates = book.add_format({"num_format": "yyyy-mm-dd"})
        ids = book.add_format({"num_format": "@", "quote_prefix": True})
        link = book.add_format({"font_color": "#6235CC", "underline": True})
        wrapped = book.add_format({"text_wrap": True, "valign": "top"})
        sheet.hide_gridlines(2)
        sheet.freeze_panes(1, 2)
        sheet.set_row(0, 29)
        sheet.write_row(0, 0, columns, header)
        for col, width in enumerate((23, 15, 14, 10, 26, 20, 56, 55, 44, 44, 22)):
            sheet.set_column(col, col, width, wrapped if col >= 4 else None)
        for i, v in enumerate(report["videos"], 1):
            a = v.get("analysis") or {}
            sheet.write_string(i, 0, v["id"], ids)
            sheet.write_url(i, 1, v["url"], link, "Открыть")
            if v["published"]:
                sheet.write_datetime(i, 2, date.fromisoformat(v["published"]), dates)
            values = [v["duration"], a.get("topic"), MOODS.get(a.get("mood"), MOODS["unknown"]),
                      a.get("summary"), timed_captions(v["speech"]), " / ".join(v["text"]),
                      v["description"], {"complete": "Готово", "sources": "Доступны источники",
                                         "repair": "Нужен вывод Qwen", "no_text": "Недостаточно текста",
                                         "sources_error": "Неполные источники"}.get(v["state"], v["state"])]
            sheet.write_row(i, 3, [x[:32767] if isinstance(x, str) else x for x in values])
        sheet.autofilter(0, 0, max(1, len(report["videos"])), len(columns)-1)
    return buffer.getvalue()
