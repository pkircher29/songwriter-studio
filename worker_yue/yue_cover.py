"""One batch of YuE2 covers of one song:  /opt/yue/bin/python worker/yue_cover.py jobs/<id>

job.json:
  source   songs/<hash>.<ext>      (shared by every job for that song)
  lyrics   the words to sing, or "" to transcribe them here
  singer   "male" | "female" | ""  (any)
  genres   {style_id: label}       candidates for "keep the original style"
  takes    [{id, name, style, seed, cfg}]; style is a template with {lang},
           {singer} and {bpm}; an id of "original" means: the genre the
           song itself is closest to

Per song, once (cached under cache/analysis/<v>-<sha1>.json):
  1. SheetSage2 transcribes the melody to a melody-only ABC score (no chords,
     so the new style re-harmonises it); tempo and key come from the score;
  2. lyrics, only if none were supplied: Whisper large-v3 on the mix;
  3. the original's genre: CLAP (music) against the style labels.
Per take:
  4. YuE2 performs the score with the lyrics in the take's style
     (cot="melody": the tune is fixed, the band and the singer are new).

Every step prints "[cover] ..." for the local app's progress display.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*weight_norm.*")

WS = Path.cwd()
CACHE = WS / "cache"
os.environ.setdefault("HF_HOME", str(CACHE / "hf"))
# trust_remote_code modules (SheetSage2) are written then imported: keep them
# off the network volume
os.environ.setdefault("HF_MODULES_CACHE", "/tmp/hf_modules")
ANALYSIS_VERSION = "y2"
# YuE2's semantic stream is ~30.5 tokens per second of audio (measured: a
# 10248-token cap gave 336s). Its default min of 200 tokens let an Acoustic
# Folk Helena stop at 0:49; the length is bounded by the original's instead.
TOKENS_PER_S = 30.5
LANG_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
              "it": "Italian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese"}
LANG_WORDS = {
    "en": "the and you to it is in that me my your i was we",
    "es": "el la que de y en los se no te mi por las una con",
    "fr": "le la les et de que je tu un une est pas pour dans",
    "de": "der die und das ich du nicht ist ein eine mit zu dich",
    "pt": "o a que de e não eu você um uma meu com para do",
    "it": "il la che di e non io tu un una mi per con sono",
}


def say(m):
    print(f"[cover] {m}", flush=True)


def sha1(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def pcm(path, sr):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-ac", "1",
                          "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def free_gpu():
    import gc
    import torch
    gc.collect()
    torch.cuda.empty_cache()


# ── analysis ────────────────────────────────────────────────────────────────

def transcribe_melody(song, work):
    # transformers' remote-code loader copies only the modules it sees imported
    # directly; SheetSage2's chord_spelling is pulled in indirectly and goes
    # missing. Seed the cache with all of them.
    mods = Path(os.environ["HF_MODULES_CACHE"]) / "transformers_modules" / "model"
    mods.mkdir(parents=True, exist_ok=True)
    (mods.parent / "__init__.py").touch()
    for f in Path("/opt/sheetsage/model").glob("*.py"):
        shutil.copy(f, mods / f.name)
    subprocess.run(["/opt/sheetsage/bin/python", "/opt/sheetsage/model/infer.py", str(song),
                    "--output", str(work / "score"), "--melody-only"], check=True)
    return (work / "score" / "score.abc").read_text(encoding="utf-8")


JUNK = re.compile(r"(?i)thank(s| you) for watching|subtitles by|amara\.org|^\W*$|♪")


def whisper_words(song):
    """[(seconds, word)] for the whole song, and the language Whisper hears.

    Long-form mode on the full mix (it read 'The Boxer' near word for word;
    the chunked mode hallucinated), with word-level timestamps: the times are
    what lines the lyrics up with the score's sections."""
    import torch
    from transformers import pipeline
    audio = pcm(song, 16000)
    asr = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3",
                   torch_dtype=torch.float16, device="cuda:0")
    res = asr({"raw": audio, "sampling_rate": 16000}, return_timestamps="word",
              generate_kwargs={"task": "transcribe", "condition_on_prev_tokens": False,
                               "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                               "compression_ratio_threshold": 1.35,
                               "logprob_threshold": -1.0, "no_speech_threshold": 0.6})
    lang = "unknown"
    try:
        feats = asr.feature_extractor(audio[:16000 * 30], sampling_rate=16000,
                                      return_tensors="pt").input_features.to("cuda:0", torch.float16)
        lang = asr.tokenizer.decode([int(asr.model.detect_language(feats)[0])]).strip("<|>")
    except Exception as e:
        say(f"warning: language detection failed ({e})")
    del asr
    free_gpu()
    words = []
    for c in res.get("chunks") or []:
        w = (c.get("text") or "").strip()
        t = (c.get("timestamp") or (None,))[0]
        if w and t is not None and not JUNK.search(w):
            words.append([round(float(t), 2), w])
    return words, lang


def lines_from_words(words):
    """Whisper's own words as timed lines: a breath of 0.8s+ or 10 words ends a line."""
    lines, cur, start, last = [], [], None, None
    for t, w in words:
        if cur and (t - last > 0.8 or len(cur) >= 10):
            lines.append([start, " ".join(cur)])
            cur = []
        if not cur:
            start = t
        cur.append(w)
        last = t
    if cur:
        lines.append([start, " ".join(cur)])
    return lines


def _norm(w):
    return re.sub(r"[^a-z0-9']", "", w.lower())


def align_lines(text, words):
    """Given lyrics (no times) -> [(seconds, line)] by matching their words to
    Whisper's timed words IN ORDER: a longest-common-subsequence alignment.
    (difflib's greedy longest-block match paired the first pre-chorus with the
    second one, since songs repeat; that filed half of Helena in one chorus.)
    Lines with no matched word get a time between their neighbours."""
    import numpy as np
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("[")]
    flat, owner = [], []
    for li, ln in enumerate(lines):
        for w in ln.split():
            if _norm(w):
                flat.append(_norm(w))
                owner.append(li)
    heard = [_norm(w) for _, w in words]
    n, m = len(flat), len(heard)
    if not n or not m:
        return []
    dp = np.zeros((n + 1, m + 1), dtype=np.int32)
    for a in range(n - 1, -1, -1):
        fa = flat[a]
        row, nxt = dp[a], dp[a + 1]
        for b in range(m - 1, -1, -1):
            row[b] = nxt[b + 1] + 1 if fa == heard[b] else max(nxt[b], row[b + 1])
    first = {}
    a = b = 0
    while a < n and b < m:
        if flat[a] == heard[b] and dp[a, b] == dp[a + 1, b + 1] + 1:
            first.setdefault(owner[a], words[b][0])
            a, b = a + 1, b + 1
        elif dp[a + 1, b] >= dp[a, b + 1]:
            a += 1
        else:
            b += 1
    known = sorted(first.items())
    out = []
    for li, ln in enumerate(lines):
        if li in first:
            t = first[li]
        else:   # between the nearest matched neighbours
            before = [(k, v) for k, v in known if k < li]
            after = [(k, v) for k, v in known if k > li]
            if before and after:
                (k0, t0), (k1, t1) = before[-1], after[0]
                t = t0 + (t1 - t0) * (li - k0) / (k1 - k0)
            else:
                t = (before[-1][1] if before else (after[0][1] if after else 0.0))
        out.append([round(float(t), 2), ln])
    return out


def score_sections(abc):
    """[(name, start_s, end_s, sung)] from the score's "% section" markers,
    timed by bars of the Vocal voice at the score's tempo."""
    q = re.search(r"^Q:\s*1/4=(\d+)", abc, re.M)
    m = re.search(r"^M:\s*(\d+)/(\d+)", abc, re.M)
    bpm = int(q.group(1)) if q else 120
    beats = int(m.group(1)) * 4 / int(m.group(2)) if m else 4
    bar_s = beats * 60 / bpm
    secs, cur, voice = [], None, None
    for line in abc.splitlines():
        if line.startswith("% "):
            cur = {"name": line[2:].strip(), "bars": 0, "sung": False}
            secs.append(cur)
            continue
        if line.startswith("V:"):
            parts = line[2:].split()
            voice = parts[0] if parts else ""
            continue
        if cur is None or voice != "Vocal" or re.match(r"^[A-Z]:", line):
            continue
        for seg in [x for x in line.split("|") if x.strip()]:
            z = re.fullmatch(r"\s*Z(\d*)\s*", seg)
            cur["bars"] += int(z.group(1) or 1) if z else 1
            if not z and re.search(r"[A-Ga-g]", seg):
                cur["sung"] = True
    out, t = [], 0.0
    for c in secs:
        out.append((c["name"], t, t + c["bars"] * bar_s, c["sung"]))
        t += c["bars"] * bar_s
    return out


def section_tag(name):
    return "[" + "-".join(p.capitalize() for p in re.split(r"[-_ ]+", name) if p) + "]"


def sectioned_lyrics(timed, sections):
    """Lyrics in the score's own sections, in its order: YuE2 needs the tags
    and lyric order to match the score, or words drift off the melody (they
    did: 'the timing of the lyrics was way off', 2026-10-12)."""
    sung = [s for s in sections if s[3]]
    if not sung:
        return ""
    buckets = [[] for _ in sung]
    for t, line in sorted(timed, key=lambda x: x[0]):
        t = t + 0.5   # a pickup sung just before the bar line belongs to the next section
        k = next((i for i, s in enumerate(sung) if s[1] <= t < s[2]), None)
        if k is None:
            k = 0 if t < sung[0][1] else len(sung) - 1
        buckets[k].append(line)
    return "\n\n".join(section_tag(s[0]) + ("\n" + "\n".join(b) if b else "") for s, b in zip(sung, buckets))


def closest_genre(song, genres):
    """Which of the style labels the original sounds most like (CLAP, music)."""
    import torch
    from transformers import ClapModel, ClapProcessor
    model = ClapModel.from_pretrained("laion/larger_clap_music").to("cuda:0").eval()
    proc = ClapProcessor.from_pretrained("laion/larger_clap_music")
    y = pcm(song, 48000)
    wins = [y[i:i + 480000] for i in range(0, max(1, len(y) - 480000), 480000)][:12] or [y]
    ids = list(genres)
    with torch.no_grad():
        a = model.get_audio_features(**proc(audios=wins, sampling_rate=48000, return_tensors="pt").to("cuda:0"))
        t = model.get_text_features(**proc(text=[f"{genres[i]} music" for i in ids],
                                           return_tensors="pt", padding=True).to("cuda:0"))
    a = (a / a.norm(dim=-1, keepdim=True)).mean(0)
    t = t / t.norm(dim=-1, keepdim=True)
    scores = (t @ a).cpu().tolist()
    del model
    free_gpu()
    ranked = sorted(zip(ids, scores), key=lambda kv: -kv[1])
    return ranked[0][0], [[i, round(s, 3)] for i, s in ranked[:5]]


def text_language(text):
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    if len(words) < 20:
        return "unknown"
    scores = {k: sum(w in set(v.split()) for w in words) / len(words) for k, v in LANG_WORDS.items()}
    k, best = max(scores.items(), key=lambda kv: kv[1])
    return k if best > 0.08 else "unknown"


def yue_tags(lyrics):
    """[verse]/[chorus]/[bridge] → YuE2's [Verse]/[Chorus]/[Bridge]."""
    return re.sub(r"\[([a-zA-Z][a-zA-Z -]*)\]", lambda m: "[" + m.group(1).strip().title() + "]", lyrics)


# ── timing check ────────────────────────────────────────────────────────────

ON_TIME_S = 3.0      # a line counts as on time within this of where the original sings it
GOOD_ENOUGH = 0.85   # keep a render with at least this share of lines on time
_ASR = None


def heard_words(path):
    """[(seconds, word)] Whisper hears in a rendered cover."""
    global _ASR
    import torch
    from transformers import pipeline
    if _ASR is None:
        _ASR = pipeline("automatic-speech-recognition", model="openai/whisper-large-v3",
                        torch_dtype=torch.float16, device="cuda:0")
    r = _ASR({"raw": pcm(path, 16000), "sampling_rate": 16000}, return_timestamps="word",
             generate_kwargs={"task": "transcribe", "condition_on_prev_tokens": False,
                              "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0), "compression_ratio_threshold": 1.35,
                              "logprob_threshold": -1.0, "no_speech_threshold": 0.6})
    return [[c["timestamp"][0], c["text"].strip()] for c in r.get("chunks") or [] if c["timestamp"][0] is not None]


def timing_score(path, ref):
    """Share of the lyric lines the cover sings within ON_TIME_S of the original.
    (Measured on Helena: an on-time render scores ~0.9, the ones that rushed
    the words ahead of the melody 0.25-0.75; the same settings vary that much
    from one seed to the next, so a bad draw is re-rendered, not shipped.)"""
    text = "\n".join(ln for _, ln in ref)
    got = align_lines(text, heard_words(path))
    if not got:
        return 0.0
    return sum(abs(a[0] - r[0]) <= ON_TIME_S for a, r in zip(got, ref)) / len(ref)


def find_end(path, length, dur):
    """(end_s, fade_s): if the cover finishes and goes silent (>=1.25s below
    rms 0.006) in the last 12% of the song, stop in that silence before YuE2
    can ad-lib a false restart (Bluegrass restarted at 3:18 after ending at
    3:12); otherwise trim at dur + 4s with a 3s fade."""
    import numpy as np
    cap = min(length, dur + 4.0)
    t0 = max(dur * 0.88, dur - 20.0)
    if cap <= t0 + 2.0:
        return cap, 3.0
    try:
        y = pcm(path, 4000)[int(t0 * 4000):int(cap * 4000)]
        hop = 1000   # 0.25s at 4 kHz
        rms = [float(np.sqrt(np.mean(y[i:i + hop] ** 2))) for i in range(0, len(y) - hop + 1, hop)]
        for idx in range(len(rms) - 5):
            if all(r < 0.006 for r in rms[idx:idx + 5]):
                quiet_t = t0 + idx * 0.25
                return min(cap, quiet_t + 0.75), 1.5
    except Exception:
        pass
    return cap, 3.0


# ── main ────────────────────────────────────────────────────────────────────

def main():
    job_dir = WS / sys.argv[1]
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    out = job_dir / "out"
    out.mkdir(exist_ok=True)
    work = Path("/tmp/yue")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    result = {"ok": False, "takes": []}
    try:
        import torch
        say(f"GPU: {torch.cuda.get_device_name(0)}")
        src = WS / job["source"]
        song = work / "song.wav"
        say("step 1/4: decoding the song")
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-ac", "2",
                        "-ar", "44100", "-t", "600", str(song)], check=True)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=nw=1:nk=1", str(song)],
                                   capture_output=True, text=True, check=True).stdout)

        key = sha1(src)
        an_path = CACHE / "analysis" / f"{ANALYSIS_VERSION}-{key}.json"
        an = json.loads(an_path.read_text(encoding="utf-8")) if an_path.exists() else {}
        prev = CACHE / "analysis" / f"y1-{key}.json"   # keep the score and genre heard before y2
        if prev.exists() and "abc" not in an:
            an.update({k: v for k, v in json.loads(prev.read_text(encoding="utf-8")).items()
                       if k in ("abc", "genre", "genre_ranking")})
        old_scores = CACHE / "scores" / f"{key}.abc"   # from before the analysis cache
        if "abc" not in an and old_scores.exists():
            an["abc"] = old_scores.read_text(encoding="utf-8")
        if "abc" not in an:
            say("step 2/4: transcribing the melody to sheet music")
            t0 = time.time()
            an["abc"] = transcribe_melody(song, work)
            say(f"melody transcribed in {time.time() - t0:.0f}s")
        else:
            say("step 2/4: melody transcribed earlier, reusing")
        q = re.search(r"^Q:\s*1/4=(\d+)", an["abc"], re.M)
        k = re.search(r"^K:\s*(\S+)", an["abc"], re.M)
        an["bpm"] = int(q.group(1)) if q else None
        an["key"] = k.group(1) if k else None

        lyrics = (job.get("lyrics") or "").strip()
        synced = job.get("lyrics_synced") or []
        if not synced and "words" not in an:
            say("step 3/4: listening to where the words fall in the song")
            an["words"], an["whisper_language"] = whisper_words(song)
        if synced:
            timed = synced                                   # LRCLIB's own time stamps
        elif lyrics:
            timed = align_lines(lyrics, an.get("words") or [])
        else:
            timed = lines_from_words(an.get("words") or [])
            if len({ln for _, ln in timed}) < 4:             # a failed transcription, not lyrics
                timed = []
                say("warning: no words could be made out; the cover will be sung without set lyrics")
            an["lyrics"] = "\n".join(ln for _, ln in timed)
        lyrics = lyrics or an.get("lyrics") or ""
        if job.get("genres") and "genre" not in an:
            say("step 3/4: working out what genre the original is")
            an["genre"], an["genre_ranking"] = closest_genre(song, job["genres"])
        an_path.parent.mkdir(parents=True, exist_ok=True)
        an_path.write_text(json.dumps(an, indent=2), encoding="utf-8")

        lang = an.get("whisper_language") if not job.get("lyrics") else text_language(lyrics)
        if lang in (None, "", "unknown"):
            lang = text_language(lyrics)
        language = LANG_NAMES.get(lang, "English")
        an.update(duration=round(dur, 1), language=lang)
        (out / "analysis.json").write_text(json.dumps({k: v for k, v in an.items() if k != "abc"}, indent=2),
                                           encoding="utf-8")
        (out / "score.abc").write_text(an["abc"], encoding="utf-8")
        say(f"heard: {an['bpm']} BPM, key {an['key']}, {language}"
            + (f", sounds like {(job.get('genres') or {}).get(an['genre'], an['genre'])}" if an.get("genre") else ""))

        from yue2 import YuE2Pipeline
        from yue2.protocol import Sampling
        semantic = Sampling(min_tokens=int(dur * TOKENS_PER_S * 0.92),
                            max_tokens=min(int(dur * TOKENS_PER_S * 1.08) + 100, 20000))
        singer = {"male": "male", "female": "female"}.get(job.get("singer") or "", "")
        if synced:
            ref_timed = synced
        elif lyrics and an.get("words"):
            ref_timed = align_lines(lyrics, an["words"])
        else:
            ref_timed = []
        sections = score_sections(an["abc"])
        sung = sectioned_lyrics(timed, sections) if timed else ""
        if not sung:   # no timing to go on: the lyrics as tagged
            sung = yue_tags(lyrics) if lyrics else "[Verse]\n"
        (out / "lyrics_sung.txt").write_text(yue_tags(lyrics) if lyrics else sung, encoding="utf-8")
        say(f"lyrics: {len([1 for ln in lyrics.splitlines() if ln.strip() and not ln.startswith('[')])} lines"
            + (f", timed from {'LRCLIB' if synced else 'the original'} for the timing check" if ref_timed else ""))
        say("step 4/4: loading YuE2")
        with YuE2Pipeline.from_pretrained("m-a-p/YuE2-3B", device="cuda") as pipe:
            for i, take in enumerate(job["takes"], 1):
                say(f"step 4/4: making the {take['name']} version ({i} of {len(job['takes'])})")
                t0 = time.time()
                template = take["style"]
                if take["id"] == "original":
                    template = (job.get("styles_by_id") or {}).get(an.get("genre"), template)
                style = (template.replace("{lang}", language)
                         .replace("{singer}", f"{singer} " if singer else "")
                         .replace("{bpm}", f"{an['bpm']} BPM" if an.get("bpm") else ""))
                style = re.sub(r"\s+,", ",", re.sub(r",\s*,", ",", style)).strip(" ,")
                cfg = take.get("cfg")
                # The plain tagged lyrics: an experiment on Helena (2026-10-12)
                # had the section-aligned ones rush the words ahead of the
                # melody at cfg 3 (11-32 of 43 lines off vs 3 of 43).
                take_sung = sung if take.get("lyrics_mode") == "aligned" else (yue_tags(lyrics) if lyrics else sung)
                seed = int(take.get("seed", 42))
                best = None
                for attempt in range(1, 3):
                    cand = pipe(style=style, lyrics=take_sung, abc=an["abc"], cot="melody",
                                seed=seed + 1000 * (attempt - 1), semantic_sampling=semantic,
                                **({"cfg_scale": float(cfg)} if cfg else {}))
                    if not ref_timed or job.get("timing_check") is False:
                        best = (None, cand)
                        break
                    probe = work / "probe.wav"
                    import soundfile as sf
                    sf.write(probe, cand.audio[: int(min(len(cand.audio) / cand.sample_rate, dur + 4) * cand.sample_rate)],
                             cand.sample_rate)
                    score = timing_score(probe, ref_timed)
                    say(f"timing check: {score:.0%} of lyric lines on time" + ("" if attempt == 1 else " (second try)"))
                    if best is None or score > best[0]:
                        best = (score, cand)
                    if score >= GOOD_ENOUGH:
                        break
                    if attempt == 1:
                        say(f"words drifting off the melody; rendering {take['name']} again")
                song_out = best[1]
                take["timing"] = None if best[0] is None else round(best[0], 2)
                cut = song_out.truncated if isinstance(song_out.truncated, dict) else {"any": song_out.truncated}
                length = len(song_out.audio) / song_out.sample_rate
                flac = out / f"{take['id']}.flac"
                song_out.save(flac)
                # the score follows the original's timing, so the song is over
                # where the original ends; past that the model only ad-libs
                end, fade = find_end(flac, length, dur)
                subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(flac),
                                "-t", f"{end:.2f}", "-af", f"afade=t=out:st={max(end - fade, 0):.2f}:d={fade}",
                                str(work / "trim.flac")], check=True)
                shutil.move(work / "trim.flac", flac)
                mp3 = flac.with_suffix(".mp3")
                subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(flac),
                                "-b:a", "256k", str(mp3)], check=True)
                result["takes"].append({"id": take["id"], "mp3": mp3.name, "flac": flac.name, "style": style,
                                        "seed": take.get("seed"), "cfg": cfg, "truncated": any(cut.values()),
                                        "timing": take.get("timing"),
                                        "seconds": round(time.time() - t0, 1)})
                # publish this take now, not at the end of the batch: a record
                # beside it (with the sizes, so a half-committed read is
                # detectable) and a volume sync so the local app can fetch it
                rec = dict(result["takes"][-1], mp3_size=mp3.stat().st_size, flac_size=flac.stat().st_size)
                (out / f"{take['id']}.done.json").write_text(json.dumps(rec), encoding="utf-8")
                subprocess.run(["sync", str(WS)], check=False)
                say(f"done: {take['name']} in {time.time() - t0:.0f}s")
        result["ok"] = True
        return 0
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        say(f"failed: {result['error']}")
        return 1
    finally:
        (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
