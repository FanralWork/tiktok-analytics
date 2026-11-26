const $ = (id) => document.getElementById(id);
const state = {report: null, view: "overview", page: 0, active: null, timer: null};
const PAGE = 8;
const num = new Intl.NumberFormat("ru-RU");
const day = new Intl.DateTimeFormat("ru-RU", {day:"numeric",month:"short",year:"numeric",timeZone:"UTC"});
const moment = new Intl.DateTimeFormat("ru-RU", {dateStyle:"medium",timeStyle:"short"});
const statusNames = {running:"В работе",complete:"Готов",partial:"Частичный",paused:"Приостановлен",error:"Ошибка"};
const moods = {positive:"Позитивное",neutral:"Нейтральное",negative:"Негативное",mixed:"Смешанное",unknown:"Нет оценки"};
const moodColors = {positive:"#35a97d",neutral:"#6285d9",negative:"#d16d89",mixed:"#9466df",unknown:"#a6a0b7"};
const views = {overview:"Обзор",videos:"Публикации",history:"Отчёты"};
const captionNotes = {unavailable:"TikTok не предоставил готовую дорожку субтитров.",
  unreadable:"TikTok указал дорожку субтитров, но её не удалось прочитать.",
  error:"Не удалось проверить наличие субтитров.",no_speech:"Локальное распознавание не подтвердило содержательную речь.",
  uncertain:"Результат распознавания речи требует проверки."};
function captionCues(v) { return v.speech?.raw?.length ? v.speech.raw : (v.speech?.clean || []); }
function hasCaptions(v) { return captionCues(v).some(c => c.text && c.accepted !== false); }
function plan(r) { return r.limit ?? (r.planned || r.selection?.length || null); }

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (value != null) node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}
function put(id, ...children) { $(id).replaceChildren(...children); }
function button(label, handler, cls = "button") {
  const node = el("button", {type:"button",class:cls,text:label});
  node.addEventListener("click", handler);
  return node;
}
function external(text, url, cls) {
  return el("a", {text,href:url.startsWith("https://www.tiktok.com/@") ? url : "#",target:"_blank",rel:"noreferrer",class:cls});
}
function empty(text) { return el("p", {class:"empty",text}); }
async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Локальный сервер не ответил.");
  return data;
}
function alertText(text) {
  $("form-error").textContent = text || "";
  $("form-error").hidden = !text;
}
function selectView(view) {
  state.view = view;
  document.querySelectorAll("[data-view]").forEach((node) =>
    node.setAttribute("aria-current", node.dataset.view === view ? "page" : "false"));
  $("breadcrumb").textContent = views[view];
  $("dashboard").hidden = view !== "overview";
  $("videos-section").hidden = view === "history" || !state.report && view !== "videos";
  $("history").hidden = view !== "history";
  window.scrollTo(0, 0);
}
function fmtDate(text) { return text ? day.format(new Date(text + "T00:00:00Z")).replace(".", "") : "—"; }

function timeline(days) {
  const keys = Object.keys(days).sort();
  $("activity-period").textContent = keys.length ? fmtDate(keys[0]) + " – " + fmtDate(keys.at(-1)) : "";
  if (!keys.length) return empty("Нет подтверждённых дат публикации.");
  const first = new Date(keys[0] + "T00:00:00Z").getTime();
  const last = new Date(keys.at(-1) + "T00:00:00Z").getTime();
  const unit = 86400000;
  const length = 1 + Math.round((last - first) / unit);
  const step = Math.max(1, Math.ceil(length / 25));
  const points = new Array(Math.ceil(length / step)).fill(0);
  keys.forEach(key => {
    const offset = Math.round((new Date(key + "T00:00:00Z").getTime() - first) / unit);
    points[Math.floor(offset / step)] += days[key];
  });
  const chart = el("div", {class:"bars",role:"img","aria-label":"Публикаций по интервалам: " + points.join(", ")});
  chart.style.setProperty("--columns", points.length);
  const max = Math.max(...points, 1);
  points.forEach((value, i) => {
    const date = new Date(first + i * step * unit).toISOString().slice(0, 10);
    const end = new Date(Math.min(last, first + (i * step + step - 1) * unit)).toISOString().slice(0, 10);
    const label = date === end ? fmtDate(date) : fmtDate(date) + " – " + fmtDate(end);
    const bar = el("span", {class:"bar",title:label + " · " + num.format(value)});
    bar.style.setProperty("--height", 100 * value / max + "%");
    chart.append(bar);
  });
  return el("div", {}, [chart, el("div", {class:"axis"}, [el("span", {text:fmtDate(keys[0])}),el("span", {text:fmtDate(keys.at(-1))})])]);
}
function legend(entries, total) {
  return el("div", {class:"legend"}, entries.map(([label, value, color]) => {
    const track = el("div", {class:"track"}, [el("i")]);
    track.style.setProperty("--pct", Math.min(100, value / Math.max(1, total) * 100) + "%");
    track.style.setProperty("--color", color);
    return el("div", {class:"legend-row"}, [el("span", {text:label}), el("strong", {text:num.format(value)}), track]);
  }));
}
function renderCharts(s) {
  put("activity", timeline(s.days));
  put("coverage", legend([["Субтитры и расшифровки",s.captions,"#49a981"],["Превью",s.previews,"#8c6ef0"],
    ["Надписи на превью",s.ocr,"#6285d9"],["Текст хотя бы из одного источника",s.with_text,"#ac72c4"]],s.selected));
  const topics = Object.entries(s.topics).slice(0, 8);
  put("topics", topics.length ? legend(topics.map(([key,count]) => [key,count,"#9276eb"]), s.with_text)
    : empty("Темы появятся после получения текста."));
  const assessed = s.selected - (s.moods.unknown || 0);
  const ranked = Object.entries(s.moods).filter(([key]) => key !== "unknown").sort((a,b) => b[1] - a[1]);
  if (!s.selected) put("moods", empty("Оценки появятся после анализа."));
  else put("moods",
    legend(Object.entries(moods).map(([key,label]) => [label,s.moods[key] || 0,moodColors[key]]),s.selected),
    el("p", {class:"mood-caption",text:assessed && ranked[0][1] > (ranked[1]?.[1] || 0)
      ? "Чаще всего " + moods[ranked[0][0]].toLowerCase() + " · оценено " + assessed + " из " + s.selected
      : "Оценено " + assessed + " из " + s.selected}));
}
function renderExports(report) {
  const route = "/api/reports/" + report.id;
  const items = [];
  if (report.files.includes("report.html"))
    items.push(el("a", {class:"button",href:route + "/report.html",text:"HTML-отчёт",target:"_blank",rel:"noreferrer"}));
  if (report.files.includes("videos.xlsx")) items.push(el("a", {class:"button",href:route + "/videos.xlsx",text:"Excel"}));
  else if (report.files.includes("videos.csv")) items.push(el("a", {class:"button",href:route + "/videos.csv",text:"CSV"}));
  if (report.folder) {
    const open = button("Папка", async () => {
      try { await api(route + "/folder", {}); } catch (error) { alertText(error.message); }
    });
    open.title = report.folder;
    items.push(open);
  }
  if (["paused","partial","error"].includes(report.state))
    items.push(button("Продолжить", async () => {
      try {
        alertText("");
        const info = await api(route + "/resume", {});
        await openReport(info.id);
        beginPolling(info.id);
      } catch (error) { alertText(error.message); }
    }));
  items.push(deleteButton(report));
  put("exports", ...items);
}

function deleteButton(report) {
  const remove = button("Удалить", async () => {
    if (!window.confirm("Удалить отчёт @" + report.account + " из истории и папки «Отчёты»?")) return;
    remove.disabled = true;
    alertText("");
    try {
      await api("/api/reports/" + report.id + "/delete", {});
      if (state.report?.id === report.id) {
        state.report = null;
        state.page = 0;
        $("video-dialog").close();
        if (!state.active) $("progress").hidden = true;
        render();
      }
      await loadHistory();
    } catch (error) {
      alertText(error.message);
      remove.disabled = false;
    }
  }, "button danger");
  remove.disabled = report.state === "running";
  if (remove.disabled) remove.title = "Отчёт можно удалить после окончания анализа.";
  return remove;
}

function render() {
  const r = state.report;
  $("dashboard-content").hidden = !r;
  $("videos-section").hidden = state.view === "history" || !r && state.view !== "videos";
  if (!r) {
    $("account-title").textContent = "Отчётов пока нет";
    $("data-label").textContent = "";
    $("scope").textContent = "Вставьте ссылку, чтобы создать отчёт.";
    $("nav-count").textContent = "";
    put("exports");
    renderVideos();
    return;
  }
  const s = r.stats;
  $("account-title").textContent = "@" + r.account;
  $("data-label").textContent = r.source;
  const dates = Object.keys(s.days).sort();
  const target = plan(r);
  const coverage = r.all_posts && !r.direct?.length ? (r.catalog_complete ? "весь доступный каталог" : "полнота каталога не подтверждена") : "";
  $("scope").textContent = (dates.length ? fmtDate(dates[0]) + " – " + fmtDate(dates.at(-1)) + " · " : "") +
    "Охвачено " + num.format(s.selected) + (target ? " из " + num.format(target) : "") +
    (coverage ? " · " + coverage : "") + (r.reused ? " · из кеша " + num.format(r.reused) : "") +
    (r.pending ? " · требует доработки " + num.format(r.pending) : "");
  $("nav-count").textContent = num.format(s.selected);
  $("overview-origin").textContent = !r.overview ? "" : r.id === "archive" ? "Из архива" :
    r.overview.method === "statistics" ? "По сохранённым карточкам" : "Qwen · локально";
  $("overview-origin").hidden = !r.overview;
  renderExports(r);
  put("metrics", ...[
    ["▤","Публикации",s.selected],["≡","Субтитры",s.captions],["▧","Превью",s.previews],["Т","Надписи на превью",s.ocr],
  ].map(([icon,label,n]) => el("div", {class:"panel metric"}, [
    el("span", {class:"metric-icon","aria-hidden":"true",text:icon}),
    el("div", {}, [el("strong", {text:num.format(n)}),el("span", {text:label})]),
  ])));
  renderCharts(s);
  if (r.overview) {
    const index = new Map(r.videos.map(v => [v.id,v]));
    const points = (r.overview.findings || []).map(point => el("li", {text:point.text}, point.ids.flatMap((id,i) => {
      const v = index.get(id);
      return v ? [external("Ролик" + (point.ids.length > 1 ? " " + (i + 1) : ""),v.url,"finding-link")] : [];
    })));
    put("summary", el("p", {text:r.overview.summary}),el("ul", {class:"findings"},points));
  } else {
    put("summary", empty(r.state === "running" ? "Сводка появится после анализа доступного текста."
      : "Для общего вывода нужен обработанный текст из роликов."));
  }
  $("notes").hidden = !r.notes.length;
  if (r.state === "error" && !s.selected) $("notes").open = true;
  put("notes-list", ...r.notes.map(note => el("li", {text:note})));
  renderVideos();
}
function titleFor(v) { return v.analysis?.topic || v.description.split("#",1)[0].trim() || v.text[0] || "Ролик TikTok"; }
function selectedVideos() {
  const term = $("search").value.trim().toLocaleLowerCase();
  const source = $("source-filter").value;
  return (state.report?.videos || []).filter(v => {
    if (source === "captions" && !hasCaptions(v) || source === "ocr" && !v.text.length ||
      source === "summary" && !v.analysis || source === "missing" && (hasCaptions(v) || v.text.length)) return false;
    return !term || [v.id,v.description,v.analysis?.summary,v.analysis?.topic,...v.text,
      ...captionCues(v).map(cue => cue.text)].some(value => value?.toLocaleLowerCase().includes(term));
  });
}
function renderVideos() {
  const rows = selectedVideos();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  state.page = Math.min(state.page, pages - 1);
  const start = state.page * PAGE;
  $("listing-count").textContent = "Показано " + (rows.length ? start + 1 : 0) + "–" + Math.min(start + PAGE,rows.length) + " из " + rows.length;
  put("video-list", ...rows.slice(start,start + PAGE).map(v => {
    const tags = [];
    if (hasCaptions(v)) tags.push(el("span", {class:"tag green",text:v.caption_status === "transcribed" ? "Расшифровка" : "Субтитры"}));
    if (v.text.length) tags.push(el("span", {class:"tag",text:"OCR"}));
    else if (v.preview) tags.push(el("span", {class:"tag gray",text:"Превью"}));
    if (!tags.length) tags.push(el("span", {class:"tag gray",text:v.description ? "Описание" : "Нет текста"}));
    const image = v.preview ? el("img", {src:v.preview,alt:"",loading:"lazy"}) : el("span", {class:"thumb-placeholder"});
    const text = v.analysis?.summary || v.text[0] || v.description || v.id;
    const title = el("div", {class:"video-name"}, [image,el("div", {}, [
      el("strong", {text:titleFor(v)}),el("small", {text,title:text})])]);
    return el("tr", {}, [el("td", {}, [title]),el("td", {text:fmtDate(v.published)}),
      el("td", {}, [el("div", {class:"tags"},tags)]),el("td", {text:moods[v.analysis?.mood] || moods.unknown}),
      el("td", {}, [button("Открыть", () => openVideo(v), "button row-action")])]);
  }));
  if (!rows.length) put("video-list",el("tr", {}, [el("td", {colspan:"5"}, [empty("Публикации не найдены.")])]));
  const back = button("Назад", () => { state.page--; renderVideos(); });
  const forward = button("Далее", () => { state.page++; renderVideos(); });
  back.disabled = state.page === 0;
  forward.disabled = state.page + 1 >= pages;
  put("pages", back,el("span", {text:(state.page + 1) + " / " + pages}),forward);
}
function block(title, text, note) {
  const content = [el("summary", {text:title}),el("div", {text})];
  if (note) content.push(el("small", {text:note}));
  return el("details", {class:"source-block"},content);
}
function openVideo(v) {
  $("dialog-title").textContent = titleFor(v);
  const a = v.analysis;
  const grouped = [el("div", {}, v.preview ? [el("img", {src:v.preview,alt:"Превью ролика"})] : []),
    el("div", {}, [el("small", {text:[v.published ? fmtDate(v.published) : "Дата неизвестна",v.duration ? v.duration + " с" : "",moods[a?.mood]].filter(Boolean).join(" · ")}),
      el("p", {text:a?.summary || (v.state === "no_text" ? "Для вывода недостаточно исходного текста." : "Вывод ещё не готов. Доступные источники приведены ниже.")}),
      ...(a?.method === "excerpt" ? [el("small", {text:"Фрагмент источника · вывод Qwen требует повторной обработки"})] : []),
      external("Открыть в TikTok",v.url)])];
  const cues = captionCues(v).filter(cue => cue.text).map(cue => {
    const sec = Math.max(0,Math.floor(cue.start || 0));
    return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2,"0") + "  " + cue.text +
      (cue.accepted === false ? " · требует проверки" : "");
  }).join("\n");
  const subtitles = block(v.caption_status === "transcribed" ? "Расшифровка речи" : "Субтитры",cues ||
    captionNotes[v.caption_status] || "Доступная дорожка субтитров не найдена.",v.speech?.language);
  subtitles.open = Boolean(cues);
  const content = [el("div", {class:"video-overview"},grouped),
    subtitles,
    block("Надписи на превью",v.text.join("\n") || "Надписи не распознаны."),
    block("Описание публикации",v.description || "Описание отсутствует.")];
  if (a?.evidence?.length) content.push(block("Фрагменты, на которых основан вывод",a.evidence.join("\n")));
  if (v.errors.length) content.push(block("Ошибки получения данных",v.errors.join("\n")));
  put("dialog-content", ...content);
  $("video-dialog").showModal();
}

async function loadHistory() {
  const {reports} = await api("/api/reports");
  const items = reports.map(r => el("div", {class:"history-row"}, [
    el("div", {}, [el("strong", {text:"@" + r.account}),
      el("div", {}, [el("small", {text:[r.created ? moment.format(new Date(r.created)) : r.source,
        statusNames[r.state], r.processed + (plan(r) ? "/" + plan(r) : ""), r.all_posts ? "Все публикации" : ""].filter(Boolean).join(" · ")})])]),
    el("div", {class:"actions"}, [
      button("Открыть", async () => {
        try { await openReport(r.id); selectView("overview"); } catch (error) { alertText(error.message); }
      }),
      deleteButton(r),
    ]),
  ]));
  put("history-list", ...(items.length ? items : [empty("Сохранённых отчётов ещё нет.")]));
  return reports;
}
async function openReport(id, polling = false) {
  const next = await api("/api/reports/" + id);
  if (!polling || state.report?.id === id) {
    if (state.report?.id !== id) state.page = 0;
    state.report = next;
    render();
  }
  return next;
}
function setBusy(busy) {
  $("run-button").disabled = busy;
  $("run-all").disabled = busy;
}
function beginPolling(id, onFinish) {
  state.active = id;
  setBusy(true);
  $("progress").hidden = false;
  if (state.timer) clearTimeout(state.timer);
  async function tick() {
    try {
      const r = await openReport(id,true);
      const target = plan(r);
      const status = [el("strong", {text:r.stage || statusNames[r.state]}),
        el("p", {text:target ? "Сохранено " + r.processed + " из " + target +
          (r.pending ? " · требует доработки " + r.pending : "") : "Размер каталога уточняется"}),
        el("progress", {value:target ? r.processed : null,max:target,"aria-label":"Ход анализа"})];
      if (r.state === "error" && !r.processed) {
        status.push(...r.notes.slice(-3).map(note => el("p", {text:note})));
        status.push(button("Вставить прямые ссылки", () => {
          $("direct").closest("details").open = true;
          $("direct").focus();
        }));
      }
      put("progress", ...status);
      if (r.state === "running") state.timer = setTimeout(tick,1500);
      else {
        state.active = null;
        state.timer = null;
        setBusy(false);
        if (r.state === "complete") $("progress").hidden = true;
        onFinish?.(r.state);
        onFinish = undefined;
        await loadHistory();
      }
    } catch (error) {
      alertText(error.message);
      state.timer = setTimeout(tick,3000);
    }
  }
  tick();
}
function step(by) { $("limit").value = Math.max(1,(Number($("limit").value) || 1) + by); }
$("minus").addEventListener("click", () => step(-1));
$("plus").addEventListener("click", () => step(1));
document.querySelectorAll("[data-view]").forEach(node => node.addEventListener("click", () => selectView(node.dataset.view)));
for (const id of ["search","source-filter"]) $(id).addEventListener("input", () => { state.page = 0; renderVideos(); });
$("analysis-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.active) return;
  alertText("");
  const profile = $("profile");
  const direct = $("direct");
  if (!profile.reportValidity()) return;
  const url = profile.value;
  const links = direct.value;
  const allPosts = event.submitter?.id === "run-all";
  setBusy(true);
  try {
    const report = await api("/api/reports", {url,limit:Number($("limit").value),
      all_posts:allPosts,refresh:$("refresh").checked,links});
    await openReport(report.id);
    selectView("overview");
    beginPolling(report.id, status => {
      if ((status === "complete" || status === "partial") && direct.value === links) {
        direct.value = "";
        if (profile.value === url) profile.value = "";
      }
    });
  } catch (error) { alertText(error.message); setBusy(false); }
});
(async function initialize() {
  try {
    const reports = await loadHistory();
    if (reports.length) {
      await openReport(reports[0].id);
      const active = reports.find(r => r.state === "running");
      if (active) beginPolling(active.id);
    } else render();
  } catch (error) { alertText(error.message); }
})();
