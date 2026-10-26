"""The styles a song can be covered in, written for YuE2.

YuE2 reads `style` as: language, vocal character, genre, tempo, instruments,
production (docs/generation.md in the YuE repo). Each template below fills
{lang} (from the lyrics), {singer} ("male "/"female "/"" from the app's
Singer choice) and {bpm} (from the song's transcribed score, so the
description agrees with the tempo the cover is played at).

`label` is the plain genre name: what the "Keep Original Style" step
compares the original against (CLAP), and what the UI shows under the name.
"""

import json
import os
import re
from pathlib import Path

ORIGINAL = "original"
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("SONGWRITER_STUDIO_DATA", ROOT / "data"))
CUSTOM_FILE = DATA / "custom_styles.json"

STYLES = [
    # id, name, group, label, YuE2 style template
    (ORIGINAL, "Keep Original Style", "Original", "the song's own genre", ""),
    ("acoustic_folk", "Acoustic Folk", "Folk & Country", "acoustic folk",
     "{lang}, warm intimate {singer}vocal, acoustic folk, {bpm}, fingerpicked acoustic guitar, upright bass, "
     "light hand percussion, harmonica, organic live room recording"),
    ("jazz_funk", "Jazz-Funk", "Funk & Soul", "jazz funk",
     "{lang}, smooth soulful {singer}vocal, jazz-funk fusion, {bpm}, slap bass, Rhodes electric piano, "
     "tight syncopated drums, horn section stabs, wah guitar, 1970s studio sound"),
    ("funk_70s", "70s Funk", "Funk & Soul", "1970s funk",
     # "sounds more like 70s soul, needs to be way funkier" (2026-10-12): lead with
     # the groove, not a soulful voice
     "{lang}, raw shouted {singer}funk vocal with grunts and gang chants, hard-driving 1970s funk, {bpm}, "
     "syncopated tight drum groove hitting on the one, loud slap-and-pop bass up front, choppy wah-wah "
     "chicken-scratch guitar, stabbing horn section riffs, clavinet, dry punchy mix"),
    ("disco", "Disco", "Funk & Soul", "1970s disco",
     "{lang}, soulful {singer}disco vocal with lush backing harmonies, 1970s disco, {bpm}, four-on-the-floor "
     "kick with open hi-hat offbeats, octave-jumping funky bass, chicken-scratch rhythm guitar, sweeping string "
     "section, brass stabs, glittering studio production"),
    ("soul_50s", "50s Soul", "Funk & Soul", "1950s soul doo-wop",
     "{lang}, heartfelt crooning {singer}vocal, 1950s soul and doo-wop, {bpm}, shuffle drums, upright bass, "
     "piano, saxophone, vocal harmony group backing, vintage mono recording"),
    ("synthwave", "Synthwave", "Electronic", "synthwave",
     "{lang}, reverb-drenched {singer}vocal, synthwave, {bpm}, analog polysynth pads, pulsing arpeggiated "
     "bassline, gated reverb drums, retro 1980s neon mood"),
    ("modern_country", "Modern Country", "Folk & Country", "modern country",
     "{lang}, earnest country {singer}vocal with a slight drawl, modern Nashville country, {bpm}, twangy "
     "telecaster, pedal steel, banjo accents, acoustic strumming, punchy polished radio production"),
    ("west_coast_90s", "90's West Coast Hip Hop", "Hip Hop", "1990s west coast g-funk hip hop",
     "{lang}, laid-back rapped {singer}vocal with a smooth sung hook, 1990s West Coast G-funk hip hop, {bpm}, "
     "boom bap drums, deep Moog bass, high whining synth lead, talk box"),
    ("pop_90s", "90's Pop", "Pop", "1990s pop",
     "{lang}, bright catchy {singer}pop vocal with layered harmonies, 1990s pop, {bpm}, shiny synths, "
     "programmed drums with crisp snare, glossy radio-ready production"),
    ("punk_rock", "Punk Rock", "Rock", "punk rock",
     "{lang}, shouted energetic {singer}vocal with gang backing shouts, punk rock, {bpm}, fast distorted "
     "power chords, driving eighth-note bass, loud aggressive drums, raw garage production"),
    ("garage_revival", "Garage Rock Revival", "Rock", "garage rock revival",
     "{lang}, cool detached {singer}vocal, 2000s garage rock revival, {bpm}, jangly overdriven guitars, "
     "simple tight drums, catchy riffs, lo-fi live band recording"),
    ("gospel_choir", "Gospel Choir", "Funk & Soul", "gospel choir",
     "{lang}, soaring passionate {singer}lead vocal with full church choir responses, gospel, {bpm}, "
     "Hammond organ, piano, hand claps, tambourine, live church reverb"),
    ("hair_metal", "Hair Metal", "Metal", "1980s hair metal",
     "{lang}, high soaring {singer}rock vocal with anthemic gang choruses, 1980s hair metal glam rock, {bpm}, "
     "crunchy distorted guitars, shredding guitar solo, big gated drums, arena reverb"),
    ("heavy_metal", "Heavy Metal", "Metal", "heavy metal",
     "{lang}, powerful belted {singer}metal vocal, heavy metal, {bpm}, palm-muted distorted guitar riffs, "
     "double kick drums, galloping bass, dual harmony guitar leads, thick loud production"),
    ("bluegrass", "Bluegrass", "Folk & Country", "bluegrass",
     "{lang}, high lonesome {singer}vocal with close harmony, bluegrass, {bpm}, banjo rolls, flatpicked "
     "acoustic guitar, fiddle, mandolin chop, upright bass, no drums, live acoustic recording"),
    ("city_pop", "City Pop", "Pop", "japanese city pop",
     "{lang}, breezy romantic {singer}vocal, 1980s Japanese city pop, {bpm}, smooth funky bass, bright "
     "electric piano, clean chorus guitar, lush brass and strings, glossy production"),
    ("midwest_emo", "Midwest Emo", "Rock", "midwest emo",
     "{lang}, earnest strained emotional {singer}vocal, midwest emo, {bpm}, twinkly clean arpeggiated "
     "guitars, mathy drums, melodic bass, intimate basement recording"),
    ("dark_electro_rock", "Dark Electro-Rock / 90s Synth-Wave Noir", "Electronic", "dark electro rock",
     "{lang}, low moody {singer}vocal with heavy reverb, dark electro-rock, 1990s synth noir, {bpm}, brooding "
     "analog synth bass, distorted guitars, cold drum machine, cinematic tension"),
    ("gothic_dreampop", "Gothic Rock / Post-Punk Dream-Pop", "Rock", "gothic rock dream pop",
     "{lang}, ethereal haunting {singer}vocal, gothic rock and atmospheric post-punk dream pop, {bpm}, "
     "chorus-drenched shimmering guitars, melodic driving bass, cavernous reverb, melancholy"),
    ("industrial", "Industrial", "Metal", "industrial rock",
     # toward Nine Inch Nails ("more nine inch nails sounding", 2026-10-12)
     "{lang}, intense {singer}vocal from whispered menace to distorted screams, 1990s industrial rock, {bpm}, "
     "gritty distorted drum machine beats, harsh bitcrushed synth bass, walls of noisy distorted guitar, glitchy "
     "metallic samples, dark brooding verses building to aggressive climaxes, abrasive analog production"),
    ("grunge", "Grunge", "Rock", "grunge",
     "{lang}, raspy angst-filled {singer}vocal, 1990s Seattle grunge, {bpm}, sludgy fuzz guitars, quiet "
     "verse loud chorus dynamics, heavy loose drums, raw production"),
    ("prog_rock", "Prog Rock", "Rock", "progressive rock",
     # The song's melody and 4/4 timing are fixed by the score, so prog's structure
     # (odd meters, long solos) can't happen; asking for "complex drumming, epic
     # arrangement" at cfg 3.0 made a mess ("terrible", 2026-10-12). Describe the
     # sound instead.
     "{lang}, clear soaring {singer}rock vocal, 1970s symphonic progressive rock, {bpm}, lush Mellotron strings, "
     "Hammond organ, analog Moog synthesizer solos, melodic electric guitar, melodic bass, dynamic tight rock "
     "drums, warm analog production"),
    ("nu_metal", "Nu Metal", "Metal", "nu metal",
     # word for word the description of the take Paul picked (cfg 3.0, 2026-10-12)
     "{lang}, aggressive {singer}nu metal vocal, nu metal, {bpm}, heavily distorted downtuned seven-string "
     "guitars, chugging palm-muted riffs, slap bass, hip hop groove drums, turntable scratches, rapped verses "
     "and screamed chorus, dark heavy production"),
    # beyond the requested list
    ("reggae", "Roots Reggae", "World", "roots reggae",
     "{lang}, relaxed soulful {singer}vocal, roots reggae, {bpm}, offbeat skank guitar, deep dub bass, "
     "one drop drums, organ bubble, warm analog mix with dub echoes"),
    ("bossa_nova", "Bossa Nova", "Jazz", "bossa nova",
     "{lang}, soft whispered intimate {singer}vocal, bossa nova, {bpm}, nylon string guitar, brushed drums, "
     "upright bass, gentle flute, relaxed Brazilian rhythm"),
    ("motown", "Motown", "Funk & Soul", "motown soul",
     "{lang}, joyful {singer}soul vocal with harmony backing vocals, 1960s Motown, {bpm}, tambourine on every "
     "beat, melodic bass, string section, horns, vibraphone"),
    ("big_band", "Big Band Swing", "Jazz", "big band swing",
     "{lang}, smooth crooner {singer}vocal, 1940s big band swing, {bpm}, full horn section, walking bass, "
     "swinging ride cymbal, piano comping, ballroom recording"),
    ("lofi_hiphop", "Lo-fi Hip Hop", "Hip Hop", "lo-fi hip hop",
     "{lang}, soft hushed {singer}vocal, lo-fi hip hop, {bpm}, dusty vinyl crackle, mellow jazzy piano chords, "
     "lazy swung boom bap drums, warm tape saturation"),
    ("delta_blues", "Delta Blues", "Folk & Country", "delta blues",
     "{lang}, gritty weathered {singer}blues vocal, delta blues, {bpm}, slide resonator guitar, stomping foot "
     "percussion, harmonica, raw front porch recording"),
    ("edm_house", "EDM / Festival House", "Electronic", "edm house",
     "{lang}, euphoric processed {singer}vocal, festival progressive house, {bpm}, four on the floor kick, "
     "huge supersaw lead, sidechain pumping, build ups and drops"),
    ("cinematic", "Orchestral Cinematic", "Orchestral", "orchestral film score",
     "{lang}, soaring emotional {singer}vocal, epic orchestral cinematic, {bpm}, full string section, French "
     "horns, timpani, choir, film score arrangement"),
    ("salsa", "Latin Salsa", "World", "salsa",
     "{lang}, passionate {singer}vocal with coro responses, salsa, {bpm}, piano montuno, congas and timbales, "
     "clave rhythm, bright brass section, tumbao bass"),
    ("kpop", "K-Pop", "Pop", "k-pop",
     "{lang}, polished layered {singer}idol pop vocal, modern K-pop, {bpm}, punchy trap-influenced beat, "
     "glossy synths, dramatic genre-switching sections, high-energy production"),
    ("drum_and_bass", "Drum & Bass", "Electronic", "drum and bass",
     "{lang}, airy soulful {singer}vocal, liquid drum and bass, {bpm}, fast breakbeats, rolling reese bass, "
     "atmospheric pads"),
    ("ska_punk", "Ska Punk", "Rock", "ska punk",
     "{lang}, energetic shouted {singer}vocal, 1990s ska punk, {bpm}, upstroke offbeat guitars, trumpet and "
     "trombone section, fast punk drums"),
]

# How hard the style description is followed: YuE2's text guidance
# (cfg_scale; its own default is 1.0). Paul's ear test on Helena -> Nu Metal
# at 1.0/1.5/2.0/3.0 (2026-10-12): 3.0 was clearly the most like the genre.
# A follow-up at 4.0 and 5.0 (and a reworded description) was not better:
# 3.0 with these descriptions is THE setting ("just stick with that").
STRENGTH = {"strong": 3.0}
DEFAULT_STRENGTH = "strong"
SINGERS = ("", "male", "female")
BUILTIN_IDS = {s[0] for s in STYLES}


def _load_custom() -> dict[str, dict]:
    try:
        rows = json.loads(CUSTOM_FILE.read_text(encoding="utf-8"))
        return {r["id"]: r for r in rows if isinstance(r, dict) and r.get("id")}
    except (OSError, ValueError):
        return {}


def _save_custom(by_id: dict[str, dict]) -> None:
    CUSTOM_FILE.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_FILE.write_text(json.dumps(list(by_id.values()), indent=2), encoding="utf-8")


def all_styles() -> list[tuple[str, str, str, str, str, bool]]:
    """[(id, name, group, label, template, is_custom_or_modified)]"""
    custom = _load_custom()
    out = []
    for sid, name, grp, lbl, tmpl in STYLES:
        if sid in custom and sid != ORIGINAL:
            c = custom[sid]
            out.append((sid, c.get("name") or name, c.get("group") or grp,
                        c.get("label") or lbl, c.get("template") or tmpl, True))
        else:
            out.append((sid, name, grp, lbl, tmpl, False))
    for sid, c in custom.items():
        if sid not in BUILTIN_IDS and sid != ORIGINAL:
            out.append((sid, c.get("name") or sid, c.get("group") or "Custom",
                        c.get("label") or (c.get("name") or sid).lower(), c.get("template") or "", True))
    return out


def get(style_id: str):
    for s in all_styles():
        if s[0] == style_id:
            return s[:5]
    raise KeyError(style_id)


def as_list() -> list[dict]:
    return [{"id": i, "name": n, "group": g, "caption": lbl if i != ORIGINAL else "",
             "template": t, "custom": cust, "builtin": i in BUILTIN_IDS}
            for i, n, g, lbl, t, cust in all_styles()]


def genres() -> dict[str, str]:
    """style id → plain genre label, for matching the original's genre."""
    return {i: lbl for i, _n, _g, lbl, _t, _c in all_styles() if i != ORIGINAL}


def templates() -> dict[str, str]:
    return {i: t for i, _n, _g, _l, t, _c in all_styles() if i != ORIGINAL}


def take_for(style_id: str, strength: str, seed: int) -> dict:
    sid, name, _g, _lbl, template = get(style_id)
    return {"id": sid, "name": name, "style": template, "seed": seed,
            "cfg": STRENGTH.get(strength, STRENGTH[DEFAULT_STRENGTH])}


def upsert_style(style_id: str | None, name: str, group: str, label: str, template: str) -> dict:
    name, group, template = name.strip(), (group.strip() or "Custom"), template.strip()
    label = (label.strip() or name.lower())
    if not name or not template:
        raise ValueError("Style name and prompt template are required")
    sid = style_id or re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:32]
    if not sid or sid == ORIGINAL:
        raise ValueError("Invalid style id")
    custom = _load_custom()
    custom[sid] = {"id": sid, "name": name, "group": group, "label": label, "template": template}
    _save_custom(custom)
    return next(x for x in as_list() if x["id"] == sid)


def reset_or_delete_style(style_id: str) -> bool:
    custom = _load_custom()
    if style_id not in custom:
        return False
    del custom[style_id]
    _save_custom(custom)
    return True
