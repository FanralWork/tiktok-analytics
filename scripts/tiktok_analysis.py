import argparse
import json
import logging
import sys
from pathlib import Path

from catalog import discover, import_urls, profile_name
from exports import coverage_text, list_videos, markdown_outputs, render_xlsx
from meaning import LocalModel, summarize_batches
from pipeline import Pipeline, RunStopped, qa_ids
from state import connect, get, lock, put, remove_media, saved


PROJECT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("tiktok")


def arguments():
    parser = argparse.ArgumentParser(description="Локальный анализ TikTok")
    parser.add_argument("--root", type=Path, default=PROJECT, help="Каталог результатов")
    parser.add_argument("--config", type=Path, default=PROJECT / "config/settings.json")
    parser.add_argument("--profile", help="Публичная ссылка TikTok")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("discover")
    commands.add_parser("status")
    commands.add_parser("qa-sample")
    importer = commands.add_parser("import")
    importer.add_argument("file", type=Path)
    exporter = commands.add_parser("export")
    exporter.add_argument("--synthesize", action="store_true")
    processor = commands.add_parser("process")
    processor.add_argument("--pilot", action="store_true")
    processor.add_argument("--retry-errors", action="store_true")
    processor.add_argument("--allow-partial-catalog", action="store_true")
    processor.add_argument("--captions-only", action="store_true")
    processor.add_argument("--resume-media", action="store_true")
    processor.add_argument("--local-media", type=Path)
    processor.add_argument("--ids", nargs="+", default=[])
    processor.add_argument("--rerun-ocr", action="store_true")
    processor.add_argument("--rerun-speech", action="store_true")
    processor.add_argument("--rerun-meaning", action="store_true")
    processor.add_argument("--thorough", action="store_true")
    processor.add_argument("--cleanup-pilot", action="store_true")
    return parser.parse_args()


def export_results(db, root, cfg, synthesize=False):
    rows = list_videos(db)
    batches, error = [], None
    if synthesize:
        useful = [(row, data) for row, data in rows if data.get("content") == "informative" and
                  (row["status"] == "ANALYZED" or data.get("scope") == "captions_only")]
        if useful:
            try:
                # Keep adjacent time periods together for comparison.
                batches = summarize_batches(db, LocalModel(cfg), useful, cfg["batch_size"])
            except Exception as exc:
                error = str(exc)
                LOG.warning("Не удалось завершить синтез: %s", error)
    markdown_outputs(db, root, rows, batches, error)
    render_xlsx(root, rows)


def run():
    args = arguments()
    root = args.root.resolve()
    if args.command in ("status", "qa-sample"):
        if not (root / "database.sqlite").is_file():
            raise SystemExit("База ещё не создана")
        with connect(root, read_only=True) as db:
            if args.command == "status":
                print("\n".join(coverage_text(db, list_videos(db))))
            else:
                print(" ".join(qa_ids(db)))
        return 0
    root.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[
        logging.StreamHandler(), logging.FileHandler(root / "logs/run.log", encoding="utf-8")])
    cfg = json.loads(args.config.read_text(encoding="utf-8-sig"))
    with lock(root):
        with connect(root) as db:
            profile = args.profile or get(db, "profile") or cfg.get("profile")
            if not profile:
                raise SystemExit("Для первой команды укажите --profile перед именем команды.")
            try:
                name = profile_name(profile)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            profile = f"https://www.tiktok.com/@{name}"
            old = get(db, "profile")
            if old and old.casefold() != profile.casefold():
                raise SystemExit("Для другого профиля требуется отдельный --root")
            put(db, "profile", profile)
            if args.command == "discover":
                exact = discover(db, profile, cfg)
                export_results(db, root, cfg)
                return 0 if exact else 2
            if args.command == "import":
                import_urls(db, profile, args.file)
                export_results(db, root, cfg)
                return 0
            if args.command == "export":
                export_results(db, root, cfg, args.synthesize)
                return 0
            if not get(db, "coverage", {}).get("verified") and not (args.allow_partial_catalog or args.local_media):
                raise SystemExit("Каталог не подтверждён. После проверки списка для доступной части укажите --allow-partial-catalog.")
            pipe = Pipeline(db, root, cfg, args.local_media.resolve() if args.local_media else None)
            selected = set(args.ids)
            if args.rerun_ocr or args.rerun_speech or args.rerun_meaning:
                if not selected:
                    raise SystemExit("Для выборочной переработки укажите --ids")
                stages = ["meaning", "meaning_extended", "result"]
                if args.rerun_ocr or args.rerun_speech:
                    stages.extend(["ocr", "ocr_extended"])
                if args.rerun_speech:
                    stages.append("transcribe")
                pipe.reset(selected, stages)
            if args.retry_errors:
                pipe.retry_failed()
            if args.resume_media:
                if args.captions_only:
                    raise SystemExit("--resume-media предназначен для обработки видеофайлов")
                pipe.resume_media(selected)
            if args.cleanup_pilot:
                for key in get(db, "pilot_ids", []):
                    if saved(db, key, "result") is not None and all(
                        (root / directory / (key + ".json")).is_file() for directory in ("transcripts", "summaries")
                    ):
                        remove_media(root, key)
            completed = False
            try:
                pipe.run(pilot=args.pilot, ids=selected, thorough=args.thorough, captions_only=args.captions_only,
                         checkpoint=lambda: export_results(db, root, cfg))
                completed = True
            except (RunStopped, KeyboardInterrupt) as exc:
                LOG.warning("Пауза. %s", str(exc) or "остановка пользователем")
                return 2
            finally:
                export_results(db, root, cfg, synthesize=completed and not args.pilot and not selected)
            return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(run())
