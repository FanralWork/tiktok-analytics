import difflib
import logging
import math
import os
import re
import shutil
import subprocess
import sysconfig
import unicodedata
from collections import Counter
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

LOG = logging.getLogger("tiktok")
WATERMARK = re.compile(r"^(?:[©♪♫\s]*tik\s*tok\s*|[©♪♫\s]*@[\w.]+)$", re.I)
HALLUCINATION = re.compile(
    r"^(?:спасибо за просмотр|подписывайтесь на (?:мой )?канал|thanks for watching|"
    r"subscribe to (?:my |the )?channel|субтитры (?:создал|сделал|добавил)\b.*)$", re.I)


def normalize(text):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def alike(left, right):
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    return min(len(a), len(b)) >= 20 and difflib.SequenceMatcher(None, a, b).ratio() > 0.94


def meaningful(text):
    return any(char.isalpha() for char in text) or bool(re.search(r"\d{2,}", text))


def ffbin(name):
    if available := shutil.which(name):
        return available
    base = Path(__file__).resolve().parents[1] / ".cache" / "ffmpeg"
    return str(next(base.glob(f"*/bin/{name}.exe"), name))


def run_av(name, arguments):
    return subprocess.run([ffbin(name), *arguments], capture_output=True, check=True,
                          timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout


def probe(path):
    import json
    raw = run_av("ffprobe", ["-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    return json.loads(raw)


def select_format(info, max_height, max_bytes):
    def dimension(item):
        return min(item.get("width") or 10000, item.get("height") or 10000)

    def fits(item):
        size = item.get("filesize") or item.get("filesize_approx") or 0
        return (not item.get("has_drm") and item.get("vcodec") != "bytevc2" and size <= max_bytes
                and urlsplit(item.get("url", "")).scheme in {"http", "https"})

    formats = [item for item in info.get("formats", []) if fits(item) and item.get("vcodec") != "none"]
    combined = [item for item in formats if item.get("acodec") != "none"]
    candidates = combined or formats
    if not candidates:
        raise ValueError("Нет прямого доступного формата в пределах лимита")
    eligible = [item for item in candidates if dimension(item) <= max_height]
    candidates = eligible or candidates
    return min(candidates, key=lambda item: (
        0 if eligible and dimension(item) >= 350 else 1,
        dimension(item),
        item.get("tbr") or float("inf"),
    ))


def web_caption_tracks(video, subtitles):
    tracks = {key: [dict(choice) for choice in choices] for key, choices in (subtitles or {}).items()}
    cla = video.get("claInfo") or video.get("cla_info") or {}
    original = cla.get("originalLanguageInfo") or {}
    formats = {"webvtt": "vtt", "creator_caption": "json", "vtt": "vtt", "srt": "srt",
               "json": "json", "json3": "json3", "ttml": "ttml"}
    for item in (*(video.get("subtitleInfos") or []), *(cla.get("captionInfos") or cla.get("caption_infos") or [])):
        if not isinstance(item, dict):
            continue
        language = item.get("LanguageCodeName") or item.get("languageCode") or item.get("language") or item.get("lang") or "und"
        alternatives = item.get("urlList") or item.get("UrlList") or []
        candidates = [item.get("Url") or item.get("url")]
        if isinstance(alternatives, list):
            candidates.extend(alternatives)
        for url in candidates:
            if not isinstance(url, str) or urlsplit(url).scheme != "https":
                continue
            kind = str(item.get("Format") or item.get("captionFormat") or item.get("format") or "").lower()
            extension = formats.get(kind) or formats.get(urlsplit(url).path.rsplit(".", 1)[-1].lower()) or "auto"
            tracks.setdefault(str(language), []).append({"url": url, "ext": extension,
                "original": bool(item.get("isOriginalCaption"))})
    language = original.get("languageCode") or original.get("language") or original.get("LanguageCodeName")
    return tracks, language


def caption_candidates(info):
    def sort_lang(pair):
        lang, _ = pair
        return (0 if lang.lower().startswith("ru") else 1 if lang.lower().startswith("en") else 2, lang)

    original = str(info.get("language") or "").lower().split("-", 1)[0]
    seen = set()
    candidates = []
    for origin, key in (("platform", "subtitles"), ("platform_auto", "automatic_captions")):
        for language, choices in (info.get(key) or {}).items():
            if language.lower() == "live_chat" or "translat" in language.lower():
                continue
            for choice in choices:
                extension = choice.get("ext") or "auto"
                data = choice.get("url") or choice.get("data")
                if not data or extension not in {"json", "json3", "vtt", "srt", "ttml", "auto"}:
                    continue
                base = language.lower().split("-", 1)[0]
                is_original = choice.get("original") or "orig" in language.lower() or (original and base == original)
                priority = (not is_original, sort_lang((language, choices))[0],
                            {"vtt": 0, "srt": 1, "ttml": 2, "json": 3, "json3": 3, "auto": 4}[extension])
                candidates.append((priority, origin, language, {**choice, "ext": extension}, data))
    for _, origin, language, choice, data in sorted(candidates, key=lambda x: x[0]):
        if data not in seen:
            seen.add(data)
            yield origin, language, choice


def parse_caption(raw, ext):
    import html
    import json
    import xml.etree.ElementTree as ET

    raw = raw.lstrip("\ufeff \r\n")
    if ext == "auto":
        ext = "json" if raw.startswith(("{", "[")) else "ttml" if raw.startswith("<") else "vtt"

    def cue(start, end, text):
        text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
        return {"start": start, "end": end, "text": text}

    def clock(text):
        text = text.strip()
        if text.endswith("ms"):
            return round(float(text[:-2]) / 1000, 3)
        total = 0.0
        for part in text.strip().replace(",", ".").split(":"):
            total = total * 60 + float(part.rstrip("s"))
        return round(total, 3)

    if ext in {"json", "json3"}:
        value = json.loads(raw)
        if isinstance(value, dict) and "events" in value:
            return [cue(e["tStartMs"]/1000, (e["tStartMs"]+e.get("dDurationMs", 0))/1000,
                        "".join(x.get("utf8", "") for x in e["segs"]))
                    for e in value["events"] if e.get("segs")]
        if isinstance(value, dict):
            value = value.get("utterances") or value.get("lines") or value.get("segments") or value.get("captions") or []
        result = []
        for item in value:
            if not isinstance(item, dict):
                continue
            start = next((item[key] for key in ("start_time", "startTime", "startTimeMs") if key in item), None)
            end = next((item[key] for key in ("end_time", "endTime", "endTimeMs") if key in item), None)
            text = item.get("text") or item.get("utterance") or item.get("content")
            if start is not None and end is not None and isinstance(text, str):
                result.append(cue(float(start)/1000, float(end)/1000, text))
        return result
    if ext == "ttml":
        return [cue(clock(e.attrib["begin"]), clock(e.attrib["end"]) if "end" in e.attrib else
                    clock(e.attrib["begin"]) + clock(e.attrib["dur"]), " ".join(e.itertext()))
                for e in ET.fromstring(raw).iter() if e.tag.rsplit("}", 1)[-1] == "p"
                and "begin" in e.attrib and ("end" in e.attrib or "dur" in e.attrib)]
    result = []
    for block in re.split(r"\n\s*\n", raw.replace("\r", "")):
        lines = block.splitlines()
        for n, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->", 1)
                result.append(cue(clock(a.strip()), clock(b.split()[0]), "\n".join(lines[n+1:])))
                break
    return result


class Recognizers:
    def __init__(self, cfg, cache):
        self.cfg = cfg
        self.cache = cache
        self.device = "cpu"
        self.dll_dirs = []

    @cached_property
    def whisper(self):
        nvidia = Path(sysconfig.get_path("purelib")) / "nvidia"
        dlls = [Path(folder) for folder in self.cfg.get("cuda_dll_directories", [])]
        dlls.extend(nvidia.glob("*/bin"))
        if os.name == "nt" and self.cfg["whisper_device"] != "cpu":
            folders = list(dict.fromkeys(str(folder.resolve()) for folder in dlls if folder.is_dir()))
            self.dll_dirs.extend(os.add_dll_directory(folder) for folder in folders)
            # CTranslate2 also loads CUDA libraries explicitly by filename.
            os.environ["PATH"] = os.pathsep.join(dict.fromkeys(folders + os.environ.get("PATH", "").split(os.pathsep)))
        import ctranslate2
        from faster_whisper import WhisperModel

        settings = {"download_root": str(self.cache / "whisper"), "num_workers": 1,
                    "cpu_threads": min(6, os.cpu_count() or 4)}
        devices = ("cuda", "cpu") if self.cfg["whisper_device"] == "auto" else (self.cfg["whisper_device"],)
        for device in devices:
            try:
                if device == "cuda":
                    output = subprocess.check_output(["nvidia-smi", "--id=0",
                        "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                        text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if int(output.strip()) < 2000 or not ctranslate2.get_cuda_device_count():
                        raise RuntimeError("Недостаточно свободной VRAM или CUDA недоступна")
                model = WhisperModel(self.cfg["whisper_model"], device=device,
                                     compute_type="int8_float16" if device == "cuda" else "int8",
                                     **settings)
                self.device = device
                LOG.info("Whisper %s запущен на %s", self.cfg["whisper_model"], self.device)
                return model
            except Exception as exc:
                if device == devices[-1]:
                    raise
                LOG.warning("CUDA Whisper недоступна: %s", exc)

    @cached_property
    def ocr(self):
        from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
        return RapidOCR(params={"Rec.lang_type": LangRec(self.cfg["ocr_language"]),
                                "Rec.ocr_version": OCRVersion(self.cfg["ocr_version"]),
                                "Rec.model_type": ModelType.MOBILE,
                                "Global.text_score": 0.25, "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                                "EngineConfig.onnxruntime.inter_op_num_threads": 1})

    def transcribe(self, path, has_audio):
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        import numpy as np

        if not has_audio:
            return {"source": "no_audio", "raw": [], "clean": [], "uncertain": False}
        raw_audio = run_av("ffmpeg", ["-v", "error", "-i", str(path), "-map", "0:a:0", "-vn",
                                      "-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
        audio = np.frombuffer(raw_audio, dtype="<i2").astype(np.float32) / 32768.0
        if not audio.size:
            raise ValueError("Аудиодорожка пуста после декодирования")
        blocks = audio[:len(audio)//1600*1600].reshape(-1, 1600)
        rms = float(np.max(np.sqrt(np.mean(blocks*blocks, axis=1)))) if blocks.size else float(np.max(np.abs(audio)))
        vad = VadOptions(threshold=0.42, min_speech_duration_ms=160, min_silence_duration_ms=340, speech_pad_ms=220)
        ranges = get_speech_timestamps(audio, vad, sampling_rate=16000)
        if not ranges and rms < 0.006:
            return {"source": "vad_quiet", "raw": [], "clean": [], "uncertain": False, "rms_peak": rms}

        options = {"vad_filter": False, "beam_size": 3, "temperature": 0, "condition_on_previous_text": False,
                   "no_speech_threshold": None, "log_prob_threshold": None, "compression_ratio_threshold": None}
        if ranges:
            options["clip_timestamps"] = ",".join(str(round(x[k]/16000, 3)) for x in ranges for k in ("start", "end"))
        else:
            # Sample distributed windows when VAD is negative but the soundtrack is audible.
            length = len(audio)/16000
            starts = sorted({0, round(max(0, length/2-2), 2), round(max(0, length-4), 2)})
            intervals = []
            for start in starts:
                end = min(length, start + 5)
                if intervals and intervals[-1][1] >= start:
                    intervals[-1][1] = max(intervals[-1][1], end)
                else:
                    intervals.append([start, end])
            options["clip_timestamps"] = ",".join(str(x) for pair in intervals for x in pair)

        def infer():
            segments, info = self.whisper.transcribe(audio, **options)
            return list(segments), info

        try:
            segments, info = infer()
        except RuntimeError as exc:
            if self.device != "cuda" or self.cfg["whisper_device"] != "auto":
                raise
            LOG.warning("Ошибка CUDA на распознавании, переход на CPU: %s", exc)
            del self.whisper
            self.cfg["whisper_device"] = "cpu"
            segments, info = infer()

        raw, clean = [], []
        counts = Counter(normalize(s.text) for s in segments)
        for s in segments:
            normalized = normalize(s.text)
            overlap = sum(max(0, min(s.end, x["end"]/16000)-max(s.start, x["start"]/16000)) for x in ranges)
            confident = s.avg_logprob >= -0.9 and s.no_speech_prob < 0.65 and s.compression_ratio <= 2.5
            suspect = (HALLUCINATION.fullmatch(normalized or "") and
                       (overlap < 0.35 or counts[normalized] >= 2 or s.avg_logprob < -0.45))
            supports = (overlap >= 0.15 or (s.avg_logprob > -0.45 and s.no_speech_prob < 0.30))
            accepted = bool(confident and supports and not suspect and meaningful(s.text))
            item = {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                    "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                    "compression_ratio": round(s.compression_ratio, 3), "vad_overlap_s": round(overlap, 3),
                    "accepted": accepted}
            raw.append(item)
            if accepted and not (clean and alike(clean[-1]["text"], item["text"]) and
                                 item["start"] - clean[-1]["end"] < 1.2):
                clean.append(item)

        # Negative VAD alone never proves absence of content.
        uncertain = bool(ranges and not clean) or any(
            not s["accepted"] and s["vad_overlap_s"] > 0.35 and meaningful(s["text"]) for s in raw
        ) or bool(not ranges and raw and not clean and rms > 0.02 and any(
            s["avg_logprob"] > -1.1 and s["no_speech_prob"] < 0.8 for s in raw
        ))
        if not ranges and clean and len(audio)/16000 > 12:
            # Confirmation outside the VAD gate requires a full transcription.
            del options["clip_timestamps"]
            segments, info = infer()
            raw = [{"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                    "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                    "accepted": s.avg_logprob > -0.6 and s.no_speech_prob < 0.4
                    and s.compression_ratio < 2.5 and not HALLUCINATION.fullmatch(normalize(s.text))}
                   for s in segments if meaningful(s.text)]
            clean = [s for s in raw if s["accepted"]]
            uncertain = not clean or any(not s["accepted"] for s in raw)
        return {"source": "whisper", "device": self.device, "language": info.language,
                "language_probability": info.language_probability, "raw": raw, "clean": clean,
                "uncertain": uncertain, "vad": ranges, "rms_peak": rms}

    def _ocr_frame(self, frame):
        import cv2
        import numpy as np

        def recognize(image):
            result = self.ocr(image)
            if result.txts is None:
                return []
            return [{"text": str(text).strip(), "confidence": round(float(score), 3),
                     "box": np.asarray(box).tolist()}
                    for text, score, box in zip(result.txts, result.scores, result.boxes)]

        raw = recognize(frame)
        if raw and max(x["confidence"] for x in raw) < 0.65:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            enhanced = cv2.createCLAHE(2.0, (8, 8)).apply(gray)
            improved = recognize(cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR))
            raw.extend(improved)
        keep = []
        for item in sorted(raw, key=lambda x: -x["confidence"]):
            text = item["text"]
            if meaningful(text) and not WATERMARK.fullmatch(text) and not any(alike(text, x["text"]) for x in keep):
                keep.append(item)
        keep.sort(key=lambda item: (round(min(p[1] for p in item["box"])/18), min(p[0] for p in item["box"])))
        reliable = [x for x in keep if x["confidence"] >= 0.64]
        return raw, reliable, len(reliable) < len(keep)

    def read_screen(self, path, duration, speech, exhaustive=False):
        import cv2
        import numpy as np
        cv2.setNumThreads(2)
        if os.name == "nt":
            # Some OpenCV Windows video backends cannot open a Unicode path.
            # FFmpeg itself accepts the original path, so decode sampled frames through it.
            cap = None
        else:
            cap = cv2.VideoCapture(str(path))
        duration = max(0.1, float(duration))
        last = max(0, duration - min(0.2, duration/5))
        frames, sampled = {}, {}

        def image(t):
            t = round(min(last, max(0, t)), 2)
            if t not in frames:
                try:
                    if cap is None:
                        raw = run_av("ffmpeg", ["-v", "error", "-ss", str(t), "-i", str(path),
                            "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=720:720:force_original_aspect_ratio=decrease",
                            "-f", "image2pipe", "-vcodec", "png", "-threads", "1", "-"])
                        value = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR) if raw else None
                    else:
                        cap.set(cv2.CAP_PROP_POS_MSEC, 1000*t)
                        ok, value = cap.read()
                        if not ok:
                            value = None
                except subprocess.CalledProcessError:
                    value = None
                if value is None:
                    return t, None
                gray = cv2.cvtColor(value, cv2.COLOR_BGR2GRAY)
                clear = float(gray.std()) > 9 and float(cv2.Laplacian(gray, cv2.CV_32F).var()) > 12
                descriptor = cv2.resize(gray, (64, 64)).astype("int16")
                # Only keep compact signatures for frames sampled but not sent to OCR.
                frames[t] = (clear, descriptor)
                return t, value
            return t, None

        def read(t):
            t, value = image(t)
            if t not in sampled and value is not None and frames[t][0]:
                raw, clean, uncertain = self._ocr_frame(value)
                sampled[t] = {"time": t, "raw": raw, "lines": clean, "uncertain": uncertain}
            return "\n".join(x["text"] for x in sampled.get(t, {}).get("lines", []))

        def changed(t, previous):
            a, b = frames[t][1], frames[previous][1]
            diff = np.abs(a-b)
            return bool(diff.mean() > 12 or np.quantile(diff, 0.92) > 28)

        try:
            first = ""
            for t in (0.16, 0.5, 1.0, 1.8, 2.8, duration*0.25):
                start = round(min(t, last), 2)
                first = read(start)
                if sampled and (first or (speech and len(sampled) >= 2)):
                    break
            if not speech or not first or exhaustive:
                for t in (duration*0.48, duration*0.8, last):
                    read(t)
                observed = ["\n".join(x["text"] for x in record["lines"]) for record in sampled.values()]
                distinct = any(text and first and not alike(text, first) for text in observed)
                # Scout more often when the sampled text is absent, short or moves.
                if exhaustive or distinct or not first or len(normalize(first)) < 18 or any(not text for text in observed):
                    step = 0.8 if exhaustive else max(0.9, min(5, duration/9))
                    previous = None
                    for i in range(1, math.ceil(last/step)):
                        t, value = image(i*step)
                        if value is None or not frames[t][0]:
                            continue
                        if exhaustive or previous is None or changed(t, previous):
                            raw, clean, uncertain = self._ocr_frame(value)
                            sampled[t] = {"time": t, "raw": raw, "lines": clean, "uncertain": uncertain}
                            previous = t
            unique = []
            for item in sorted(sampled.values(), key=lambda record: record["time"]):
                lines = [line for line in item["lines"] if not any(
                         any(alike(line["text"], old["text"]) for old in earlier["lines"]) for earlier in unique)]
                if lines:
                    unique.append({"time": item["time"], "lines": lines})
            # Decode failure, blurred/black footage and low-score text need human review.
            return {"raw": sorted(sampled.values(), key=lambda item: item["time"]), "clean": unique,
                    "uncertain": not sampled or any(x["uncertain"] for x in sampled.values()),
                    "decoded_frames": len(frames), "ocr_frames": len(sampled)}
        finally:
            if cap is not None:
                cap.release()
