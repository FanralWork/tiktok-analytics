import contextlib
import ctypes
import json
import sqlite3
import time
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS configuration(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, url TEXT NOT NULL, description TEXT, published TEXT, duration REAL,
  pinned INTEGER, availability TEXT, source TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'DISCOVERED', last_error TEXT,
  updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS discovery(
  id INTEGER PRIMARY KEY, started TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished TEXT, outcome TEXT, ids TEXT, expected_count INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS stages(
  video_id TEXT REFERENCES videos(id), stage TEXT, state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, data TEXT, error TEXT,
  updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(video_id, stage)
);
CREATE TABLE IF NOT EXISTS batch_summaries(
  fingerprint TEXT PRIMARY KEY, ids TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS videos_status ON videos(status);
"""


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def atomic(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(value, encoding="utf-8")
    replace_file(tmp, path)


def replace_file(source, target):
    # Windows readers and antivirus can briefly hold a handle that excludes replacement.
    for delay in (0.03, 0.08, 0.18, 0.35, 0.75):
        try:
            source.replace(target)
            return
        except PermissionError:
            time.sleep(delay)
    source.replace(target)


@contextlib.contextmanager
def lock(root):
    import msvcrt
    with (root / ".run.lock").open("a+b") as handle:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise SystemExit("Для этой базы уже выполняется другая команда.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def memory_gib():
    status = (ctypes.c_ulonglong * 8)()
    status[0] = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("Не удалось измерить свободную RAM")
    return status[2] / 2**30


def connect(root, read_only=False):
    if read_only:
        db = sqlite3.connect((root / "database.sqlite").as_uri() + "?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db
    for folder in ("logs", "transcripts", "summaries", "media", ".cache"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "database.sqlite", timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db


def get(db, key, default=None):
    row = db.execute("SELECT value FROM configuration WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    with db:
        db.execute("INSERT OR REPLACE INTO configuration VALUES (?,?)", (key, encode(value)))


def upsert_video(db, video):
    with db:
        db.execute("""
        INSERT INTO videos(id,url,description,published,duration,pinned,availability,source)
        VALUES (:id,:url,:description,:published,:duration,:pinned,:availability,:source)
        ON CONFLICT(id) DO UPDATE SET
          description=COALESCE(excluded.description,videos.description),
          published=COALESCE(excluded.published,videos.published),
          duration=COALESCE(excluded.duration,videos.duration),
          pinned=COALESCE(excluded.pinned,videos.pinned),
          availability=COALESCE(excluded.availability,videos.availability)
        """, video)


def journal(db, video_id, stage, state, *, data=None, error=None, status=None, attempt=False):
    with db:
        db.execute("""
        INSERT INTO stages(video_id,stage,state,attempts,data,error) VALUES (?,?,?,?,?,?)
        ON CONFLICT(video_id,stage) DO UPDATE SET state=excluded.state,
          attempts=stages.attempts+excluded.attempts,data=excluded.data,error=excluded.error,
          updated=CURRENT_TIMESTAMP
        """, (video_id, stage, state, int(attempt), encode(data) if data is not None else None, error))
        if status:
            db.execute("UPDATE videos SET status=?,last_error=?,updated=CURRENT_TIMESTAMP WHERE id=?",
                       (status, error, video_id))


def saved(db, video_id, stage):
    row = db.execute("SELECT * FROM stages WHERE video_id=? AND stage=?",
                     (video_id, stage)).fetchone()
    return json.loads(row["data"]) if row and row["state"] == "DONE" else None


def remove_media(root, video_id):
    for path in (root / "media").glob(f"{video_id}.*"):
        if path.is_file():
            path.unlink(missing_ok=True)
