# Локальный анализ TikTok

## Приложение

После установки дважды щёлкните `Запустить TikTok Аналитику.cmd` в корне проекта. Откроется `http://127.0.0.1:8765/`. Для запуска вручную из той же папки используйте `.\.venv\Scripts\python.exe app\server.py --open`.

Вставьте ссылку на профиль, укажите число публикаций и запустите анализ. Обработка начинается с новых роликов. При запуске для другого аккаунта приложение запрашивает публичный каталог TikTok. Для уже сохранённого каталога можно отметить «Сверить каталог с TikTok». При отказе платформы отчёт показывает фактический охват и причину. Последние завершённые результаты остаются в разделе «Отчёты». Прерванный анализ можно продолжить оттуда.

Если публичный каталог профиля недоступен, можно проанализировать конкретную публикацию по её прямой ссылке либо вставить список прямых ссылок этого аккаунта. Каждый ролик всё равно проверяется на доступность.

Готовые субтитры и публичные превью загружаются с TikTok. RapidOCR распознаёт надписи с превью. Qwen, запущенный через местный Ollama, составляет выводы и оценивает тон текста. Облачное распознавание и отправка текстов модели в интернет не используются. Если готовых субтитров нет, карточка показывает, что удалось получить из превью и описания.

Файлы сохраняются в `Отчёты/дата_аккаунт_ID/`. Внутри находятся автономный HTML-отчёт с миниатюрами, Excel с фильтрами и ссылками, CSV, Markdown и JSON. Кнопка «Папка» в приложении открывает этот каталог. [Инструкция по установке и запуску](../README.md).

Запускайте команды из корня проекта в PowerShell после выполнения `.\scripts\bootstrap.ps1`. Версии Python-пакетов указаны в `config/requirements.lock.txt`.

## Первый запуск CLI

Укажите ссылку на свой профиль вместо `@username`. После `discover` или `import` профиль сохраняется в локальной базе, и следующие команды берут его оттуда.

```powershell
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py --profile 'https://www.tiktok.com/@username' discover
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py status
```

При остановке проверки публичных субтитров продолжите той же командой. SQLite сохраняет найденные публикации, завершённые этапы, причины отказов и разборы. Команда `status` доступна во время обработки.

```powershell
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py process --allow-partial-catalog --captions-only
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py export --synthesize
```

Для отдельного фонового процесса используйте `.\scripts\start_captions.ps1`. Его PID записывается в `logs/captions.pid`.

В режиме субтитров сохранённые реплики доступны в `transcripts`. В `summaries` лежат разборы с временными метками. При наличии только субтитров карточка остаётся в `NEEDS_REVIEW`, поскольку экранный текст не проверен. Отчёты обновляются каждые 25 записей и при завершении команды. Для промежуточного прогона смотрите `logs/run.log` и проверяйте актуальные цифры через `status`.

Если TikTok снова предоставляет легальный доступ к медиа, обработайте оставшиеся видеодорожки. Пять подряд отказов HTTP 403 останавливают массовый запрос медиа. Проверка CAPTCHA и вход в аккаунт в конвейере отсутствуют.

```powershell
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py process --allow-partial-catalog --resume-media --retry-errors
```

Конвейер принимает законно полученные файлы `<ID>.mp4`, `<ID>.mkv`, `<ID>.webm` или `<ID>.mov` из заданной папки и использует их локально. Исходные файлы из этой папки остаются на месте. При желании ограничьте выборку через `--ids`.

```powershell
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py process --local-media '.\local-videos'
```

Прямые ссылки можно импортировать из UTF-8 файла, по одной на строку. Формат JSON Lines с полями `id`, `url`, `description`, `upload_date`, `duration` также поддерживается. Полнота импорта отражает полноту исходного файла.

```powershell
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py --profile 'https://www.tiktok.com/@username' import '.\links.txt'
```

## Другой канал

```powershell
$cli = 'scripts\tiktok_analysis.py'
$python = '.\.venv\Scripts\python.exe'
$target = '..\TikTok_Analysis_other_account'
& $python $cli --root $target --profile 'https://www.tiktok.com/@other_account' discover
& $python $cli --root $target status
& $python $cli --root $target process --pilot
& $python $cli --root $target process --cleanup-pilot
& $python $cli --root $target export --synthesize
```

Если список частичный, проверьте долю найденных публикаций. Для обработки найденной части добавьте `--allow-partial-catalog` к командам `process`. Код возврата 2 от `discover` означает, что полнота не подтверждена. Кеш моделей общий для запусков из этого проекта.

## Контроль качества

```powershell
.\.venv\Scripts\python.exe scripts\test_pipeline.py
.\.venv\Scripts\python.exe scripts\tiktok_analysis.py qa-sample
```

Тест использует локальные ролики с синтетической речью, надписями, звуковой дорожкой без речи и поздней сменой текста. Тестовые данные сохраняются в `.cache`. Для выборочной перепроверки известных ID используйте `process --ids ID1 ID2 --rerun-ocr --thorough`, при необходимости с `--local-media`. Для других этапов доступны `--rerun-speech` и `--rerun-meaning`. При нехватке RAM или свободного диска конвейер сохраняет прогресс и ставит обработку на паузу.

Whisper Small распознаёт язык автоматически. RapidOCR PP-OCRv5 использует мобильную кириллическую модель для надписей с кириллицей и латиницей. Пороговые решения эвристические. Короткая тихая речь или надпись между выбранными кадрами может потребовать расширенной проверки. Транскрипты содержат исходные фрагменты и очищенные записи.

## Отчёт за период и CUDA

Для отдельного отчёта по превью используйте следующий запуск. Он сохраняет записи по одному ID и продолжает с пропущенных. Добавьте `--report-only`, чтобы собрать файл повторно из сохранённого OCR.

```powershell
$endDate = Get-Date
.\.venv\Scripts\python.exe scripts\recent.py --start ($endDate.AddMonths(-3).ToString('yyyy-MM-dd')) --end ($endDate.ToString('yyyy-MM-dd'))
```

При переносе проекта запуск `.\scripts\bootstrap.ps1 -EnableCUDA` устанавливает зафиксированные библиотеки CUDA 12 для Whisper в виртуальное окружение. Уже работающий системный драйвер NVIDIA сохраняется. Автовыбор устройства ищет совместимые DLL внутри этого окружения.
