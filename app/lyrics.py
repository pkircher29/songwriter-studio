"""Find a song's real lyrics on LRCLIB (free, no key) from its file name.

Whisper on a separated vocal is the fallback, not the first choice: sung
words under distortion and reverb transcribe badly, and the cover sings
whatever it is given. LRCLIB's free-text search matches "artist title"
loosely; the duration of the file picks between the album cut, the live
take and the acoustic version, which share a title but not lyrics.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request

API = "https://lrclib.net/api/search"
UA = "SongwriterStudio/1.0 (https://github.com/pkircher29/songwriter-studio)"
MAX_DURATION_GAP = 8.0   # seconds; a different edit of the song beyond this


def probe_duration(path) -> float | None:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)],
                             capture_output=True, text=True, timeout=30,
                             creationflags=0x08000000 if hasattr(subprocess, "STARTUPINFO") else 0)
        return float(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def clean_title(name: str) -> str:
    """'01 - The Gaslight Anthem - The Boxer [Official Audio] (2010)' → 'gaslight anthem the boxer'."""
    s = re.sub(r"[\[(][^\])]*[\])]", " ", name)                  # (Official Video), [Radio Edit]
    s = re.sub(r"(?i)\b(official|audio|video|lyrics?|hq|hd|remaster(ed)?|explicit)\b", " ", s)
    s = re.sub(r"^\s*\d{1,3}\s*[-._)]\s*", " ", s)                 # leading track number
    s = re.sub(r"[_\-–—,.]+", " ", s)
    s = re.sub(r"(?i)^\s*the\s+", "", s.strip())                    # "The Band" is often "Band" there
    return re.sub(r"\s+", " ", s).strip().lower()


def search(title: str, duration: float | None) -> dict | None:
    q = clean_title(title)
    if not q:
        return None
    url = f"{API}?{urllib.parse.urlencode({'q': q})}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    rows = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                rows = json.load(r)
            break
        except Exception as e:
            print(f"[lyrics] LRCLIB lookup for {q!r} failed (try {attempt + 1}): {e}", flush=True)
    if rows is None:
        return None
    rows = [r for r in rows if (r.get("plainLyrics") or "").strip() and not r.get("instrumental")]
    if duration:
        rows = [r for r in rows if r.get("duration") and abs(r["duration"] - duration) <= MAX_DURATION_GAP]
        rows.sort(key=lambda r: abs(r["duration"] - duration))
    if not rows:
        return None
    # prefer an entry with time stamps: they line the words up with the melody
    best = next((r for r in rows if (r.get("syncedLyrics") or "").strip()), rows[0])
    return {"source": "LRCLIB", "artist": best.get("artistName"), "track": best.get("trackName"),
            "text": tag_stanzas(best["plainLyrics"]), "synced": parse_synced(best.get("syncedLyrics") or "")}


def parse_synced(lrc: str) -> list:
    """'[01:02.30] line' → [[62.3, 'line'], ...] (blank lines dropped)."""
    out = []
    for ln in lrc.splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", ln)
        if m and m.group(3).strip():
            out.append([round(int(m.group(1)) * 60 + float(m.group(2)), 2), m.group(3).strip()])
    return out


def tag_stanzas(plain: str) -> str:
    """Blank-line stanzas → ACE-Step's [verse]/[chorus] format; a stanza that
    comes back word for word is the chorus."""
    stanzas = [s.strip() for s in re.split(r"\n\s*\n", plain.replace("\r", "")) if s.strip()]
    norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()
    counts: dict[str, int] = {}
    for s in stanzas:
        counts[norm(s)] = counts.get(norm(s), 0) + 1
    return "\n\n".join(("[chorus]" if counts[norm(s)] > 1 else "[verse]") + "\n" + s for s in stanzas)
