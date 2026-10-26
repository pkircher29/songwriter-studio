# Houseband

**Hear it out.**

You wrote a song. You can play it — one voice and a guitar or a piano, recorded
on your phone at the kitchen table. You can hear the finished version in your
head; you just can't hear it out loud. Houseband is the band. Drop the demo,
pick a style, and hear your song played back by a full band and a singer, with
your melody and your words. Try it as Bluegrass, Synthwave, Gospel Choir,
Nu Metal… 36 styles, or describe your own, and keep the take that sounds like
what you meant.

What it is, and isn't:

- **It plays your song.** The melody is transcribed from your demo and fixed;
  the words are yours (typed in, transcribed from your demo, or looked up if
  the song is published). Houseband never writes a song or a lyric for you.
- **Takes are sketches for deciding**, not a finished master: does this song
  want to be a waltz or a stomp, a choir or one voice? Hear it before you book
  a studio or a band.
- **Local-first.** Your songs, lyrics and takes stay in `data/` on your machine.
  The GPU work runs where you choose (below).

Two editions:

- **Houseband** (this repo): the desktop app. You bring the compute — your own
  Modal account, or your own NVIDIA GPU.
- **Houseband Cloud**: a hosted edition on our GPUs, in private development.

## Use it

1. Drop your demo on the left (`mp3`, `wav`, `flac`, `m4a`, `ogg`, `aac`,
   `opus`; up to 200 MB).
2. Check the lyrics in **Edit Lyrics & Stanzas** (see *Melody fit* below), or
   let the first take transcribe them from the demo.
3. Click **Hear it** on a style, or tick several and **Hear It in Selected
   Styles** to run them in one batch. Each finished take lands in the rack with
   A/B against your demo (`A` key flips between them at the same moment),
   MP3 and FLAC downloads, and a lyric-timing score.

### Install

```
git clone https://github.com/pkircher29/houseband
cd houseband
py -3.12 -m venv .venv                  # Linux/macOS: python3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

Needs `ffmpeg`/`ffprobe` on PATH. Then double-click **`Houseband.bat`**
(Windows; starts in the background and opens `http://127.0.0.1:8795`, a second
double-click just reopens the page) or run `.venv/bin/python app/server.py`.

### Where the band plays: bring your own compute

**A. Your own Modal account** (default; any OS; easiest)

```
.venv\Scripts\chuck-gpu login
```

[chuck-gpu](https://github.com/pkircher29/chuck-gpu) builds the worker image
from `worker_yue/chuck-gpu.toml` on your Modal account and runs each batch in
a sandbox there (default GPU L40S; pick another in the app). A take is roughly
75–120 s of GPU time per style plus ~40 s once per song, which is a few cents
on Modal's pricing at the time of writing; Modal's free tier includes monthly
credits. Nothing runs on anyone else's account.

**B. Your own NVIDIA GPU** (Linux or WSL2; 24 GB VRAM, bf16)

Build the two worker environments the cloud image uses — the exact recipe is
`worker_yue/chuck-gpu.toml` (`/opt/yue` with `yue2-infer` at the pinned
commit, `/opt/sheetsage` with the SheetSage2 model; the worker expects those
paths) — then start the app with the local engine:

```
HOUSEBAND_ENGINE=local HOUSEBAND_YUE_PYTHON=/opt/yue/bin/python python app/server.py
```

The same worker script runs on your machine; models download into
`data/cloud-yue/cache/` on the first take. The GPU picker disappears (there is
only yours).

Settings are environment variables: `HOUSEBAND_DATA` (where songs live,
default `data/`), `HOUSEBAND_PORT` (8795), `HOUSEBAND_HOST` (127.0.0.1),
`HOUSEBAND_ENGINE` (`modal` | `local`), `HOUSEBAND_YUE_PYTHON`.

## The lyrics editor

Words that don't fit the melody get rushed, dropped, or improvised by the
singer. **Melody fit** (in *Edit Lyrics & Stanzas*) pairs each lyric section
with the sung sections of your melody and shows its syllables against the room
the melody has, phrase by phrase, with a colored bar (green 85–115%, amber
70–130%, red beyond). **Clean up** fixes section tags, drops draft notes and
(backing) brackets, and spells out digits; **Split long lines** breaks lines
over N syllables at commas, sentence ends and run-on capitals. Neither changes
your words, and nothing is saved until **Save Lyrics**. The melody score is kept
with the song after its first take (`data/songs/<id>/score.abc`), so a song
with no take yet shows a note instead of the cards; if the automatic pairing
is off (an instrumental section, a repeated chorus), pick the melody section on
the card. Keeping the same line count keeps synced word timestamps.

## How it works

Per song, once (cached):

1. **SheetSage2** transcribes the demo's melody to sheet music (ABC, melody
   only, no chords), which also gives tempo and key;
2. **Whisper large-v3** transcribes the lyrics, unless you typed them or
   LRCLIB had them;
3. **CLAP** decides which style the demo already sounds closest to (used by
   *Keep Original Style*).

Per style:

4. **YuE2** (`cot="melody"`) performs that score with the lyrics in the style's
   description: the tune is fixed by the score; the arrangement, harmony, band
   and singer are new. The take is trimmed where your demo ends. Style strength
   is YuE2 `cfg_scale` 3.0, picked by ear.

Style descriptions: `app/styles.py` (custom ones in `data/custom_styles.json`).
Worker: `worker_yue/yue_cover.py`. App: `app/server.py` (FastAPI) and one page,
`app/static/index.html`, with the lyric helpers in `app/lyrics-lib.js`
(inlined into the page; `tests/check_page.py` keeps the copies identical).

Checks: `python tests/check_page.py`, `node --test tests/lyrics-lib.test.cjs`.

## Where this is going

Songwriter tools, in the same spirit (suggest, never author): a coach that
points at weak spots and asks questions instead of handing over lines, a
stress-vs-beat prosody check, named versions with A/B, lyric and chord sheet
export. Houseband Cloud for people who'd rather not set up a GPU.

## Licenses

This code: Apache-2.0. YuE2 model weights are CC BY-NC 4.0 with the authors'
creator permission (personal use and creators' monetised outputs allowed;
companies need a licence). SheetSage2, Whisper and CLAP keep their own model
licenses; read them before you ship anything built on this.
