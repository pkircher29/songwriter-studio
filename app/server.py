"""Houseband — desktop app: drop a demo, pick a style, hear your song played by a full band.

The heavy lifting (melody transcription, lyric transcription, YuE2) runs on
a GPU you bring: your own Modal account through chuck-gpu (HOUSEBAND_ENGINE=modal,
the default) or an NVIDIA GPU on this machine (HOUSEBAND_ENGINE=local). This
process only queues jobs, stages their files, starts the worker, and serves
the UI and the results.

    data/songs/<hash>/source.<ext>      the song as uploaded
    data/songs/<hash>/song.json         title, analysis once known, takes
    data/songs/<hash>/takes/*.mp3|flac  every cover made of it
    data/cloud/                         the one chuck-gpu workspace:
        chuck-gpu.toml, worker/         (copied from ../worker on each job)
        songs/<hash>.<ext>              uploaded once per song, reused
        jobs/<job>/job.json, out/       per job; out/ comes back
        cache/                          models + analyses, cloud-only
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
import lyrics as lyrics_db  # noqa: E402
import styles  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("HOUSEBAND_DATA", ROOT / "data"))
SONGS = DATA / "songs"
# Where the GPU work runs. "modal": your own Modal account via chuck-gpu (the
# worker image is built from worker_yue/chuck-gpu.toml). "local": the same
# worker script on this machine, Linux/WSL2 with an NVIDIA 24 GB GPU, using
# the Python env you built for it (see README "Run on your own GPU").
ENGINE = os.environ.get("HOUSEBAND_ENGINE", "modal")
LOCAL_PYTHON = os.environ.get("HOUSEBAND_YUE_PYTHON", "/opt/yue/bin/python")
# YuE2 engine: worker_yue/ runs in the data/cloud-yue chuck-gpu workspace.
# (worker/ + data/cloud is the earlier ACE-Step engine, kept for its sweep.)
CLOUD = DATA / "cloud-yue"
WORKER = ROOT / "worker_yue"
STATIC = Path(__file__).parent / "static"
DEFAULT_GPU = "L40S"   # YuE2 wants 24 GB + bf16; L40S is the cheapest with headroom
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
MAX_BYTES = 200 * 1024 * 1024


def chuck_gpu_exe() -> str:
    exe = Path(sys.executable).with_name("chuck-gpu.exe" if os.name == "nt" else "chuck-gpu")
    return str(exe) if exe.exists() else (shutil.which("chuck-gpu") or "chuck-gpu")


def chuck_gpu_env() -> dict:
    """Environment for a `chuck-gpu run` child.

    Hosted on Modal (deploy/modal_app.py) the app runs inside a Modal
    container, where the modal client ignores MODAL_TOKEN_* and talks to the
    container-local API instead, while chuck-gpu refuses to start without a
    token. With the token from the cover-studio-modal-token secret, the child
    runs as an ordinary client against the public API: the same path as on a
    laptop, so nothing depends on container credentials reaching a child."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if env.get("MODAL_IS_REMOTE") == "1" and env.get("MODAL_TOKEN_ID") and env.get("MODAL_TOKEN_SECRET"):
        env.pop("MODAL_IS_REMOTE")
        env["MODAL_SERVER_URL"] = "https://api.modal.com"
    return env


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            if attempt == 19:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


# ── songs ───────────────────────────────────────────────────────────────────

def song_dir(song_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{16}", song_id or ""):
        raise HTTPException(404, "no such song")
    d = SONGS / song_id
    if not (d / "song.json").exists():
        raise HTTPException(404, "no such song")
    return d


def song_view(d: Path) -> dict:
    meta = read_json(d / "song.json", {})
    an = meta.get("analysis") or {}
    return {
        "id": d.name,
        "title": meta.get("title", d.name),
        "source": next((f.name for f in d.glob("source.*")), None),
        "added": meta.get("added"),
        "duration": an.get("duration") or meta.get("duration"),
        "analysis": {"bpm": an.get("bpm"), "key": an.get("key"), "language": an.get("language"),
                     "genre": styles.genres().get(an.get("genre"))}
                    if an else None,
        "has_score": (d / "score.abc").exists(),   # melody score, kept after the first cover (Melody fit)
        "lyrics": meta.get("lyrics") or None,
        "lyrics_synced": meta.get("lyrics_synced") or [],
        "lyrics_source": meta.get("lyrics_source"),
        "takes": sorted(meta.get("takes", []), key=lambda t: (-int(bool(t.get("starred"))), -t.get("made", 0))),
    }


# ── jobs: one at a time, in order ───────────────────────────────────────────

STEP_RE = re.compile(r"\[cover\] step (\d)/4: (.*)")


class Job:
    def __init__(self, song_id: str, take_list: list[dict], gpu: str | None, lyrics: str | None,
                 singer: str = ""):
        self.id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.song_id = song_id
        self.takes = take_list
        self.gpu = gpu
        self.lyrics = lyrics
        self.singer = singer
        self.delivered: set[str] = set()   # take ids already filed under the song
        self.state = "queued"      # queued → running → done | failed | cancelled
        self.phase = "Waiting for the GPU queue"
        self.step = 0
        self.error = None
        self.created = time.time()
        self.started = None
        self.ended = None
        self.log: list[str] = []
        self.proc: subprocess.Popen | None = None
        self.sandbox = None
        self.cancel_requested = False

    def view(self) -> dict:
        return {"id": self.id, "song_id": self.song_id, "styles": [t["name"] for t in self.takes],
                "style_ids": [t["id"] for t in self.takes],
                "state": self.state, "phase": self.phase, "step": self.step, "error": self.error,
                "created": self.created, "started": self.started, "ended": self.ended,
                "log": self.log[-60:]}

    def note(self, line: str) -> None:
        self.log.append(line)
        if line.startswith("[cover] "):
            text = line[8:]
            m = STEP_RE.match(line)
            if m:
                self.step, text = int(m.group(1)), m.group(2)
            elif text.startswith("steps 2-3"):
                self.step = 3
            if not text.startswith(("GPU:", "heard:")):
                self.phase = text[:1].upper() + text[1:]
        elif "[chuck-gpu]" in line:
            m = re.search(r"sandbox (sb-\w+)", line)
            if m:
                self.sandbox = m.group(1)
            if "uploading" in line:
                self.phase = "Uploading the song"
            elif "starting" in line and "sandbox" in line:
                self.phase = "Starting a cloud GPU (usually under a minute)"
            elif "downloading" in line:
                self.phase = "Downloading the finished cover"


JOBS: list[Job] = []
LOCK = threading.Lock()
WAKE = threading.Event()
QUEUE_FILE = DATA / "queue.json"


def save_queue() -> None:
    """Persist queued jobs so restarting the server never drops waiting covers."""
    with LOCK:
        rows = [{"id": j.id, "song_id": j.song_id, "takes": j.takes, "gpu": j.gpu,
                 "lyrics": j.lyrics, "singer": j.singer, "created": j.created}
                for j in JOBS if j.state == "queued"]
    write_json(QUEUE_FILE, rows)


def stage(job: Job) -> str:
    """Write the workspace for `job`; return its path relative to the workspace."""
    CLOUD.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WORKER / "chuck-gpu.toml", CLOUD / "chuck-gpu.toml")
    (CLOUD / "worker").mkdir(exist_ok=True)
    for f in WORKER.iterdir():
        if f.suffix == ".py":
            shutil.copy2(f, CLOUD / "worker" / f.name)
    sd = SONGS / job.song_id
    src = next(sd.glob("source.*"))
    staged = CLOUD / "songs" / f"{job.song_id}{src.suffix.lower()}"
    if not staged.exists():
        staged.parent.mkdir(exist_ok=True)
        shutil.copy2(src, staged)
    rel = f"jobs/{job.id}"
    meta = read_json(sd / "song.json", {})
    spec = {"source": f"songs/{staged.name}", "takes": job.takes,
            "lyrics": job.lyrics or meta.get("lyrics") or "", "singer": job.singer,
            "lyrics_synced": [] if job.lyrics else (meta.get("lyrics_synced") or []),
            "genres": styles.genres(), "styles_by_id": styles.templates()}
    write_json(CLOUD / rel / "job.json", spec)
    return rel


SONG_LOCK = threading.Lock()


def add_take(job: Job, t: dict, mp3: Path, flac: Path | None) -> None:
    """File one finished take under its song (moves the audio in)."""
    sd = SONGS / job.song_id
    (sd / "takes").mkdir(exist_ok=True)
    base = f"{t['id']}-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(mp3, sd / "takes" / f"{base}.mp3")
    if flac is not None and flac.exists():
        shutil.move(flac, sd / "takes" / f"{base}.flac")
    with SONG_LOCK:
        meta = read_json(sd / "song.json", {})
        meta.setdefault("takes", []).append({
            "file": f"{base}.mp3", "flac": f"{base}.flac", "style": t["id"],
            "name": styles.get(t["id"])[1], "strength": t.get("cfg"), "seed": t.get("seed"),
            "caption": t.get("style"), "made": time.time(), "engine": "yue2",
            "timing": t.get("timing"), "seconds": t.get("seconds"), "starred": False,
        })
        write_json(sd / "song.json", meta)
    job.delivered.add(t["id"])


def _out_dir(rel: str):
    """(list_names, read_bytes) for the job's out/ folder: on the Modal volume,
    or on this disk when the worker runs here."""
    if ENGINE == "local":
        out = CLOUD / rel / "out"
        return (lambda: [p.name for p in out.iterdir()] if out.is_dir() else []), (lambda n: (out / n).read_bytes())
    import modal
    from chuck_gpu.cli import VOLUME_NAME, workspace_key
    vol, remote = modal.Volume.from_name(VOLUME_NAME), f"/{workspace_key(CLOUD)}/{rel}/out"
    return (lambda: [Path(e.path).name for e in vol.listdir(remote)]), (lambda n: b"".join(vol.read_file(f"{remote}/{n}")))


def fetch_ready(job: Job, rel: str) -> None:
    """File every take the job has finished that isn't filed yet.

    The worker writes <id>.done.json (with sizes) and syncs after each take,
    so this can run while the job is still going (each take arrives as soon
    as it's made) and after a job died (nothing finished is lost)."""
    try:
        list_names, read_bytes = _out_dir(rel)
        names = list_names()
    except Exception:
        return
    tmp = CLOUD / rel / "early"
    for done in (n for n in names if n.endswith(".done.json")):
        tid = done[:-len(".done.json")]
        if tid in job.delivered:
            continue
        try:
            rec = json.loads(read_bytes(done))
            tmp.mkdir(parents=True, exist_ok=True)
            got = {}
            for kind in ("mp3", "flac"):
                dest = tmp / rec[kind]
                dest.write_bytes(read_bytes(rec[kind]))
                got[kind] = dest
            if got["mp3"].stat().st_size != rec["mp3_size"] or got["flac"].stat().st_size != rec["flac_size"]:
                continue   # not fully committed yet; next poll
            add_take(job, rec, got["mp3"], got["flac"])
            job.note(f"[cover] {styles.get(tid)[1]} is ready")
        except Exception:
            continue


def fetch_early(job: Job, rel: str, stop: threading.Event) -> None:
    while not stop.wait(8):
        fetch_ready(job, rel)


def collect(job: Job, rel: str) -> None:
    """File whatever the end-of-run download brought that wasn't fetched early."""
    out = CLOUD / rel / "out"
    result = read_json(out / "result.json", {}) or {}
    sd = SONGS / job.song_id
    meta = read_json(sd / "song.json", {})
    an = read_json(out / "analysis.json")
    if an:
        meta["analysis"] = an
        if not meta.get("lyrics") and an.get("lyrics"):   # nothing found online: Whisper's
            meta["lyrics"], meta["lyrics_source"] = an["lyrics"], "transcribed from the song"
    with SONG_LOCK:
        write_json(sd / "song.json", meta)
    if (out / "score.abc").exists():   # the melody the covers sing; the lyrics editor compares words to it
        shutil.copyfile(out / "score.abc", sd / "score.abc")
    for t in result.get("takes", []):
        if t["id"] not in job.delivered and (out / t["mp3"]).exists():
            add_take(job, t, out / t["mp3"], out / t["flac"])
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "the cloud job failed without saying why")
    shutil.rmtree(CLOUD / rel, ignore_errors=True)


def run_job(job: Job) -> None:
    """Start the cloud job as its OWN process (output to a log file, not a
    pipe into this one), so restarting the app can't kill it: a restart on
    2026-10-12 killed a 13-style batch after its first cover. The process,
    its log and run.json live in the job's folder; adopt_orphans() picks
    them back up when the app starts."""
    job.state, job.started = "running", time.time()
    job.phase = "Preparing"
    try:
        rel = stage(job)
        if ENGINE == "local":
            argv = [LOCAL_PYTHON, "-u", "worker/yue_cover.py", rel]
        else:
            argv = [chuck_gpu_exe(), "run", "--dir", str(CLOUD)]
            if job.gpu:
                argv += ["--gpu", job.gpu]
            argv += ["--", "/opt/yue/bin/python", "-u", "worker/yue_cover.py", rel]
        env = chuck_gpu_env()
        # CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        flags = 0x08000000 | 0x00000008 | 0x00000200 if os.name == "nt" else 0
        with open(CLOUD / rel / "run.log", "wb") as log:
            job.proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        env=env, creationflags=flags, close_fds=True,
                                        cwd=str(CLOUD) if ENGINE == "local" else None,
                                        start_new_session=os.name != "nt")
        write_json(CLOUD / rel / "run.json", {
            "pid": job.proc.pid, "host": socket.gethostname(), "id": job.id, "song_id": job.song_id,
            "takes": job.takes, "singer": job.singer, "gpu": job.gpu, "created": job.created, "started": job.started})
        follow(job, rel)
    except Exception as e:
        job.state, job.error, job.phase = "failed", str(e)[:800], "Failed"
        job.ended = time.time()


def follow(job: Job, rel: str) -> None:
    """Tail the job's log into its progress until the process ends, then file results."""
    from chuck_gpu.jobs import pid_alive
    log = CLOUD / rel / "run.log"
    info = read_json(CLOUD / rel / "run.json", {})
    pid = job.proc.pid if job.proc else info.get("pid")
    # a pid from another machine/container (hosted: the container was
    # replaced) is dead here, even if some other process now has that number
    here = info.get("host", socket.gethostname()) == socket.gethostname()
    alive = (lambda: job.proc.poll() is None) if job.proc else (lambda: here and pid_alive(pid))
    stop_early = threading.Event()
    threading.Thread(target=fetch_early, args=(job, rel, stop_early), daemon=True).start()
    pos, buf = 0, b""
    try:
        while True:
            running = alive()
            try:
                with open(log, "rb") as f:
                    f.seek(pos)
                    chunk = f.read()
                pos += len(chunk)
            except OSError:
                chunk = b""
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for ln in lines:
                text = re.sub(r"\x1b\[[0-9;]*m", "", ln.decode("utf-8", "replace")).rstrip()
                if text:
                    job.note(text)
            if not running:
                break
            time.sleep(1)
        stop_early.set()
        if job.cancel_requested:
            job.state, job.phase = "cancelled", "Stopped"
            fetch_ready(job, rel)   # keep whatever was finished
            return
        if not (CLOUD / rel / "out" / "result.json").exists():
            fetch_ready(job, rel)
            raise RuntimeError("the cloud job ended early: " + " / ".join(job.log[-3:])
                               + (f" (kept {len(job.delivered)} finished cover(s))" if job.delivered else ""))
        collect(job, rel)
        job.state, job.phase = "done", "Finished"
    except Exception as e:
        job.state, job.error, job.phase = "failed", str(e)[:800], "Failed"
    finally:
        job.ended = time.time()


def adopt_orphans() -> None:
    """At startup: pick up cloud jobs a previous run of the app started or queued."""
    seen: set[str] = set()
    for rj in sorted((CLOUD / "jobs").glob("*/run.json")):
        info = read_json(rj, {})
        if not info.get("song_id") or not (SONGS / info["song_id"]).exists():
            continue
        job = Job(info["song_id"], info.get("takes", []), info.get("gpu"), None, info.get("singer", ""))
        job.id, job.created, job.started = info.get("id", job.id), info.get("created", job.created), info.get("started")
        job.state, job.phase = "running", "Reconnected after a restart"
        seen.add(job.id)
        with LOCK:
            JOBS.append(job)
        threading.Thread(target=follow, args=(job, rj.parent.relative_to(CLOUD).as_posix()), daemon=True).start()
    for info in read_json(QUEUE_FILE, []) or []:
        if not info.get("song_id") or not (SONGS / info["song_id"]).exists() or info.get("id") in seen:
            continue
        job = Job(info["song_id"], info.get("takes", []), info.get("gpu"), info.get("lyrics"), info.get("singer", ""))
        job.id, job.created = info.get("id", job.id), info.get("created", job.created)
        with LOCK:
            JOBS.append(job)
    save_queue()
    WAKE.set()


def worker_loop() -> None:
    while True:
        WAKE.wait(2)
        WAKE.clear()
        with LOCK:
            busy = any(j.state == "running" for j in JOBS)
            nxt = None if busy else next((j for j in JOBS if j.state == "queued"), None)
            if nxt:
                # Every queued cover of the same song rides along: one cloud
                # GPU start (1-2 minutes, the bulk of a cover's cost) instead
                # of one per click. Styles already in the batch are dropped.
                for other in [j for j in JOBS if j.state == "queued" and j is not nxt
                              and j.song_id == nxt.song_id and j.gpu == nxt.gpu
                              and j.singer == nxt.singer]:
                    have = {t["id"] for t in nxt.takes}
                    nxt.takes += [t for t in other.takes if t["id"] not in have]
                    other.state, other.phase, other.ended = "merged", f"Made together with job {nxt.id}", time.time()
                nxt.state = "running"
        if nxt:
            save_queue()
            run_job(nxt)


def stop_sandbox(sandbox_id: str) -> None:
    try:
        import modal
        modal.Sandbox.from_id(sandbox_id).terminate()
    except Exception:
        pass


# ── api ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="Houseband")


@app.get("/")
def index():
    # never cached: an old copy of the page outliving a fix is a broken app
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/styles")
def api_styles():
    return {"styles": styles.as_list(), "strengths": list(styles.STRENGTH)}


class StyleSave(BaseModel):
    id: str | None = None
    name: str
    group: str = "Custom"
    label: str = ""
    template: str


@app.post("/api/styles")
def api_create_style(body: StyleSave):
    try:
        return styles.upsert_style(body.id, body.name, body.group, body.label, body.template)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/styles/{style_id}")
def api_update_style(style_id: str, body: StyleSave):
    try:
        return styles.upsert_style(style_id, body.name, body.group, body.label, body.template)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/styles/{style_id}")
def api_delete_style(style_id: str):
    if not styles.reset_or_delete_style(style_id):
        raise HTTPException(404, "no custom style or override to remove")
    return {"ok": True, "styles": styles.as_list()}


@app.get("/api/songs")
def api_songs():
    SONGS.mkdir(parents=True, exist_ok=True)
    rows = [song_view(d) for d in SONGS.iterdir() if (d / "song.json").exists()]
    return sorted(rows, key=lambda r: -(r["added"] or 0))


@app.post("/api/songs")
async def api_upload(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in AUDIO_EXT:
        raise HTTPException(400, f"not an audio file I can read ({ext or 'no extension'})")
    SONGS.mkdir(parents=True, exist_ok=True)
    tmp = SONGS / f".up-{uuid.uuid4().hex}{ext}"
    h, size = hashlib.sha1(), 0
    with open(tmp, "wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_BYTES:
                f.close()
                tmp.unlink(missing_ok=True)
                raise HTTPException(413, "that file is over 200 MB")
            h.update(chunk)
            f.write(chunk)
    sid = h.hexdigest()[:16]
    d = SONGS / sid
    if (d / "song.json").exists():
        tmp.unlink(missing_ok=True)
        return song_view(d)
    d.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, d / f"source{ext}")
    title = Path(file.filename).stem
    duration = await asyncio.to_thread(lyrics_db.probe_duration, d / f"source{ext}")
    found = await asyncio.to_thread(lyrics_db.search, title, duration)
    meta = {"title": title, "added": time.time(), "takes": [], "duration": duration}
    if found:
        meta.update(lyrics=found["text"], lyrics_synced=found["synced"],
                    lyrics_source=f"LRCLIB: {found['artist']} – {found['track']}")
    write_json(d / "song.json", meta)
    return song_view(d)


class LyricsEdit(BaseModel):
    lyrics: str


@app.put("/api/songs/{song_id}/lyrics")
def api_lyrics(song_id: str, body: LyricsEdit):
    d = song_dir(song_id)
    meta = read_json(d / "song.json", {})
    text = body.lyrics.strip()
    if text != (meta.get("lyrics") or "").strip():
        # Fixing words ("hearts", not "cars") keeps the line count: keep the
        # time stamps, re-attached in order. Anything else invalidates them.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("[")]
        synced = meta.get("lyrics_synced") or []
        if synced and len(lines) == len(synced):
            meta["lyrics_synced"] = [[t, ln] for (t, _), ln in zip(synced, lines)]
        else:
            meta.pop("lyrics_synced", None)
    meta["lyrics"] = text
    meta["lyrics_source"] = "edited by you" if text else None
    write_json(d / "song.json", meta)
    return song_view(d)


@app.delete("/api/songs/{song_id}")
def api_delete_song(song_id: str):
    d = song_dir(song_id)
    if any(j.song_id == song_id and j.state in ("queued", "running") for j in JOBS):
        raise HTTPException(409, "a cover of this song is still being made")
    shutil.rmtree(d)
    for f in (CLOUD / "songs").glob(f"{song_id}.*"):
        f.unlink(missing_ok=True)
    return {"ok": True}


@app.delete("/api/songs/{song_id}/takes/{file}")
def api_delete_take(song_id: str, file: str):
    d = song_dir(song_id)
    meta = read_json(d / "song.json", {})
    keep = [t for t in meta.get("takes", []) if t["file"] != file]
    if len(keep) == len(meta.get("takes", [])):
        raise HTTPException(404, "no such take")
    for f in (file, file.rsplit(".", 1)[0] + ".flac"):
        (d / "takes" / Path(f).name).unlink(missing_ok=True)
    meta["takes"] = keep
    write_json(d / "song.json", meta)
    return {"ok": True}


@app.get("/api/songs/{song_id}/score")
def api_score(song_id: str):
    p = song_dir(song_id) / "score.abc"
    if not p.exists():
        raise HTTPException(404, "no melody score yet; it is saved with the song's first cover")
    return PlainTextResponse(p.read_text(encoding="utf-8"))


@app.get("/api/songs/{song_id}/audio/{name}")
def api_audio(song_id: str, name: str):
    d = song_dir(song_id)
    name = Path(name).name
    p = d / name if name.startswith("source.") else d / "takes" / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.post("/api/songs/{song_id}/takes/{file}/star")
def api_star_take(song_id: str, file: str):
    d = song_dir(song_id)
    with SONG_LOCK:
        meta = read_json(d / "song.json", {})
        target = next((t for t in meta.get("takes", []) if t["file"] == file), None)
        if not target:
            raise HTTPException(404, "no such take")
        target["starred"] = not bool(target.get("starred"))
        write_json(d / "song.json", meta)
    return {"ok": True, "starred": target["starred"]}


@app.get("/api/songs/{song_id}/peaks")
def api_peaks(song_id: str, file: str = "", bars: int = 180):
    import array
    import math
    d = song_dir(song_id)
    name = Path(file).name if file else next((f.name for f in d.glob("source.*")), "")
    p = d / name if name.startswith("source.") else d / "takes" / name
    if not name or not p.exists():
        raise HTTPException(404, "no such audio file")
    bars = max(40, min(bars, 360))
    cache_path = d / "peaks.json"
    cache = read_json(cache_path, {}) or {}
    key = f"{name}:{bars}:{p.stat().st_size}"
    if key in cache:
        return {"peaks": cache[key]}
    try:
        flags = 0x08000000 if os.name == "nt" else 0
        raw = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(p), "-ac", "1", "-ar", "1000", "-f", "f32le", "-"],
            capture_output=True, check=True, timeout=20, creationflags=flags
        ).stdout
        samples = array.array("f", raw)
        n = len(samples)
        if n < bars:
            peaks = [0.2] * bars
        else:
            step = n / bars
            vals = []
            for i in range(bars):
                seg = samples[int(i * step):max(int(i * step) + 1, int((i + 1) * step))]
                rms = math.sqrt(sum(x * x for x in seg) / len(seg)) if seg else 0.0
                pk = max((abs(x) for x in seg), default=0.0)
                vals.append(rms * 0.65 + pk * 0.35)
            hi = max(vals) or 1.0
            peaks = [round(max(0.06, min(1.0, (v / hi) ** 0.85)), 3) for v in vals]
        cache[key] = peaks
        with SONG_LOCK:
            write_json(cache_path, cache)
        return {"peaks": peaks}
    except Exception:
        return {"peaks": [0.25] * bars}


class CoverRequest(BaseModel):
    song_id: str
    styles: list[str]
    strength: str = styles.DEFAULT_STRENGTH
    gpu: str | None = None
    singer: str = ""


@app.post("/api/covers")
def api_cover(req: CoverRequest):
    song_dir(req.song_id)
    if not req.styles:
        raise HTTPException(400, "pick at least one style")
    try:
        take_list = [styles.take_for(s, req.strength, int.from_bytes(os.urandom(3), "big"))
                     for s in dict.fromkeys(req.styles)]
    except KeyError as e:
        raise HTTPException(400, f"unknown style {e}")
    if req.singer not in styles.SINGERS:
        raise HTTPException(400, "singer must be male, female or empty")
    job = Job(req.song_id, take_list, req.gpu or None, None, req.singer)
    with LOCK:
        JOBS.append(job)
    save_queue()
    WAKE.set()
    return job.view()


@app.get("/api/jobs")
def api_jobs():
    with LOCK:
        return [j.view() for j in JOBS[-20:]][::-1]


@app.post("/api/jobs/{job_id}/cancel")
def api_cancel(job_id: str):
    job = next((j for j in JOBS if j.id == job_id), None)
    if not job or job.state not in ("queued", "running"):
        raise HTTPException(409, "that job isn't running")
    job.cancel_requested = True
    if job.state == "queued":
        job.state, job.phase, job.ended = "cancelled", "Removed from the queue", time.time()
        save_queue()
        return {"ok": True}
    job.phase = "Stopping the cloud GPU"
    if job.sandbox:
        threading.Thread(target=stop_sandbox, args=(job.sandbox,), daemon=True).start()
    if job.proc and job.proc.poll() is None:
        job.proc.terminate()
    elif not job.proc:
        info = read_json(CLOUD / "jobs" / job.id / "run.json", {})
        pid = info.get("pid")
        if pid and info.get("host", socket.gethostname()) == socket.gethostname():
            try:
                os.kill(pid, 9)
            except OSError:
                pass
    return {"ok": True}


@app.get("/api/gpus")
def api_gpus():
    if ENGINE == "local":
        return {"gpus": [], "default": None, "engine": "local"}
    try:
        from chuck_gpu import gpus
        return {"gpus": gpus.catalog(), "default": DEFAULT_GPU, "engine": "modal"}
    except Exception as e:
        return JSONResponse({"gpus": [], "error": str(e), "engine": "modal"})


_started = False


def start_background() -> None:
    """Adopt jobs from a previous run and start the job queue (once per process).
    main() calls it locally; deploy/modal_app.py calls it in the cloud."""
    global _started
    with LOCK:
        if _started:
            return
        _started = True
    adopt_orphans()
    threading.Thread(target=worker_loop, daemon=True).start()


def main() -> None:
    if sys.stdout is None or sys.stderr is None:
        # started by pythonw (Houseband.bat): no console, and uvicorn's
        # log setup fails outright without one; keep its output in a file
        DATA.mkdir(parents=True, exist_ok=True)
        log = open(DATA / "server.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    import uvicorn
    import webbrowser
    port = int(os.environ.get("HOUSEBAND_PORT", "8795"))
    host = os.environ.get("HOUSEBAND_HOST", "127.0.0.1")
    url = f"http://127.0.0.1:{port}"
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:   # already running: just show it
            webbrowser.open(url)
            return
    start_background()
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
