"""Run deterministic local media fixtures. No TikTok requests or production database writes."""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from catalog import entry
from meaning import LocalModel
from pipeline import Pipeline
from recognition import ffbin
from state import connect, put, saved, upsert_video


FIXTURES = {
    "1000000000000001": ("", "speech", "speech"),
    "1000000000000002": ("ПОЛИВАЙТЕ РАСТЕНИЯ УТРОМ", "silent", "screen"),
    "1000000000000003": ("ДОБАВЬТЕ 250 МЛ ВОДЫ", "tone", "screen"),
    "1000000000000004": ("", "tone", "none"),
    "1000000000000005": ("ДЛЯ ТЕСТА НУЖНО 3 ГОРШКА", "speech", "mixed"),
    "1000000000000006": ("", "late", "screen"),
}


def ffmpeg(*args):
    subprocess.run([ffbin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], check=True)


def scene(path, text):
    image = Image.new("RGB", (540, 700), "#13304A")
    draw = ImageDraw.Draw(image)
    draw.ellipse((-180, 430, 360, 980), fill="#208456")
    draw.polygon([(0, 0), (278, 0), (0, 280)], fill="#6BBBDD")
    if text:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 32)
        draw.rounded_rectangle((10, 300, 530, 393), radius=12, fill="#FAF7E8")
        draw.text((270, 346), text, anchor="mm", font=font, fill="#152536")
    image.save(path)


def fixtures(root):
    root.mkdir(parents=True, exist_ok=True)
    script = (
        "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Speech; "
        "$s=[System.Speech.Synthesis.SpeechSynthesizer]::new(); "
        "$s.SetOutputToWaveFile([IO.Path]::Combine((Get-Location).Path,'spoken.wav')); "
        "$s.Speak('Water your plants in the morning. Use two hundred fifty milliliters of water.'); "
        "$s.Dispose()"
    )
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], cwd=root, check=True)
    late_path = root / "late.png"
    scene(late_path, "СУШИТЕ СЕМЕНА 5 ДНЕЙ")
    for key, (text, audio, _) in FIXTURES.items():
        still = root / (key + ".png")
        scene(still, text)
        options = ["-loop", "1", "-framerate", "12", "-i", str(still)]
        if audio == "speech":
            options.extend(["-i", str(root / "spoken.wav"), "-af", "apad"])
        elif audio == "tone":
            music = ("aevalsrc=0.08*exp(-4*mod(t\\,0.5))*"
                     "(sin(2*PI*220*t)+0.6*sin(2*PI*if(lt(mod(t\\,4)\\,2)\\,330\\,440)*t))"
                     ":sample_rate=16000")
            options.extend(["-f", "lavfi", "-i", music])
        elif audio == "late":
            options.extend(["-loop", "1", "-framerate", "12", "-i", str(late_path),
                            "-filter_complex", "[0:v]trim=duration=3,setpts=PTS-STARTPTS[a];"
                            "[1:v]trim=duration=5,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[v]",
                            "-map", "[v]"])
        ffmpeg(*options, "-t", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264",
               "-preset", "ultrafast", "-threads", "2", "-c:a", "aac", root / (key + ".mp4"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--recognition-only", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    cache = project / ".cache"
    cache.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="pipeline_qa_", dir=cache))
    source = root / "input"
    cfg = json.loads((project / "config/settings.json").read_text(encoding="utf-8"))
    fixtures(source)
    profile = "https://www.tiktok.com/@fixture"
    db = connect(root)
    put(db, "profile", profile)
    for key in FIXTURES:
        upsert_video(db, entry("fixture", {"id": key}, "test"))
    pipeline = Pipeline(db, root, cfg, source)
    if not args.recognition_only:
        LocalModel(cfg).check()
    output = []
    for video in db.execute("SELECT * FROM videos ORDER BY id").fetchall():
        key = video["id"]
        expected = FIXTURES[key][2]
        media = pipeline.process_media(video)
        if not media:
            errors = db.execute("SELECT stage,error FROM stages WHERE video_id=? AND state='FAILED'", (key,))
            raise AssertionError(f"{key}: {[tuple(row) for row in errors]}")
        speech, screen = media
        actual = "mixed" if speech["clean"] and screen["clean"] else "speech" if speech["clean"] else "screen" if screen["clean"] else "none"
        recognized = " ".join(line["text"] for item in screen["clean"] for line in item["lines"])
        spoken = " ".join(item["text"] for item in speech["clean"])
        print(key, expected, actual, repr(spoken), repr(recognized), flush=True)
        assert actual == expected, (key, expected, actual)
        if key.endswith("3"):
            assert "250" in recognized, key
        if key.endswith("6"):
            assert "5" in recognized and screen["clean"][0]["time"] >= 2.8, key
        if not args.recognition_only:
            pipeline.process(video)
            result = saved(db, key, "result")
            assert result and result["content"] in ({"none"} if expected == "none" else {"informative"}), key
            assert result.get("modality", "none") == expected, (key, result.get("modality"), expected)
        output.append({"id": key, "expected": expected, "actual": actual, "speech": spoken,
                       "screen": recognized, "device": pipeline.models.device})
    (root / "result.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    print(f"{len(output)} fixtures passed. {root / 'result.json'}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
