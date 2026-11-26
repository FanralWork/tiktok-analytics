# TikTok Analytics

A local Windows application and CLI for collecting available public TikTok posts and producing reports from captions, thumbnail text, and other available source evidence.

## Features

- Accepts a public profile URL, a post URL, or a list of direct post URLs.
- Extracts available captions, runs OCR on thumbnails, and can transcribe available media locally.
- Uses a local Qwen model through Ollama to summarize evidence and retains source links, timestamped excerpts, and review markers.
- Stores report history locally and supports continuing interrupted or incomplete processing.
- Exports standalone HTML, XLSX, CSV, Markdown, and JSON reports.

## Stack and architecture

`app/server.py` serves a loopback-only HTTP interface. The frontend uses HTML, CSS, and JavaScript. `app/service.py` manages runs and report history. `scripts/` implements discovery, media handling, recognition, evidence processing, and the SQLite-backed CLI. The Python pipeline uses yt-dlp, FFmpeg, RapidOCR with ONNX Runtime, faster-whisper, Ollama, and XlsxWriter.

## Installation and launch

Install Python 3.14 x64 and Ollama for Windows and make them available in `PATH`. Run the following commands from the project directory in PowerShell.

```powershell
.\scripts\bootstrap.ps1
.\.venv\Scripts\python.exe app\server.py --open
```

The bootstrap installs the pinned Python dependencies and obtains FFmpeg and the configured Ollama model. The first speech recognition run may download a Whisper model. Optional NVIDIA support is installed with `.\scripts\bootstrap.ps1 -EnableCUDA`. The normal Windows launcher is `Запустить TikTok Аналитику.cmd`. The default UI address is `http://127.0.0.1:8765/`. Recognition and language-model defaults are in `config/settings.json`.

## Usage

Enter the URL of a public profile you manage or paste a list of its public post URLs. Start a small analysis, inspect the recovered evidence for each card, and open the report folder. The local interface is in Russian.

The CLI can import a text file with one direct URL per line.

```powershell
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py --profile 'https://www.tiktok.com/@username' import '.\links.txt'
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py status
```

Web reports are written to `Отчёты/` and app state to `app_data/`. Keep media, inputs, and reports out of the repository. The [CLI guide](scripts/README.md) documents more commands in Russian.

## Status and limitations

Public catalogue and media availability can vary. The reports flag unconfirmed coverage and insufficient source material. Local model output and recognition are heuristic and should be checked against the evidence. A clean bootstrap, live TikTok collection, and end-to-end model execution were outside the local regression run for this review.

Author credited in this project’s original documentation · **Fanral**

## Кратко для портфолио

Локальное приложение для анализа доступных публикаций TikTok. Объединяет субтитры, OCR и распознавание речи, формирует выводы с локальной LLM и экспортирует отчёты со ссылками на источники.
