#!/usr/bin/env python3
from __future__ import annotations
"""
yarg_sync.py — Auto-calibrate song.ini delay values for YARG song libraries.

Analyzes chart timing vs. audio onsets to compute the correct `delay` value for
each song, then writes it back. Supports plain song folders, Xbox 360 CON
packages (Rock Band / Fortnite Festival), and Clone Hero/YARG SNG archives.

CON packages are fully extracted: STFS filesystem parsed, songs.dta converted
to song.ini, multitrack MOGG split into OGG stems, .png_xbox art decoded.

Usage:
    python yarg_sync.py /path/to/library [--dry-run] [--debug] [--workers N] [--max-delay N]
    python yarg_sync.py /path/to/library --skip-inconclusive --delete-cons --delete-sngs
    python yarg_sync.py /path/to/library --skip-existing --skip-existing-inconclusive

Output per song: song.ini updated, yarg_sync_list.json updated with delay+confidence.

Dependencies: pip install librosa numpy soundfile scipy pillow texture2ddecoder
"""

# --- Startup: single-thread C libs BEFORE numpy/librosa/scipy initialize ---
import os
os.environ.update({
    "OMP_NUM_THREADS":        "1",
    "OPENBLAS_NUM_THREADS":   "1",
    "MKL_NUM_THREADS":        "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS":    "1",
})

try:
    import subprocess
    import io
    import argparse
    import configparser
    import json
    import re
    import struct
    import sys
    import threading
    import time
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from typing import Any
    import hashlib
    from threading import Lock
    import gc
    import shutil
    import numpy as np
    import librosa
    import soundfile as sf
    import scipy.signal as spsignal
    import ExtractCONSNG
except ImportError as e:
    pkg = getattr(e, "name", None)

    print(f"Error: {e}")
    print("-" * 40)

    if pkg == "ExtractCONSNG":
        print("Could not find the local module 'ExtractCONSNG.py'.")
        print("Make sure it is in the same folder as this script.")
    else:
        print("One or more required Python packages are missing.")
        print("Install them with:")
        print()
        print("python -m pip install librosa numpy soundfile scipy pillow texture2ddecoder")
        print()

    print("-" * 40)
    sys.exit(1)


# --- Constants ---

# Delay search
MAX_DELAY_MS = 200     # initial search range (ms)
COARSE_STEP = 3        # coarse scan spacing before fine refinement
FINE_STEP = 1          # fine scan spacing
EXTEND_LIMIT = 5       # can extend to ±(EXTEND_LIMIT × MAX_DELAY_MS)
TOLERANCE_MS = 15      # onset match window (ms) — overridden by --tolerance
SOFT_SCORE_POWER = 2   # parameter to control the shape of the soft score curve
HOP_LENGTH = 512       # librosa hop length for onset detection — overridden by --hop-length
BOUNDARY_BONUS = False # whether to apply extra weight to first/last 5% of chart notes
BOUNDARY_PCT = 0.02    # fraction of notes at each end that receive the boundary bonus
STREAK_BONUS = False   # whether to apply a multiplier to notes that are isolated
STREAK_BASE = 5.0     # the base of the sigmoid used (\frac{1-a^{-x}}{1+a^{-x}}*(a+1)/(a-1)) TODO: ?? I THINK ??

# Audio - lowpass emphasizes kick/bass onsets; HPSS isolates percussive transients
LOWPASS_HZ = 200     # lowpass cutoff
SR = 22050           # target sample rate
AUDIO_CLIP_S = 1800  # max seconds of audio to read (None = unlimited) (1800 = 30 minutes)

# Display / runtime
MAX_SONG_NAME = 30
USE_INI = True
if os.name == 'nt':
    APPDATA_ROAMING = Path(os.environ["APPDATA"])
    MODIF_PATH = APPDATA_ROAMING.parent / 'LocalLow' / 'YARC' / 'YARG' / 'nightly' / 'song_offsets.json'
    print('Json file for offsets exists:', MODIF_PATH.is_file())
    if MODIF_PATH.is_file():
        USE_INI = False
else:
    MODIF_PATH = None
    print("You are running a non-Windows OS.")
print(f'Path for values: {MODIF_PATH}, Use ini files: {USE_INI}.')
LIST_PATH = Path(__file__).parent / "yarg_sync_list.json"
WORKER_THREADS = 6
ETA_EMA_ALPHA = 0.1

# File format magic bytes
CON_MAGIC = b"CON "
SNG_MAGIC = b"SNGPKG"
EXTENSION_LIST = [".ogg", ".opus", ".mp3", ".wav"]
OTHER_EXTENSIONS = [".ini", ".mid", ".bak", ".webm", ".mp4", ".png"]
EXCLUDED_CON_SUFFIXES = frozenset(EXTENSION_LIST + OTHER_EXTENSIONS)

# STFS block geometry
_STFS_BLOCK = 0x1000
_STFS_BASE = 0xC000   # data area start; block 0 is always at 0xC000

# Thresholds
TOTAL_PCT_MIN = 0.1  # overridden to 0.2 in hard-score mode

CACHE_PATH = Path(__file__).parent / ".mogg_cache"
CACHE_PATH.mkdir(parents=True, exist_ok=True)


# --- Terminal color ---

def _setup_color() -> bool:
    if os.name == "nt":
        import ctypes
        try:
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            m = ctypes.c_ulong()
            k.GetConsoleMode(h, ctypes.byref(m))
            k.SetConsoleMode(h, m.value | 0x0004)
            return True
        except Exception:
            return False
    return sys.stdout.isatty()


_COLOR = _setup_color()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text

def green(t):  return _c("32", t)
def yellow(t): return _c("33", t)
def red(t):    return _c("31", t)
def bold(t):   return _c("1",  t)
def dim(t):    return _c("2",  t)
def cyan(t):   return _c("36", t)

def dprint(debug: bool, msg: str, always_print: bool = False) -> None:
    """Print msg always if always_print, or prefixed with [debug] when debug is on."""
    if always_print:
        print(msg)
    elif debug:
        print(f"      [debug] {msg}")


# --- Score formatting ---

def format_match_quadruplet(score: float, needed: float, nb_onsets: int, total: int, dimmed=True) -> str:
    score_str = green(f"{score:.2f}") if score >= needed else red(f"{score:.2f}")
    needed_str = f"{needed:.2f}"
    if dimmed:
        return f"{score_str}\033[2m/{needed_str}/{nb_onsets}/{total}"
    return f"{score_str}/{needed_str}/{nb_onsets}/{total}"


def format_score_percentage(score: float, needed: float, nb_onsets: int, total: int, dimmed=True) -> str:
    pct_needed = score / needed if needed > 0 else 0
    pct_onsets = score / nb_onsets if nb_onsets > 0 else 0
    pct_total  = score / total if total > 0 else 0
    s_needed = green(f"{pct_needed:.2%}") if pct_needed >= 1.0 else red(f"{pct_needed:.2%}")
    s_onsets = green(f"{pct_onsets:.2%}") if pct_onsets >= 0.5 else red(f"{pct_onsets:.2%}")
    s_total  = green(f"{pct_total:.2%}") if pct_total >= 0.5 else red(f"{pct_total:.2%}")
    if dimmed:
        return f"{s_needed}\033[2m, {s_onsets}\033[2m, {s_total}\033[2m"
    return f"{s_needed}, {s_onsets}, {s_total}"


# --- JSON list helpers (yarg_sync_list.json) ---

def load_hash_list() -> dict[str, int]:
    print(f"Checking for existing entries in {MODIF_PATH}...")
    if not MODIF_PATH.exists():
        print(f"No existing list file found at {MODIF_PATH}. Starting fresh.")
        return {}
    try:
        with MODIF_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"Found {len(data)} existing entries in the {MODIF_PATH.name} file. (Last: {list(data.keys())[-1]})")
        for i, x in enumerate(data):
            print(x, data[x])
            if i > 3-2: # print only 3 values
                print('...\n')
                break
        return data
    except Exception as e:
        print(red(f"Error reading list file: {e}. Starting fresh."))
        return {}

def load_list() -> dict[str, Any]:
    print(f"Checking for existing entries in {LIST_PATH}...")
    if not LIST_PATH.exists():
        print(f"No existing list file found at {LIST_PATH}. Starting fresh.")
        return {}
    try:
        with LIST_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"Found {len(data)} existing entries in the {LIST_PATH.name} file.")
        for i, x in enumerate(data):
            print(x, data[x])
            if i > 3-2: # print only 3 values
                print('...\n')
                break
        return data
    except Exception as e:
        print(red(f"Error reading list file: {e}. Starting fresh."))
        return {}


def save_list(data: dict[str, Any]) -> None:
    with LIST_PATH.open("w", encoding="utf-8") as f:
        json.dump(dict(sorted(data.items())), f, indent=2)


def add_to_list(data: dict[str, Any], key: str, delay: int, ratio: float) -> None:
    data[key] = [delay, ratio]


def remove_from_list(data: dict[str, Any], key: str, debug: bool, always_print: bool = True) -> None:
    for suffix in [" [clustered] [inconclusive]", " [inconclusive]", " [clustered]", ""]:
        full = key + suffix
        if full in data:
            if debug or always_print:
                print(f"Removing {full} from recorded list.")
            del data[full]


# --- JSON list key helpers ---
def _entry_needs_processing(existing: dict, key: str) -> bool:
    """Return True if entry is missing, legacy int-only, or has low confidence (<TOTAL_PCT_MIN)."""
    actual_key = key if key in existing else key.replace("_extracted", "")
    if actual_key not in existing:
        return True
    value = existing[actual_key]
    return isinstance(value, int) or value[1] < TOTAL_PCT_MIN


# --- Song / audio discovery ---


def count_audio_files(debug: bool, always_print: bool, songs: list[Path] | None = None) -> int:
    count = 0
    if songs is None:
        return count
    for ini_path in songs:
        folder = ini_path.parent
        count += 1 if any(folder.glob('*.mogg')) else sum(len(list(folder.glob(f'*{extension}'))) for extension in EXTENSION_LIST)

    dprint(debug, f"Number of audio files to process: {count}", always_print=always_print)
    return count


def song_key(
    path: Path,
    *,
    clustered: bool = False,
    inconclusive: bool = False,
    skip_existing_inconclusive: bool = False,
    skip_existing_clustered: bool = False,
) -> str:
    """Return the canonical key used in yarg_sync_list.json."""

    if path.suffix == ".ini":
        base = f"{path.parent.parent.name.lower()} / {path.parent.name.lower()}"

    else:
        # CON extractions are often placed in a folder named after the package.
        # Avoid generating keys like "Artist / Song / Song" by collapsing the
        # duplicated directory level when it exists.
        parent = (
            path.parent.parent
            if path.parent.name == path.stem
            else path.parent
        )

        name = (
            path.stem.lower()
            if path.suffix == ".sng"
            else path.parts[-1].lower()
        )

        base = f"{parent.name.lower()} / {name}"

    suffix = (
        (" [clustered]" if clustered or skip_existing_clustered else "") +
        (" [inconclusive]" if inconclusive or skip_existing_inconclusive else "")
    )

    return base + suffix


def get_song_list(
    root: Path,
    skip_extracted: bool = False,
) -> tuple[list[Path], int, int, int, list, list, list]:

    cons = sorted(
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix not in EXCLUDED_CON_SUFFIXES
        and ExtractCONSNG.is_con_file(p)
    )
    sngs = sorted(root.rglob("*.sng"))

    # Always exclude _extracted INIs whose parent CON/SNG is present
    # — pre_extract_all handles those, they'll be added back via extracted_map
    excluded_dirs = {Path(str(p) + "_extracted") for p in cons} | \
                    {Path(str(p) + "_extracted") for p in sngs}

    inis = sorted(
        p for p in root.rglob("song.ini")
        if not (skip_extracted and ExtractCONSNG.is_extracted_path(p))
        and p.parent not in excluded_dirs
    )

    songs: list[Path] = sorted(inis + cons + sngs)
    return songs, len(inis), len(cons), len(sngs), inis, cons, sngs


# --- Audio filtering ---

_LOWPASS_SOS = spsignal.butter(4, LOWPASS_HZ / (SR / 2), btype="low", output="sos")


def apply_lowpass(y: np.ndarray) -> tuple[str, np.ndarray]:
    try:
        return "lowpass", np.asarray(spsignal.sosfiltfilt(_LOWPASS_SOS, y), dtype=np.float32)
    except Exception:
        return "lowpass", y


def _mogg_candidates(
    folder: Path,
    debug: bool,
) -> tuple[list[Path], str]:
    """Extract the preferred MOGG into a cached folder and return its parts."""
    mogg_files = sorted(folder.glob("*.mogg"))
    if not mogg_files:
        return [], ''

    mogg_path = mogg_files[0]
    dta_path = Path(str(mogg_path) + ".dta")
    song_info = None

    if dta_path.is_file():
        try:
            dta_text = dta_path.read_text(encoding="utf-8", errors="ignore")
            grouped = ExtractCONSNG._parse_songs_dta_grouped(dta_text)
            metadata = next(iter(grouped.values()), None)
            song_info = metadata.get("song") if isinstance(metadata, dict) else None
        except Exception as exc:
            dprint(debug, f"Could not parse MOGG DTA {dta_path.name}: {exc}")

    hash_value = hashlib.sha1(mogg_path.read_bytes()).hexdigest().upper()
    cache_folder = CACHE_PATH / hash_value
    cache_folder.mkdir(parents=True, exist_ok=True)

    ExtractCONSNG._split_mogg(
        mogg_path.read_bytes(),
        cache_folder,
        debug,
        song_info=song_info,
        output_format="wav",
    )

    candidates = ExtractCONSNG.find_audio_candidates(cache_folder)

    return candidates, hash_value


# --- Chart parsing — .chart format ---

def _parse_resolution(text: str) -> int:
    m = re.search(r"Resolution\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else 192


def _parse_bpm_map(text: str) -> list[tuple[int, float]]:
    m = re.search(r"\[SyncTrack\]\s*\{([^}]*)\}", text, re.DOTALL)
    if not m:
        return [(0, 120.0)]
    bpms = [(int(b.group(1)), int(b.group(2)) / 1000.0)
            for b in re.finditer(r"(\d+)\s*=\s*B\s+(\d+)", m.group(1))]
    return sorted(bpms) if bpms else [(0, 120.0)]


def _ticks_to_ms(ticks: np.ndarray, resolution: int, bpm_map: list) -> np.ndarray:
    if len(ticks) == 0:
        return np.array([], dtype=np.float64)
    seg_ticks = np.array([t for t, _ in bpm_map], dtype=np.float64)
    seg_bpms = np.array([b for _, b in bpm_map], dtype=np.float64)
    seg_ms = np.zeros(len(seg_ticks), dtype=np.float64)
    for i in range(1, len(seg_ticks)):
        seg_ms[i] = seg_ms[i-1] + (seg_ticks[i] - seg_ticks[i-1]) / resolution * (60000.0 / seg_bpms[i-1])
    idx = np.clip(np.searchsorted(seg_ticks, ticks, side="right") - 1, 0, len(seg_ticks) - 1)
    return seg_ms[idx] + (ticks - seg_ticks[idx]) / resolution * (60000.0 / seg_bpms[idx])


_NOTE_RE = re.compile(r"^\s*(\d+)\s*=\s*N\s+\d+\s+\d+", re.MULTILINE)

# Maps .chart track suffixes (after stripping difficulty prefix) to MIDI-style part names
# so that midi_parts_for_stem() works the same way for both .chart and .mid files.
_CHART_SUFFIX_TO_PART = {
    "Single":       "PART GUITAR",
    "GHLGuitar":    "PART GUITAR",
    "DoubleBass":   "PART BASS",
    "Bass":         "PART BASS",
    "GHLBass":      "PART BASS",
    "Drums":        "PART DRUMS",
    "DoubleDrum":   "PART DRUMS",
    "Keyboard":     "PART KEYS",
    "Vocals":       "PART VOCALS",
    "DoubleRhythm": "PART RHYTHM",
}
_CHART_DIFF_PREFIXES = ("Expert", "Hard", "Medium", "Easy")
_CHART_SKIP_SECTIONS = {"Song", "SyncTrack", "Events"}


def notes_from_chart(chart_path: Path) -> tuple[dict[str, np.ndarray], str]:
    """Parse a .chart file and return a dict of MIDI-style part name -> ms timestamps,
    so that midi_parts_for_stem() can do stem-matched calibration just like for .mid files."""
    try:
        text = chart_path.read_text(encoding="utf-8", errors="replace")
        with open(chart_path, "rb") as f:
            # Compute the SHA-256 digest directly from the file object
            hash_value = hashlib.file_digest(f, "sha1").hexdigest().upper()
        resolution = _parse_resolution(text)
        bpm_map = _parse_bpm_map(text)
        sections = {m.group(1): m.group(2)
                    for m in re.finditer(r"\[(\w+)\]\s*\{([^}]*)\}", text, re.DOTALL)}
        part_ticks: dict[str, set[int]] = {}
        for track_name, body in sections.items():
            if track_name in _CHART_SKIP_SECTIONS:
                continue
            suffix = track_name
            for prefix in _CHART_DIFF_PREFIXES:
                if track_name.startswith(prefix):
                    suffix = track_name[len(prefix):]
                    break
            part = _CHART_SUFFIX_TO_PART.get(suffix)
            if part is None:
                continue
            ticks = {int(m.group(1)) for m in _NOTE_RE.finditer(body)}
            if ticks:
                part_ticks.setdefault(part, set()).update(ticks)
        return {
            part: _ticks_to_ms(np.array(sorted(ticks), dtype=np.float64), resolution, bpm_map)
            for part, ticks in part_ticks.items()
        }, hash_value
    except Exception:
        return {}, ""


# --- Chart parsing — MIDI format ---

def _read_vlq(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        if pos >= len(data):
            raise EOFError(f"VLQ read past end of track (pos={pos}, len={len(data)})")
        b = data[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, pos


def _parse_midi_track(track: bytes) -> list:
    events = []
    p = tick = 0
    running_status = 0
    while p < len(track):
        try:
            delta, p = _read_vlq(track, p)
        except EOFError:
            break
        tick += delta
        if p >= len(track):
            break
        b = track[p]
        if b & 0x80:
            status = b
            if (b & 0xF0) != 0xF0:
                running_status = b
            p += 1
        else:
            status = running_status
        try:
            if status == 0xFF:
                mtype = track[p]; p += 1
                mlen, p = _read_vlq(track, p)
                events.append(('meta', tick, mtype, track[p:p + mlen]))
                p += mlen
            elif (status & 0xF0) in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                b1 = track[p] if p < len(track) else 0
                b2 = track[p + 1] if p + 1 < len(track) else 0
                p += 2
                events.append(('midi2', tick, status, b1, b2))
            elif (status & 0xF0) in (0xC0, 0xD0):
                p += 1
            elif status in (0xF0, 0xF7):
                mlen, p = _read_vlq(track, p)
                p += mlen
        except (EOFError, IndexError):
            break
    return events


def _parse_midi_file(data: bytes) -> tuple[int, list, list] | None:
    """Parse MIDI header and all tracks. Returns (tpq, bpm_map, all_tracks) or None."""
    if data[:4] != b"MThd":
        return None
    _, _fmt, num_tracks, division = struct.unpack(">IHHH", data[4:14])
    if division & 0x8000:
        return None
    tpq = division
    pos = 14
    all_tracks = []
    for _ in range(num_tracks):
        if data[pos:pos + 4] != b"MTrk":
            break
        length = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        all_tracks.append(_parse_midi_track(data[pos + 8:pos + 8 + length]))
        pos += 8 + length
    tempo_map = [(0, 500000)]
    for e in (all_tracks[0] if all_tracks else []):
        if e[0] == 'meta' and e[2] == 0x51:
            tempo_map.append((e[1], struct.unpack(">I", b"\x00" + e[3][:3])[0]))
    bpm_map = [(t, 60000000.0 / u) for t, u in tempo_map]
    return tpq, bpm_map, all_tracks


def notes_from_mid(mid_path: Path, debug: bool) -> tuple[dict[str, np.ndarray], str]:
    try:
        parsed = _parse_midi_file(mid_path.read_bytes())
        with open(mid_path, "rb") as f:
            # Compute the SHA-256 digest directly from the file object
            hash_value = hashlib.file_digest(f, "sha1").hexdigest().upper()
        if parsed is None:
            return {}, ""
        tpq, bpm_map, all_tracks = parsed
        result: dict[str, np.ndarray] = {}
        for track_events in all_tracks[1:]:
            name_bytes: bytes | None = None
            note_ticks: set[int] = set()
            for e in track_events:
                if e[0] == 'meta' and e[2] == 0x03:
                    name_bytes = e[3]
                elif e[0] == 'midi2' and (e[2] & 0xF0) == 0x90 and e[4] > 0:
                    note_ticks.add(e[1])
            if note_ticks and name_bytes is not None:
                part = name_bytes.decode("utf-8", errors="replace")
                result[part] = _ticks_to_ms(np.array(sorted(note_ticks), dtype=np.float64), tpq, bpm_map)
        return result, hash_value
    except Exception as e:
        dprint(debug, f"exception in notes_from_mid: {e}")
        return {}, ""


# --- MIDI part selection per stem ---

_STEM_ALIASES = {
    "bass": "bass",
    "drum": "drums",
    "drums": "drums",
    "guitar": "guitar",
    "keys": "keys",
    "vocals": "vocals",
    "rhythm": "rhythm",
    "backing": "backing",
    "song": "backing",
}


def _normalized_part_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "", name.lower())
    normalized = re.sub(r"\d+$", "", normalized)
    if normalized.startswith("part"):
        normalized = normalized[4:]
    if normalized.startswith("real"):
        normalized = normalized[4:]
    if normalized.endswith("coop"):
        normalized = normalized[:-4]
    return normalized


def midi_parts_for_stem(stem_name: str, parts: dict[str, np.ndarray]) -> np.ndarray:
    stem_key = _STEM_ALIASES.get(_normalized_part_name(stem_name))
    for part_name, note_times in parts.items():
        part_key = _normalized_part_name(part_name)
        if stem_key is not None:
            if part_key == stem_key:
                return note_times
        elif part_key == _normalized_part_name(stem_name):
            return note_times
    return np.array([])


# --- Chart notes entry point ---

def get_chart_notes(folder: Path, debug: bool) -> tuple[dict[str, np.ndarray], str, str]:
    """Return chart notes as a dict of part name -> ms timestamps, and an error reason string.
    Both .chart and .mid return dicts so that midi_parts_for_stem() applies to both."""
    chart = folder / "notes.chart"
    mid = folder / "notes.mid"

    if not chart.exists() and not mid.exists():
        return {}, "no notes.chart or notes.mid found", ""

    if chart.exists():
        parts, hash_value = notes_from_chart(chart)
        if parts:
            all_times = np.unique(np.concatenate(list(parts.values())))
            if len(all_times) >= 8:
                return parts, "", hash_value

    if mid.exists():
        parts, hash_value = notes_from_mid(mid, debug)
        if parts:
            all_times = np.unique(np.concatenate(list(parts.values())))
            if len(all_times) >= 8:
                return parts, "", hash_value
            return {}, f"too few notes in chart ({len(all_times)}) to accurately calibrate (minimum is 8)", ""

    return {}, "no usable chart notes found", ""






def _is_extraction_cache_fresh(source: Path, dest: Path, require_chart_or_mid: bool = False) -> bool:
    if not ExtractCONSNG._has_audio(dest):
        return False
    if require_chart_or_mid:
        has_chart = (dest / "notes.mid").exists() or (dest / "notes.chart").exists()
        return has_chart and source.stat().st_mtime <= dest.stat().st_mtime
    return (dest / "notes.mid").exists() and source.stat().st_mtime <= dest.stat().st_mtime



# ---------------------------------------------------------------------------
# SNG extraction  (Clone Hero / YARG format)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Onset-to-note matching
# ---------------------------------------------------------------------------


def _boundary_bonus_weight(dist_ms: np.ndarray) -> np.ndarray:
    """Extra bonus for boundary notes (first/last 5% by count): spike shape near zero.
    Uses inverted exponents vs the base curve: 2*(1-(dist/T)^(1/2))^2.
    A perfectly aligned boundary note contributes +2.0 on top of the base score;
    any misalignment drops the bonus sharply, helping discriminate one-measure-off errors."""
    x = dist_ms / TOLERANCE_MS
    return 2.0 * np.maximum(0.0, 1.0 - x ** 0.5) ** 2.0


def _boundary_mask(chart_notes_ms: np.ndarray) -> np.ndarray:
    """Return a boolean mask marking the first and last BOUNDARY_PCT of notes by count."""
    n = len(chart_notes_ms)
    if n == 0:
        return np.zeros(n, dtype=bool)
    k = max(1, int(np.ceil(n * BOUNDARY_PCT)))
    mask = np.zeros(n, dtype=bool)
    mask[:k] = True
    mask[-k:] = True
    return mask


def _streak_bonus_weight(streak_nbs: np.ndarray) -> np.ndarray:
    """Return a multiplier bonus based on the length of the streak each note belongs to.
    This multiplier is given by a sigmoid function, tanh(ln(STREAK_BASE)*streak_nbs/2)."""
    tanh_arg = np.log(STREAK_BASE) / 2 * streak_nbs
    scaling_factor = (STREAK_BASE - 1) / (STREAK_BASE + 1)
    return np.tanh(tanh_arg) / scaling_factor

def _get_streak_nbs(dist_ms: np.ndarray) -> np.ndarray:
    """Return the length of the streak each note belongs to."""

    successes = _nearest_onset_mask(dist_ms)
    streak_nbs = np.ones(successes.size, dtype=int)

    if successes.any():
        idx = np.flatnonzero(successes)
        breaks = np.where(np.diff(idx) != 1)[0]

        starts = np.r_[0, breaks + 1]
        ends = np.r_[breaks + 1, len(idx)]

        for s, e in zip(starts, ends):
            streak_nbs[idx[s:e]] = e - s

    return streak_nbs


def _score_with_bonus(chart_notes_ms: np.ndarray,
                      audio_onsets_ms: np.ndarray,
                      dist_ms: np.ndarray,
                      debug: bool) -> float:
    """Soft score with optional boundary and streak modifiers."""
    weights = _soft_weight(dist_ms, debug)

    if STREAK_BONUS:
        streak_nbs = _get_streak_nbs(dist_ms)
        weights *= _streak_bonus_weight(streak_nbs)

    base = weights.sum()

    if not BOUNDARY_BONUS:
        return float(base)

    mask = _boundary_mask(chart_notes_ms)
    bonus_boundary = _boundary_bonus_weight(dist_ms[mask]).sum()

    return float(base + bonus_boundary)



def _soft_weight(dist_ms: np.ndarray, debug: bool) -> np.ndarray:
    x = dist_ms / TOLERANCE_MS
    return np.maximum(0.0, 1.0 - x**SOFT_SCORE_POWER) ** (1.0 / SOFT_SCORE_POWER)


def _hard_weight(dist_ms: np.ndarray, debug: bool) -> np.ndarray:
    return np.asarray(_nearest_onset_mask(dist_ms), dtype=np.float32)


def _soft_score_at(chart_notes_ms: np.ndarray,
                   audio_onsets_ms: np.ndarray,
                   delay: int,
                   debug: bool) -> float:
    dist = _nearest_onset_dist(audio_onsets_ms, chart_notes_ms + delay)
    return _score_with_bonus(chart_notes_ms, audio_onsets_ms, dist, debug)


def _hard_score_at(chart_notes_ms: np.ndarray,
                   audio_onsets_ms: np.ndarray,
                   delay: int,
                   debug: bool) -> int:
    dist = _nearest_onset_dist(audio_onsets_ms, chart_notes_ms + delay)
    return int(_hard_weight(dist, debug).sum())


def _nearest_onset_dist(audio_onsets_ms: np.ndarray, shifted_notes: np.ndarray) -> np.ndarray:
    """Return the distance (ms) from each shifted note to its nearest onset."""
    indices = np.searchsorted(audio_onsets_ms, shifted_notes)
    lo = np.clip(indices - 1, 0, len(audio_onsets_ms) - 1)
    hi = np.clip(indices,     0, len(audio_onsets_ms) - 1)
    return np.minimum(np.abs(audio_onsets_ms[lo] - shifted_notes),
                      np.abs(audio_onsets_ms[hi] - shifted_notes))


def _nearest_onset_mask(dist_ms: np.ndarray) -> np.ndarray:
    """Return True for notes whose nearest onset is within tolerance."""
    return dist_ms <= TOLERANCE_MS


def compute_match_details(audio_onsets_ms: np.ndarray, chart_notes_ms: np.ndarray,
                          delay: int) -> tuple[int, int, float]:
    shifted = chart_notes_ms + delay
    mask = _nearest_onset_mask(_nearest_onset_dist(audio_onsets_ms, shifted))
    matched = chart_notes_ms[mask]
    n = int(mask.sum())
    total = len(chart_notes_ms)
    if n < 2 or total < 2:
        return n, total, (1.0 if n > 0 else 0.0)
    try:
        std_dev_ratio = float(np.std(audio_onsets_ms, ddof=1)) / float(np.std(chart_notes_ms, ddof=1))
    except Exception:
        std_dev_ratio = 0.0
    return n, total, std_dev_ratio


# ---------------------------------------------------------------------------
# Delay estimation
# ---------------------------------------------------------------------------

def _score_delays(
    audio_onsets_ms: np.ndarray,
    chart_notes_ms: np.ndarray,
    delays: np.ndarray,
    weight_fn,
    bmask: np.ndarray | None,
    debug: bool,
) -> np.ndarray:
    shifted = chart_notes_ms[np.newaxis, :] + delays[:, np.newaxis]
    dist = _nearest_onset_dist(audio_onsets_ms, shifted)

    weights = weight_fn(dist, debug)

    if STREAK_BONUS:
        for i, dist_row in enumerate(dist):
            weights[i] *= _streak_bonus_weight(_get_streak_nbs(dist_row))

    scores = weights.sum(axis=1)

    if BOUNDARY_BONUS and bmask is not None:
        scores += _boundary_bonus_weight(dist[:, bmask]).sum(axis=1)

    return scores


def _explain_score(
    audio_onsets_ms: np.ndarray,
    chart_notes_ms: np.ndarray,
    delay: int,
    hard_score: bool,
) -> dict:
    """
    Return detailed information about the score for a single delay.

    This function is intended for debugging and tuning the calibration
    algorithm. It mirrors the scoring pipeline while exposing intermediate
    statistics that help explain *why* a particular delay scored well.
    """

    shifted = chart_notes_ms + delay
    insert = np.searchsorted(audio_onsets_ms, shifted)

    left = np.clip(insert - 1, 0, len(audio_onsets_ms) - 1)
    right = np.clip(insert, 0, len(audio_onsets_ms) - 1)

    left_dist = np.abs(audio_onsets_ms[left] - shifted)
    right_dist = np.abs(audio_onsets_ms[right] - shifted)

    use_right = right_dist < left_dist

    nearest_idx = np.where(use_right, right, left)
    dist = np.where(use_right, right_dist, left_dist)

    tolerance = TOLERANCE_MS

    matched = dist <= tolerance
    matched_dist = dist[matched]

    if hard_score:
        weights = _hard_weight(dist, False)
    else:
        weights = _soft_weight(dist, False)

    base_score = float(weights.sum())

    streak_bonus = 0.0
    if STREAK_BONUS:
        streak_weights = _streak_bonus_weight(_get_streak_nbs(dist))
        streak_bonus = float((weights * (streak_weights - 1.0)).sum())
        weights *= streak_weights

    boundary_bonus = 0.0
    if BOUNDARY_BONUS:
        bmask = _boundary_mask(chart_notes_ms)
        boundary_bonus = float(_boundary_bonus_weight(dist[bmask]).sum())

    total_score = float(weights.sum() + boundary_bonus)
    matched_idx = nearest_idx[matched]
    reuse_counts = np.bincount(matched_idx, minlength=len(audio_onsets_ms))
    max_reuse = int(reuse_counts.max()) if reuse_counts.size else 0
    unique_matched = len(np.unique(matched_idx))
    duplicate_matches = len(matched_idx) - unique_matched
    streak_pct = (
        streak_bonus / total_score
        if total_score > 0
        else 0.0
    )

    return {
        "score": total_score,
        "base_score": base_score,
        "streak_bonus": streak_bonus,
        "streak_pct": streak_pct,
        "boundary_bonus": boundary_bonus,

        "notes_within_tolerance": int(matched.sum()),
        "total_notes": len(chart_notes_ms),

        "perfect_matches": int(np.sum(matched & (dist <= 5))),
        "good_matches": int(np.sum(matched & (dist > 5) & (dist <= 10))),
        "acceptable_matches": int(np.sum(matched & (dist > 10))),
        "misses": int(np.sum(~matched)),

        "average_error": (
            float(matched_dist.mean())
            if matched_dist.size else None
        ),
        "median_error": (
            float(np.median(matched_dist))
            if matched_dist.size else None
        ),
        "max_error": (
            float(matched_dist.max())
            if matched_dist.size else None
        ),
        "unique_onsets_used": unique_matched,
        "duplicate_matches": duplicate_matches,
        "max_onset_reuse": max_reuse,
    }


def _explain_grouped_score(
    audio_onsets_groups: list[np.ndarray],
    chart_notes_groups: list[np.ndarray],
    delay: int,
    hard_score: bool,
) -> dict:
    """Combine score diagnostics without allowing groups to cross-match."""
    details = [
        _explain_score(audio, chart, delay, hard_score)
        for audio, chart in zip(audio_onsets_groups, chart_notes_groups)
    ]
    if not details:
        return _explain_score(np.array([0.0]), np.array([0.0]), delay, hard_score)

    total_matches = sum(item["notes_within_tolerance"] for item in details)
    average_error = sum(
        item["average_error"] * item["notes_within_tolerance"]
        for item in details
        if item["average_error"] is not None
    ) / total_matches if total_matches else None
    return {
        "score": sum(item["score"] for item in details),
        "base_score": sum(item["base_score"] for item in details),
        "streak_bonus": sum(item["streak_bonus"] for item in details),
        "notes_within_tolerance": total_matches,
        "total_notes": sum(item["total_notes"] for item in details),
        "perfect_matches": sum(item["perfect_matches"] for item in details),
        "good_matches": sum(item["good_matches"] for item in details),
        "acceptable_matches": sum(item["acceptable_matches"] for item in details),
        "misses": sum(item["misses"] for item in details),
        "average_error": average_error,
        "median_error": average_error,
        "max_error": max(
            (item["max_error"] for item in details if item["max_error"] is not None),
            default=None,
        ),
        "unique_onsets_used": sum(item["unique_onsets_used"] for item in details),
        "duplicate_matches": sum(item["duplicate_matches"] for item in details),
        "max_onset_reuse": max(item["max_onset_reuse"] for item in details),
    }


def _explain_candidate_score(candidate: dict, delay: int, hard_score: bool) -> dict:
    if candidate.get("grouped"):
        return _explain_grouped_score(
            candidate["audio_onsets_ms"],
            candidate["chart_notes_ms"],
            delay,
            hard_score,
        )
    return _explain_score(
        candidate["audio_onsets_ms"],
        candidate["chart_notes_ms"],
        delay,
        hard_score,
    )


def _compare_delays(
    best_delay: int,
    best_candidate,
    known_delay: int,
    best_known_delay: int,
    known_candidate,
    debug_lines: list[str],
) -> None:
    """Compare two candidate delays using the same scoring diagnostics."""

    score_diff = best_candidate["score"] - known_candidate["score"]
    mean_score = (best_candidate["score"] + known_candidate["score"]) / 2.0
    score_error = abs(score_diff) / mean_score if mean_score > 0 else 0.0

    debug_lines.append(
        f"                  audio's best delay={best_delay:+d} ms  "
        f"known delay={known_delay:+d} → "
        f"best around≈{best_known_delay:+d} ms  "
        f"Δscore={score_diff:+.2f} "
        f"({score_error:+.2%})"
    )

    fields = [
        ("score", "score"),
        ("base", "base_score"),
        ("streak", "streak_bonus"),
        ("matches", "notes_within_tolerance"),
        ("perfect", "perfect_matches"),
        ("good", "good_matches"),
        ("acceptable", "acceptable_matches"),
        ("avg err", "average_error"),
        ("median", "median_error"),
    ]

    for label, key in fields:
        av = best_candidate[key]
        bv = known_candidate[key]

        if av is None or bv is None:
            continue

        diff = av - bv

        if isinstance(av, float):
            debug_lines.append(
                f"                  {label:<11}: {av:7.2f} ({diff:+6.2f})"
            )
        else:
            debug_lines.append(
                f"                  {label:<11}: {av:7d} ({diff:+4d})"
            )

    delay_error = best_delay - best_known_delay

    debug_lines.append(
        f"            delay diff : {delay_error:+d} ms"
    )


def _extend_search(
    score_fn,
    chart_notes_ms: np.ndarray,
    audio_onsets_ms: np.ndarray,
    center: int,
    max_delay: int,
    best_d: int,
    best_s: float,
    debug: bool,
    debug_lines: list[str],
) -> tuple[int, float, bool, np.ndarray, np.ndarray]:
    """Extend the search beyond the initial range if the best delay lies at its boundary."""

    if abs(best_d - center) < max_delay:
        return (
            best_d,
            best_s,
            False,
            np.array([best_d], dtype=int),
            np.array([best_s], dtype=float),
        )

    if debug:
        debug_lines.append(
            f"        {dim(f'[debug] Best delay {-best_d:+d} ms exceeds max delay ±{max_delay} ms')}"
        )

    original_best = best_d
    searched_delays = [best_d]
    searched_scores = [best_s]
    direction = 1 if best_d >= 0 else -1
    limit = max_delay * EXTEND_LIMIT
    delay = (max_delay + COARSE_STEP + FINE_STEP) * direction
    no_improve = 0

    while no_improve < 10 and abs(delay) <= limit:
        score = score_fn(chart_notes_ms, audio_onsets_ms, delay, debug)
        searched_delays.append(delay)
        searched_scores.append(score)

        if score > best_s:
            best_s = score
            best_d = delay
            no_improve = 0
        else:
            no_improve += 1

        delay += direction * FINE_STEP

    if debug:
        if best_d != original_best:
            debug_lines.append(
                f"        {dim(f'[debug] Best delay {-original_best:+d} ms changed to {-best_d:+d} ms after extension search')}"
            )
        else:
            debug_lines.append(
                f"        {dim(f'[debug] No better delay found during extension search up to {-delay:+d} ms')}"
            )

    return (
        best_d,
        best_s,
        True,
        np.asarray(searched_delays, dtype=int),
        np.asarray(searched_scores, dtype=float),
    )


def _analyze_score_curve(
    delays: np.ndarray,
    scores: np.ndarray,
) -> dict[str, float]:
    """
    Compute diagnostics describing the quality of a score curve.

    These values are informational only and do not affect calibration.
    """

    best_idx = int(np.argmax(scores))
    best_score = float(scores[best_idx])

    # Second-best score (excluding the winner itself)
    if len(scores) > 1:
        second_best = float(np.max(np.delete(scores, best_idx)))
    else:
        second_best = best_score

    prominence = best_score - second_best

    # Number of delays within 98% of the best score
    if best_score > 0:
        peak_width = int(np.sum(scores >= best_score * 0.98))
    else:
        peak_width = 0

    # Approximate curvature around the maximum
    sharpness = 0.0
    if 0 < best_idx < len(scores) - 1:
        sharpness = (
            best_score
            - (scores[best_idx - 1] + scores[best_idx + 1]) / 2.0
        )

    return {
        "best_score": best_score,
        "second_best": second_best,
        "prominence": prominence,
        "peak_width": peak_width,
        "sharpness": sharpness,
    }


def estimate_delay_ms(
    ini_path: Path,
    audio_onsets_ms: np.ndarray,
    chart_notes_ms: np.ndarray,
    max_delay: int,
    anchor_ms: int | None = None,
    hard_score: bool = False,
    debug: bool = False,
    debug_lines: list[str] | None = None,
) -> tuple[int | None, float, float, int, bool, list[str]]:
    if debug_lines is None:
        debug_lines = []

    _weight_fn = _hard_weight if hard_score else _soft_weight
    bmask = _boundary_mask(chart_notes_ms) if BOUNDARY_BONUS else None

    # Vectorized coarse scan — centered on anchor if provided, else 0
    center = anchor_ms if anchor_ms is not None else 0
    coarse_delays = np.arange(center - max_delay, center + max_delay + 1, COARSE_STEP)

    coarse_scores = _score_delays(
        audio_onsets_ms,
        chart_notes_ms,
        coarse_delays,
        _weight_fn,
        bmask,
        debug,
    )

    best_idx = int(np.argmax(coarse_scores))
    best_d = int(coarse_delays[best_idx])
    best_s = float(coarse_scores[best_idx])

    # Vectorized fine scan around coarse best
    fine_delays = np.arange(
        best_d - COARSE_STEP,
        best_d + COARSE_STEP + FINE_STEP,
        FINE_STEP,
    )

    fine_scores = _score_delays(
        audio_onsets_ms,
        chart_notes_ms,
        fine_delays,
        _weight_fn,
        bmask,
        debug,
    )

    best_idx = int(np.argmax(fine_scores))
    best_d = int(fine_delays[best_idx])
    best_s = float(fine_scores[best_idx])

    # Extend search if best is at boundary
    score_fn = _hard_score_at if hard_score else _soft_score_at

    best_d, best_s, extended, extend_delays, extend_scores = _extend_search(
        score_fn,
        chart_notes_ms,
        audio_onsets_ms,
        center,
        max_delay,
        best_d,
        best_s,
        debug,
        debug_lines,
    )

    curve_stats = _analyze_score_curve(
        fine_delays,
        fine_scores,
    )
    if debug:
        debug_lines.append(
            "        "
            f"[curve] "
            f"prominence={curve_stats['prominence']:.2f}, "
            f"width={curve_stats['peak_width']}, "
            f"sharpness={curve_stats['sharpness']:.2f}"
        )

    min_needed = max(3.0, min(len(chart_notes_ms), len(audio_onsets_ms)) / 2.0) * (TOLERANCE_MS / 20.0)
    if best_s < min_needed:
        return None, best_s, min_needed, best_d, extended, debug_lines
    return best_d, best_s, min_needed, best_d, extended, debug_lines


# ---------------------------------------------------------------------------
# song.ini read / write
# ---------------------------------------------------------------------------

def _strip_ini_comments(text: str) -> str:
    lines = []
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("#!"):
            continue
        lines.append(line)
    return "".join(lines)


def read_ini(ini_path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(strict=False)
    try:
        text = ini_path.read_text(encoding="utf-8-sig", errors="replace")
        cfg.read_string(_strip_ini_comments(text))
    except configparser.Error as e:
        print(e)
    return cfg


def get_delay_value(cfg: configparser.ConfigParser) -> tuple[str | None, int | None]:
    for section in cfg.sections():
        if cfg.has_option(section, "delay"):
            try:
                return section, int(cfg[section]["delay"])
            except ValueError:
                return section, 0
    return None, None


def write_delay(ini_path: Path, new_delay: int, had_delay_key: bool) -> None:
    if not ini_path.exists():
        ini_path.write_text(f"[song]\ndelay = {new_delay}\n", encoding="utf-8")
        return
    text = ini_path.read_text(encoding="utf-8-sig", errors="replace")
    if had_delay_key:
        new_text = re.sub(
            r"(?i)(^\s*delay\s*=\s*)-?\d+",
            lambda m: m.group(1) + str(new_delay),
            text, flags=re.MULTILINE
        )
    else:
        cfg = configparser.ConfigParser(strict=False)
        cfg.read_string(_strip_ini_comments(text))
        section = cfg.sections()[0] if cfg.sections() else "song"
        new_text = re.sub(
            rf"(\[{re.escape(section)}\][^\n]*\n)",
            rf"\1delay = {new_delay}\n",
            text, count=1, flags=re.IGNORECASE
        )
        if new_text == text:
            new_text = text.rstrip() + f"\n[{section}]\ndelay = {new_delay}\n"
    ini_path.write_text(new_text, encoding="utf-8")


def write_delay_hash(hash_value: str, existing_hash: dict, delay: int) -> None:
    """Write the delay value to the json file in MODIF_PATH"""
    existing_hash[hash_value] = delay


def remove_delay_hash(hash_value: str, existing_hash: dict) -> None:
    """Remove the delay value from the json file in MODIF_PATH"""
    if hash_value in existing_hash:
        del existing_hash[hash_value]


# ---------------------------------------------------------------------------
# ETA computation
# ---------------------------------------------------------------------------

"""
Drop-in replacement for compute_eta() in CalibrateAudioOffset.py.

Root cause of the old function's flakiness: it modeled ETA as
    avg_time_per_item * remaining / workers
using a per-song self-reported "elapsed" sample blended into an EMA. That
model breaks whenever the assumptions don't hold — and they rarely do:
  - `workers` assumes perfect parallel scaling. False near the end of a run
    (fewer remaining items than workers) and false whenever a song is slow
    enough to stall other threads.
  - The EMA is seeded 100% from the very first sample (avg_time = sample),
    so one slow/unrepresentative first song poisons the ETA for a while.
  - Every sample gets equal EMA weight regardless of how much work it
    represents (current_audios varies a lot between songs), so a single
    heavy song can swing the average as much as ten light ones.
  - "skip" results (fast, trivial) and full analysis runs (slow) feed the
    same average, so the mix ratio of skips vs. real work silently shifts
    the ETA.

This version instead measures observed wall-clock throughput
(items completed / real elapsed time). That number is *inherently*
concurrency-aware — if 6 workers are actually busy, the completions-per-
second naturally reflects that; no `/ workers` fudge needed, and no
assumption that all workers stay saturated.
"""


class ETATracker:
    """Wall-clock-throughput ETA. Call update(done) each time progress is made."""

    def __init__(self, total: int, window_s: float = 2.0, alpha: float = 0.2):
        self.total = total
        self.window_s = window_s   # min real time between throughput samples
        self.alpha = alpha         # EMA weight for new windowed samples
        now = time.perf_counter()
        self.start = now
        self._win_t = now
        self._win_n = 0            # `done` count at start of current window
        self.rate: float | None = None   # smoothed items/sec

    def update(self, done: int) -> str:
        now = time.perf_counter()
        window_elapsed = now - self._win_t

        # Only fold in a new throughput sample once a real time window has
        # passed — this is what makes it immune to bursty completions (e.g.
        # several futures unblocking back-to-back) and to any one item's
        # duration dominating the estimate.
        if window_elapsed >= self.window_s and done > self._win_n:
            window_rate = (done - self._win_n) / window_elapsed
            self.rate = (
                window_rate if self.rate is None
                else self.rate * (1 - self.alpha) + window_rate * self.alpha
            )
            self._win_t, self._win_n = now, done

        # Bootstrap phase (before the first full window): fall back to the
        # cumulative average instead of guessing from a single sample.
        rate = self.rate
        if rate is None:
            elapsed = now - self.start
            rate = done / elapsed if elapsed > 0 else 0.0

        remaining = max(self.total - done, 0)
        eta_sec = int(remaining / rate) if rate > 0 else 0
        h, rem = divmod(eta_sec, 3600)
        m, s = divmod(rem, 60)
        per_item = f"{1 / rate:.1f}s/audio" if rate > 0 else "…"
        return f"  [{h}h{m}m{s:02}s left, {per_item}]"

# def compute_eta(start_time: float, avg_time: float, total_audios: int,
#                 counter_audios: int, current_audios: int, debug: bool,
#                 sample_elapsed: float | None = None, workers: int = 1) -> tuple[str, float]:
#     if current_audios > 0:
#         raw = sample_elapsed if sample_elapsed is not None else time.perf_counter() - start_time
#         sample = raw / current_audios
#         avg_time = sample if avg_time < 0 else avg_time * (1 - ETA_EMA_ALPHA) + sample * ETA_EMA_ALPHA
#     remaining = total_audios - counter_audios
#     eta_sec = int(avg_time * remaining  / workers) if avg_time > 0 and remaining > 0 else 0
#     eta_m, eta_s = divmod(eta_sec, 60)
#     eta_h, eta_m = divmod(eta_m, 60)
#     return dim(f"  [{eta_h}h{eta_m}m{eta_s:02}s left, {avg_time:.1f}s/audio]"), avg_time


# ---------------------------------------------------------------------------
# Per-song processing (runs in worker thread)
# ---------------------------------------------------------------------------

def _make_skip_result(result: dict, reason: str, t0: float, debug_lines: list | None = None) -> dict:
    result["skip_reason"] = reason
    result["elapsed"] = time.perf_counter() - t0
    if debug_lines is not None:
        result["debug_lines"] = debug_lines
    return result

def _load_audio(p: Path) -> np.ndarray | None:
    """Load and mono-mix an audio file, returning a float32 array or None on failure."""
    try:
        if AUDIO_CLIP_S is not None:
            sr_sf = sf.info(str(p)).samplerate
            stop = int(AUDIO_CLIP_S * sr_sf)
        else:
            sr_sf = stop = None
        
        # Read audio via SoundFile
        y_raw, sr_sf = sf.read(str(p), always_2d=False, dtype="float32", stop=stop)
        
        # Mix down to mono immediately in float32
        if y_raw.ndim > 1:
            y_raw = y_raw.mean(axis=1, dtype=np.float32)
            
        if sr_sf != SR:
            y_resampled = librosa.resample(y_raw, orig_sr=sr_sf, target_sr=SR)
            del y_raw
            return np.asarray(y_resampled, dtype=np.float32)
            
        return np.asarray(y_raw, dtype=np.float32)
        
    except Exception:
        try:
            y, _ = librosa.load(str(p), sr=SR, mono=True, duration=AUDIO_CLIP_S)
            return np.asarray(y, dtype=np.float32)
        except Exception:
            return None
        
def _get_onset_envelopes(
    audio_path: Path,
    y_norm: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:

    raw = librosa.onset.onset_strength(
        y=y_norm,
        sr=SR,
        hop_length=HOP_LENGTH,
    )

    _, filtered = apply_lowpass(y_norm)

    lowpass = librosa.onset.onset_strength(
        y=filtered,
        sr=SR,
        hop_length=HOP_LENGTH,
    )

    result = (raw, lowpass)

    del filtered

    return result

def _prepare_audio(
    p: Path,
    chart_notes: np.ndarray,
    debug_lines: list[str]
) -> tuple[np.ndarray, np.ndarray, float] | None:
    """Load, validate and normalize an audio file."""

    y_full = _load_audio(p)

    if y_full is None:
        debug_lines.append(
            f"        {dim(f'[debug] [{p.name}] file corrupt or empty')}"
        )
        return None

    if getattr(y_full, "size", 0) == 0:
        debug_lines.append(
            f"        {dim(f'[debug] [{p.name}] file corrupt or empty (zero length)')}"
        )
        return None

    rms = float(np.sqrt(np.dot(y_full, y_full) / max(len(y_full), 1)))
    if not np.isfinite(rms) or rms <= 0.001:
        debug_lines.append(
            f"        {dim(f'[debug] [{p.name}] file corrupt or silent (RMS too low)')}"
        )
        return None

    absvals = np.abs(y_full)
    if not np.any(np.isfinite(absvals)):
        debug_lines.append(
            f"        {dim(f'[debug] [{p.name}] contains no finite samples')}"
        )
        return None

    peak = float(np.nanmax(absvals))
    y_norm = y_full / (peak + 1e-9)

    clip_ms = len(y_norm) / SR * 1000.0
    chart_window = chart_notes[chart_notes <= clip_ms]
    if len(chart_window) == 0:
        chart_window = chart_notes

    return y_norm, chart_window, clip_ms


def _refine_onset_frames(frames: np.ndarray, onset_env: np.ndarray) -> np.ndarray:
    """Parabolic interpolation to get sub-frame onset positions.
    Fits a parabola through each detected frame and its two neighbours to find
    the true peak between frames, giving sub-hop-length timing precision cheaply."""
    refined = np.asarray(frames, dtype=np.float32).copy()
    for i, f in enumerate(frames):
        if 0 < f < len(onset_env) - 1:
            a, b, c = onset_env[f - 1], onset_env[f], onset_env[f + 1]
            denom = 2.0 * (2.0 * b - a - c)
            if denom > 0:
                refined[i] = f + (a - c) / denom
    return refined


def _process_filter(
    ini_path: Path,
    onset_env: np.ndarray,
    chart_window: np.ndarray,
    cur_del: int,
    max_delay: int,
    clip_ms: float,
    p: Path,
    label: str,
    hard_score: bool,
    flip_sign: bool,
    debug: bool,
    debug_lines: list[str],
) -> tuple[dict | None, list[str]]:
    """Process one onset envelope and return the resulting calibration candidate."""

    delta_val = 0.05
    frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=SR, units="frames",
        hop_length=HOP_LENGTH,
        pre_max=3, post_max=3, pre_avg=10, post_avg=10,
        delta=0.05, wait=5, backtrack=True,
    )
    onsets = librosa.frames_to_time(_refine_onset_frames(frames, onset_env), sr=SR, hop_length=HOP_LENGTH) * 1000.0
    nb_onsets = len(onsets)

    # Retry with more sensitive params if too few onsets
    while nb_onsets < 1.5 * clip_ms / 1000 and delta_val > 1e-3:
        delta_val -= 0.005
        # dprint(debug, f"more sensitivity on onset detection needed ({ini_path.parent})")
        frames2 = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=SR, units="frames",
            hop_length=HOP_LENGTH,
            pre_max=2, post_max=2, pre_avg=5, post_avg=5,
            delta=delta_val, wait=3, backtrack=True,
        )
        onsets2 = librosa.frames_to_time(_refine_onset_frames(frames2, onset_env), sr=SR, hop_length=HOP_LENGTH) * 1000.0
        if len(onsets2) > nb_onsets:
            onsets = onsets2
        nb_onsets = len(onsets)

    if nb_onsets == 0:
        debug_lines.append(
            f"        {dim(f'[debug] [{p.name} ({label})] 0 onsets detected')}"
        )
        return None, debug_lines

    if nb_onsets < len(chart_window) / 3:
        debug_lines.append(
            f"        {dim(f'[debug] [{p.name} ({label})] too few onsets ({nb_onsets}) vs chart notes ({len(chart_window)}) ({nb_onsets / len(chart_window):.1f})')}"
        )
        return None, debug_lines

    res, score, needed, best_d, ext, debug_lines = estimate_delay_ms(
        ini_path,
        onsets,
        chart_window,
        max_delay,
        anchor_ms=cur_del,
        hard_score=hard_score,
        debug=debug,
        debug_lines=debug_lines,
    )

    hard_count, _, std_dev_ratio = compute_match_details(
        onsets,
        chart_window,
        best_d,
    )

    computed_delay = best_d if not flip_sign else -best_d

    quadruplet = format_match_quadruplet(score, needed, nb_onsets, len(chart_window), dimmed=True)
    score_str = format_score_percentage(score, needed, nb_onsets, len(chart_window), dimmed=True)

    delta = computed_delay - cur_del
    delta_str = f"  ({delta:+d} ms)" if abs(delta) > 1e-2 else ""
    ext_str = " extended" if ext else ""

    debug_lines.append(dim(
        f"        [debug] [{p.name} ({label})] "
        f"{cur_del:+d} ms -> {computed_delay:+d} ms{delta_str}"
        f"  [{quadruplet} matched, {score_str}, "
        f"{std_dev_ratio:.2f} std.dev ratio]{ext_str}"
    ))

    candidate = {
        "res": res,
        "delay": computed_delay,
        "score": score,
        "comparison": score / nb_onsets,
        "hard_count": hard_count,
        "needed": needed,
        "ext": ext,
        "std_dev_ratio": std_dev_ratio,
        "audio_onsets_ms": onsets,
        "nb_onsets": nb_onsets,
        "chart_notes_ms": chart_window,
        "nb_chart_notes": len(chart_window),
        "score_pct": score / len(chart_window) if len(chart_window) else 0,
        "filter_label": label,
        "audio_name": p.name,
    }

    return candidate, debug_lines


def _score_grouped_delays(
    onset_groups: list[np.ndarray],
    chart_groups: list[np.ndarray],
    delays: np.ndarray,
    weight_fn,
    debug: bool,
) -> np.ndarray:
    """Score one shared delay while keeping every stem paired with its chart part."""
    scores = np.zeros(len(delays), dtype=float)
    for audio_onsets_ms, chart_notes_ms in zip(onset_groups, chart_groups):
        if len(audio_onsets_ms) and len(chart_notes_ms):
            scores += _score_delays(
                audio_onsets_ms,
                chart_notes_ms,
                delays,
                weight_fn,
                _boundary_mask(chart_notes_ms) if BOUNDARY_BONUS else None,
                debug,
            )
    return scores


def _estimate_grouped_delay_ms(
    onset_groups: list[np.ndarray],
    chart_groups: list[np.ndarray],
    max_delay: int,
    anchor_ms: int,
    hard_score: bool,
    debug: bool,
    debug_lines: list[str],
) -> tuple[int | None, float, float, int, bool]:
    """Estimate one delay from paired stem/chart groups without cross-matching them."""
    weight_fn = _hard_weight if hard_score else _soft_weight
    center = anchor_ms
    coarse_delays = np.arange(center - max_delay, center + max_delay + 1, COARSE_STEP)
    coarse_scores = _score_grouped_delays(
        onset_groups, chart_groups, coarse_delays, weight_fn, debug
    )
    best_idx = int(np.argmax(coarse_scores))
    best_d = int(coarse_delays[best_idx])
    best_s = float(coarse_scores[best_idx])

    fine_delays = np.arange(best_d - COARSE_STEP, best_d + COARSE_STEP + FINE_STEP, FINE_STEP)
    fine_scores = _score_grouped_delays(
        onset_groups, chart_groups, fine_delays, weight_fn, debug
    )
    best_idx = int(np.argmax(fine_scores))
    best_d = int(fine_delays[best_idx])
    best_s = float(fine_scores[best_idx])

    def score_at(_unused_chart, _unused_audio, delay: int, _debug: bool) -> float:
        return float(_score_grouped_delays(
            onset_groups,
            chart_groups,
            np.array([delay]),
            weight_fn,
            debug,
        )[0])

    best_d, best_s, extended, _, _ = _extend_search(
        score_at,
        np.array([]),
        np.array([]),
        center,
        max_delay,
        best_d,
        best_s,
        debug,
        debug_lines,
    )

    min_needed = sum(
        max(3.0, min(len(chart), len(audio)) / 2.0)
        for audio, chart in zip(onset_groups, chart_groups)
    ) * (TOLERANCE_MS / 20.0)
    return (
        (best_d if best_s >= min_needed else None),
        best_s,
        min_needed,
        best_d,
        extended,
    )


def _process_grouped_filter(
    ini_path: Path,
    onset_env_groups: list[np.ndarray],
    chart_groups: list[np.ndarray],
    cur_del: int,
    max_delay: int,
    clip_ms: float,
    label: str,
    hard_score: bool,
    flip_sign: bool,
    debug: bool,
    debug_lines: list[str],
) -> tuple[dict | None, list[str]]:
    """Detect onsets per stem and score all stem/chart pairs with one shared delay."""
    onset_groups: list[np.ndarray] = []
    valid_chart_groups: list[np.ndarray] = []
    for onset_env, chart_notes in zip(onset_env_groups, chart_groups):
        delta_val = 0.05
        frames = librosa.onset.onset_detect(
            onset_envelope=onset_env, sr=SR, units="frames",
            hop_length=HOP_LENGTH, pre_max=3, post_max=3,
            pre_avg=10, post_avg=10, delta=delta_val, wait=5, backtrack=True,
        )
        onsets = librosa.frames_to_time(
            _refine_onset_frames(frames, onset_env), sr=SR, hop_length=HOP_LENGTH
        ) * 1000.0
        while len(onsets) < 1.5 * clip_ms / 1000 and delta_val > 1e-3:
            delta_val -= 0.005
            frames = librosa.onset.onset_detect(
                onset_envelope=onset_env, sr=SR, units="frames",
                hop_length=HOP_LENGTH, pre_max=2, post_max=2,
                pre_avg=5, post_avg=5, delta=delta_val, wait=3, backtrack=True,
            )
            candidate_onsets = librosa.frames_to_time(
                _refine_onset_frames(frames, onset_env), sr=SR, hop_length=HOP_LENGTH
            ) * 1000.0
            if len(candidate_onsets) > len(onsets):
                onsets = candidate_onsets
        if len(onsets):
            onset_groups.append(onsets)
            valid_chart_groups.append(chart_notes)
        elif debug:
            debug_lines.append(
                dim(f"        [debug] [all stems ({label})] no onsets detected "
                    f"for paired chart group ({len(chart_notes)} notes)")
            )

    if not onset_groups:
        debug_lines.append(dim(f"        [debug] [all stems ({label})] no usable paired onsets"))
        return None, debug_lines

    res, score, needed, best_d, ext = _estimate_grouped_delay_ms(
        onset_groups, valid_chart_groups, max_delay, cur_del,
        hard_score, debug, debug_lines,
    )
    computed_delay = best_d if not flip_sign else -best_d
    total_notes = sum(len(chart) for chart in valid_chart_groups)
    hard_count = sum(
        compute_match_details(audio, chart, best_d)[0]
        for audio, chart in zip(onset_groups, valid_chart_groups)
    )
    std_ratios = [compute_match_details(audio, chart, best_d)[2]
                  for audio, chart in zip(onset_groups, valid_chart_groups)]
    std_dev_ratio = float(np.mean(std_ratios)) if std_ratios else 0.0
    debug_lines.append(dim(
        f"        [debug] [all stems ({label})] {cur_del:+d} ms -> "
        f"{computed_delay:+d} ms  [{score:.2f}/{needed:.2f}/"
        f"{sum(len(audio) for audio in onset_groups)}/{total_notes} matched]"
        f"  [{std_dev_ratio:.2f} std.dev ratio]"
        f"{' extended' if ext else ''}"
    ))
    candidate = {
        "res": res,
        "delay": computed_delay,
        "score": score,
        "comparison": score / max(sum(len(audio) for audio in onset_groups), 1),
        "hard_count": hard_count,
        "needed": needed,
        "ext": ext,
        "std_dev_ratio": std_dev_ratio,
        "audio_onsets_ms": onset_groups,
        "nb_onsets": sum(len(audio) for audio in onset_groups),
        "chart_notes_ms": valid_chart_groups,
        "nb_chart_notes": total_notes,
        "score_pct": score / total_notes if total_notes else 0,
        "filter_label": label,
        "audio_name": "all stems",
        "grouped": True,
    }
    return candidate, debug_lines


def _process_song(
    ini_path: Path,
    max_delay: int,
    debug: bool,
    flip_sign: bool,
    skip_inconclusive: bool,
    hard_score: bool = False,
    cancel_event: threading.Event | None = None,
    existing_hash: dict = {}
) -> dict:
    result: dict = {
        "ini_path":       ini_path,
        "status":         "skip",
        "skip_reason":    "",
        "best_overall":   None,
        "clustered":      False,
        "inconclusive":   False,
        "cur_del":        0,
        "had_delay_key":  False,
        "chart_notes":    np.array([]),
        "debug_lines":    [],
        "elapsed":        0.0,
        "current_audios": 0,
        "nb_onsets":      0
    }

    t0 = time.perf_counter()
    folder = ini_path.parent

    mogg_candidates, hash_value_mogg = _mogg_candidates(folder, debug)
    candidates = mogg_candidates if mogg_candidates else ExtractCONSNG.find_audio_candidates(folder)
    result["current_audios"] = len(candidates)
    if not candidates:
        print('not candidates is true')
        return _make_skip_result(result, "no audio files found", t0)
    if any(p is None for p in candidates):
        print('any(p is None for p in candidates) is true')
        return _make_skip_result(result, "one or more audio files could not be loaded", t0)

    chart_parts, chart_reason, hash_value = get_chart_notes(folder, debug)
    if not chart_parts:
        return _make_skip_result(result, chart_reason, t0)
    all_chart_notes = np.unique(np.concatenate(list(chart_parts.values())))
    result["chart_notes"] = all_chart_notes
    result["hash"] = hash_value

    cfg = read_ini(ini_path)
    section, cur_del = get_delay_value(cfg)
    had_delay_key = section is not None
    if not had_delay_key or not cur_del:
        cur_del = existing_hash.get(hash_value)
    if cur_del is None:
        cur_del = 0
    result["cur_del"] = cur_del
    result["had_delay_key"] = had_delay_key

    best_overall = None
    best_inconclusive = None
    inconclusive_delays = []
    debug_lines: list[str] = []
    valid_audio_found = False
    result["candidates"] = []

    raw_envelopes: list[np.ndarray] = []
    lowpass_envelopes: list[np.ndarray] = []
    chart_groups: list[np.ndarray] = []
    max_clip_ms = 0.0
    backing_only = all(
        p.stem.lower() in {"backing", "song"}
        for p in candidates
    )

    for p in candidates:
        if cancel_event is not None and cancel_event.is_set():
            result["status"] = "cancelled"
            return _make_skip_result(result, "", t0)

        # Load and normalise audio
        chart_notes = midi_parts_for_stem(p.stem.lower(), chart_parts)
        if backing_only:
            chart_notes = all_chart_notes
        if len(chart_notes) == 0:
            if debug:
                debug_lines.append(
                    f"        [debug] [{p.name}] no MIDI part mapping; "
                    f"available parts: {', '.join(chart_parts)}"
                )
            continue
        if debug:
            debug_lines.append(
                f"        [debug] [{p.name}] paired with {len(chart_notes)} MIDI notes"
            )
        prepared = _prepare_audio(
            p,
            chart_notes,
            debug_lines,
        )
        if prepared is None:
            continue
        y_norm, _, clip_ms = prepared
        valid_audio_found = True
        max_clip_ms = max(max_clip_ms, clip_ms)
        chart_groups.append(prepared[1])

        raw_env, lowpass_env = _get_onset_envelopes(p, y_norm)

        raw_envelopes.append(raw_env)
        lowpass_envelopes.append(lowpass_env)

    if hash_value_mogg:
        mogg_extract_path = CACHE_PATH / hash_value_mogg
        if mogg_extract_path.is_dir():
            try:
                [x.unlink() for x in mogg_extract_path.glob('*') if x.is_file()]
                dprint(debug, f'Emptied directory {mogg_extract_path}.')
            except:
                dprint(debug, f'Could not empty directory {mogg_extract_path}.')
            shutil.rmtree(mogg_extract_path)


    if not valid_audio_found:
        return _make_skip_result(result, "no audio file (or all stems are silent), need logic to check mogg", t0, debug_lines)

    aggregate_inputs = [
        (raw_envelopes, "no filter"),
        (lowpass_envelopes, "lowpass"),
    ]
    for onset_env_groups, label in aggregate_inputs:
        candidate, debug_lines = _process_grouped_filter(
            ini_path, onset_env_groups, chart_groups, cur_del, max_delay,
            max_clip_ms, label, hard_score, flip_sign,
            debug, debug_lines,
        )
        result["candidates"].append(candidate)

        if candidate is None:
            continue

        res = candidate.pop("res")
        if res is not None:
            if best_overall is None or candidate["score"] > best_overall["score"]:
                best_overall = {**candidate, "inconclusive": False}
        else:
            inconclusive_delays.append(candidate["delay"])
            if candidate["score"] >= candidate["needed"] * 0.5:
                if best_inconclusive is None or candidate["score"] > best_inconclusive["score"]:
                    best_inconclusive = {**candidate, "inconclusive": True}

    tight_values_cluster = bool(inconclusive_delays and (max(inconclusive_delays) - min(inconclusive_delays)) <= 30)

    if best_overall is None:
        if best_inconclusive is not None and tight_values_cluster:
            # All passes agree within 30ms — write it but keep inconclusive flag
            best_overall = best_inconclusive  # inconclusive=True already
        elif best_inconclusive is not None and not skip_inconclusive:
            best_overall = best_inconclusive
        else:
            return _make_skip_result(result, "cross-correlation inconclusive (tried all files and filters)", t0, debug_lines)

    if best_overall["score_pct"] < TOTAL_PCT_MIN:
        if tight_values_cluster and best_overall.get('inconclusive'):
            best_overall['inconclusive'] = True
        else:
            return _make_skip_result(
                result,
                f"best overall score covers only {best_overall['score_pct']:.1%} "
                f"of the selected chart notes",
                t0,
                debug_lines,
            )

    result["status"] = "ok"
    result["best_overall"] = best_overall
    result["clustered"] = best_overall['std_dev_ratio'] < 0.8
    result["inconclusive"] = bool(best_overall.get('inconclusive'))
    result["debug_lines"] = debug_lines
    result["elapsed"] = time.perf_counter() - t0
    result["nb_onsets"] = best_overall['nb_onsets']

    del raw_envelopes, lowpass_envelopes
    gc.collect()

    return result


# ---------------------------------------------------------------------------
# Helpers for the result printing loop
# ---------------------------------------------------------------------------

def _format_song_type_counts(nb_inis: int, nb_cons: int, nb_sngs: int) -> str:
    parts = []
    if nb_inis: parts.append(f"{nb_inis} {'are' if nb_inis > 1 else 'is'} INI")
    if nb_cons: parts.append(f"{nb_cons} {'are' if nb_cons > 1 else 'is'} CON")
    if nb_sngs: parts.append(f"{nb_sngs} {'are' if nb_sngs > 1 else 'is'} SNG")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Library pipeline
# ---------------------------------------------------------------------------

def process_library(
    root: Path,
    dry_run: bool,
    debug: bool,
    max_delay: int,
    flip_sign: bool,
    skip_inconclusive: bool = False,
    skip_existing: bool = False,
    skip_existing_inconclusive: bool = False,
    skip_existing_clustered: bool = False,
    keep_skipped: bool = False,
    workers: int = WORKER_THREADS,
    delete_cons: bool = False,
    delete_sngs: bool = False,
    overwrite: bool = False,
    skip_extracted: bool = False,
    dump_raw: bool = False,
    hard_score: bool = False,
    tolerance: int | None = None,
    hop_length: int | None = None,
    boundary_bonus: bool = False,
    streak_bonus: bool = False,
    extract: bool = False,
    extract_only: bool = False,
) -> None:
    print()
    if delete_cons and not dry_run:
        print(red(bold("  WARNING: --delete-cons is set. CON files will be permanently deleted after extraction.")))
        print("  Press Ctrl+C to abort...\n")
    tag = dim("[dry-run] ") if dry_run else ""

    existing = load_list()
    existing_hash = load_hash_list()

    print("Scanning songs...")
    # First scan — full, for display only
    all_songs, nb_inis, nb_cons, nb_sngs, _, cons, sngs = get_song_list(
        root, skip_extracted=skip_extracted
    )
    if not all_songs:
        print("No songs found.")
        return

    print(bold(f"Scanning {len(all_songs)} songs"))
    print(bold(_format_song_type_counts(nb_inis, nb_cons, nb_sngs)))

    # Filter in-place instead of re-scanning
    if skip_existing:
        songs = [p for p in all_songs if _entry_needs_processing(existing, song_key(p))]
        # TODO more efficient way to do this? At the moment it needs to do all three to consider all posible endings
        if skip_existing_inconclusive:
            songs = [p for p in songs if song_key(p, skip_existing_inconclusive=True, skip_existing_clustered=False) not in existing]
        if skip_existing_clustered:
            songs = [p for p in songs if song_key(p, skip_existing_inconclusive=False, skip_existing_clustered=True) not in existing]
        if skip_existing_inconclusive and skip_existing_clustered:
            songs = [p for p in songs if song_key(p, skip_existing_inconclusive=True, skip_existing_clustered=True) not in existing]
        # Recompute per-type counts from the filtered list
        inis = [p for p in songs if p.suffix == ".ini"]
        cons  = [p for p in songs if p.suffix not in EXCLUDED_CON_SUFFIXES and ExtractCONSNG.is_con_file(p)]
        sngs  = [p for p in songs if p.suffix == ".sng"]
        print(bold(f"{tag}After filtering existing entries, {len(songs)} songs remain to process"))
    else:
        songs = all_songs

    cancel_event = threading.Event()
    extracted_map = {}
    if extract:
        extracted_map = ExtractCONSNG.pre_extract_all(cons, sngs, overwrite, debug, workers, cancel_event, delete_cons, delete_sngs, dry_run, dump_raw)
    if extract_only:
        return

    # Replace CON/SNG paths with their extracted ini paths
    songs = [
        extracted_map[p] if p in extracted_map else p for p in songs
        if not (ExtractCONSNG.is_con_file(p) or ExtractCONSNG.is_sng_file(p)) or p in extracted_map
    ]

    print("\nScanning number of audio files...")
    total_audios = count_audio_files(debug=debug, always_print=True, songs=songs)
    print("\n")

    updated = skipped = extended_count = inconclusive_count = clustered_count = 0
    eta = ETATracker(total_audios)
    counter_audios = 0
    name_width = MAX_SONG_NAME
    start_time = time.perf_counter()

    global TOTAL_PCT_MIN, TOLERANCE_MS, HOP_LENGTH, BOUNDARY_BONUS, STREAK_BONUS
    if tolerance is not None:
        TOLERANCE_MS = tolerance
    # Scale thresholds proportionally with TOLERANCE_MS so that tighter windows
    # don't cause excessive skips — scores are naturally lower at tight tolerance.
    tolerance_scale = TOLERANCE_MS / 30.0
    TOTAL_PCT_MIN = (0.2 if hard_score else 0.1) * tolerance_scale
    if hop_length is not None:
        HOP_LENGTH = hop_length
    BOUNDARY_BONUS = boundary_bonus
    STREAK_BONUS = streak_bonus

    executor = ThreadPoolExecutor(max_workers=workers)
    count_results = {"no filter": 0, "lowpass": 0}
    try:
        futures = []
        # useful to build the dictionary of known_delays more easily, do not delete.
        # for song in songs:
        #     print(f'"{song_key(song)}": None,')
        for ini_path in songs:
            if ini_path:
                fut = executor.submit(
                    _process_song, ini_path, max_delay, debug, flip_sign,
                    skip_inconclusive, hard_score, cancel_event, existing_hash
                )
                futures.append((ini_path, fut))

        print('Starting calibration...                         [score/needed/nb_onsets/nb_notes matched, %(needed), %(onsets), %(notes)]')
        for i, (ini_path, fut) in enumerate(futures):
            try:
                r = fut.result()
            except Exception as exc:
                print(red(f"[error] {ini_path.parent.name}: {exc}"))
                skipped += 1
                continue

            folder = ini_path.parent
            display = folder.name[:name_width - 3] + "..." if len(folder.name) > name_width else folder.name
            padded = f"{display:<{name_width}}"

            hash_value = r.get('hash')
            cur_del = r["cur_del"]
            had_delay_key = r["had_delay_key"]
            chart_notes = r["chart_notes"]
            debug_lines = r["debug_lines"]
            current_audios = r["current_audios"]
            counter_audios += current_audios
            nb_onsets = r["nb_onsets"]

            if r["status"] in ("skip", "cancelled"):
                if r["status"] == "cancelled":
                    continue
                eta_str = dim(eta.update(counter_audios))
                if not keep_skipped and not dry_run:
                    remove_from_list(existing, song_key(ini_path), debug)
                skipped += 1
                if debug_lines:
                    for line in debug_lines:
                        print(line)
                continue

            best = r["best_overall"]
            clustered = r["clustered"]
            inconclusive = r["inconclusive"]

            quadruplet = format_match_quadruplet(best['score'], best['needed'], nb_onsets, len(chart_notes))
            score_str = format_score_percentage(best['score'], best['needed'], nb_onsets, len(chart_notes))
            trailing = "\n" + "      " + name_width * " " + "  " if debug else "  "
            match_s = trailing + f"[{quadruplet} matched, {score_str}, {best['std_dev_ratio']:.2f} std.dev ratio]"

            delta = best['delay'] - cur_del
            delta_s = f"  ({delta:+d} ms)" if abs(delta) > 10e-3 else ""
            chosen_s = f"  (winner: {r.get("best_overall").get("audio_name")} ({r.get("best_overall").get("filter_label")}))" if debug else ""

            tags = "".join([
                f"  {yellow('[clustered]')}" if clustered else "",
                yellow("  [extended]") if best['ext'] else "",
                cyan("  [inconclusive]") if inconclusive else "",
                dim("  [new key]") if not had_delay_key else "",
                dim("  [dry-run]") if dry_run else "",
            ])

            if inconclusive:
                ok_label = cyan("ok  ")
                inconclusive_count += 1
            elif best['ext'] or clustered:
                ok_label = yellow("ok  ")
            else:
                ok_label = green("ok  ")

            eta_str = dim(eta.update(counter_audios))
            best_delay = best["delay"]
            print(
                f"{ok_label}  {padded}  {dim(f'{cur_del:+d} ms -> ')}{bold(f'{best_delay:+d} ms')}"
                f"{delta_s}{chosen_s}{match_s}{tags}{eta_str} (done {i+1}/{len(songs)})"
            )

            known_delays = {
                "03.a guitar hero iii / aerosmith - same old song & dance": -26+13,
                "03.a guitar hero iii / afi - miss murder": -17+11,
                "03.a guitar hero iii / alice cooper - school's out": -25+15,
                "03.a guitar hero iii / beastie boys - sabotage": None,
                "03.a guitar hero iii / black sabbath - paranoid": None,
                "03.a guitar hero iii / bloc party - helicopter": None,
                "03.a guitar hero iii / blue öyster cult - cities on flame with rock & roll": None,
                "03.a guitar hero iii / cream - sunshine of your love": None,
                "03.a guitar hero iii / disturbed - stricken": None,
                "03.a guitar hero iii / eric johnson - cliffs of dover": None,
                "03.a guitar hero iii / foghat - slow ride": None,
                "03.a guitar hero iii / guns n' roses - welcome to the jungle": None,
                "03.a guitar hero iii / heart - barracuda": None,
                "03.a guitar hero iii / iron maiden - the number of the beast": None,
                "03.a guitar hero iii / kiss - rock & roll all nite": None,
                "03.a guitar hero iii / living colour - cult of personality": None,
                "03.a guitar hero iii / matchbook romance - monsters": None,
                "03.a guitar hero iii / metallica - one": None,
                "03.a guitar hero iii / metallica - one (co-op)": None,
                "03.a guitar hero iii / mountain - mississippi queen": None,
                "03.a guitar hero iii / muse - knights of cydonia": None,
                "03.a guitar hero iii / pat benatar - hit me with your best shot": None,
                "03.a guitar hero iii / pearl jam - even flow": None,
                "03.a guitar hero iii / poison - talk dirty to me": None,
                "03.a guitar hero iii / priestess - lay down": None,
                "03.a guitar hero iii / queens of the stone age - 3's & 7's": None,
                "03.a guitar hero iii / rage against the machine - bulls on parade": None,
                "03.a guitar hero iii / red hot chili peppers - suck my kiss": None,
                "03.a guitar hero iii / santana - black magic woman": None,
                "03.a guitar hero iii / scorpions - rock you like a hurricane": None,
                "03.a guitar hero iii / slash - guitar battle vs. slash": None,
                "03.a guitar hero iii / slayer - raining blood": None,
                "03.a guitar hero iii / slipknot - before i forget": None,
                "03.a guitar hero iii / social distortion - story of my life": None,
                "03.a guitar hero iii / sonic youth - kool thing": None,
                "03.a guitar hero iii / stevie ray vaughan - pride & joy": -27+37,
                "03.a guitar hero iii / tenacious d - the metal": None,
                "03.a guitar hero iii / the dead kennedys - holiday in cambodia": None,
                "03.a guitar hero iii / the killers - when you were young": None,
                "03.a guitar hero iii / the rolling stones - paint it black": None,
                "03.a guitar hero iii / the sex pistols - anarchy in the uk": None,
                "03.a guitar hero iii / the smashing pumpkins - cherub rock": None,
                "03.a guitar hero iii / the strokes - reptilia": None,
                "03.a guitar hero iii / the who - the seeker": None,
                "03.a guitar hero iii / tom morello - guitar battle vs. tom morello": None,
                "03.a guitar hero iii / weezer - my name is jonas": None,
                "03.a guitar hero iii / white zombie - black sunshine": None,
                "03.a guitar hero iii / zz top - la grange": None
            }
            known_delay = known_delays.get(song_key(ini_path))

            if known_delay is not None:
                compare_results = []

                for candidate in r["candidates"]:
                    explain_best = _explain_candidate_score(
                        candidate, candidate["delay"], hard_score
                    )

                    known_candidate = _explain_candidate_score(
                        candidate, known_delay, hard_score
                    )

                    best_known_delay = known_delay

                    for d in range(known_delay - 2, known_delay + 3):
                        c = _explain_candidate_score(candidate, d, hard_score)

                        if c["score"] > known_candidate["score"]:
                            known_candidate = c
                            best_known_delay = d

                    score_diff = explain_best["score"] - known_candidate["score"]
                    relative_error = abs(score_diff) / (
                        (explain_best["score"] + known_candidate["score"]) / 2
                    )

                    compare_results.append((
                        relative_error,
                        candidate,
                        explain_best,
                        best_known_delay,
                        known_candidate,
                    ))

                compare_results.sort(key=lambda x: x[0])

                for _, candidate, explain_best, best_known_delay, known_candidate in compare_results:
                    comparison_name = f"{candidate['audio_name']} ({candidate['filter_label']})"
                    winner_name = f'{r.get("best_overall").get("audio_name")} ({r.get("best_overall").get("filter_label")})'
                    compare_line = f"        [compare] {comparison_name}"
                    print_green = False
                    if comparison_name == winner_name:
                        print_green = True
                    compare_line = green(compare_line + " (winner)") if print_green else compare_line
                    debug_lines.append(compare_line)

                    _compare_delays(
                        candidate["delay"],
                        explain_best,
                        known_delay,
                        best_known_delay,
                        known_candidate,
                        debug_lines,
                    )

            if debug or inconclusive:
                for line in debug_lines:
                    print(line)

            save_key = song_key(ini_path, clustered=clustered, inconclusive=inconclusive)

            # Track whether anything actually changed
            if save_key in existing:
                existing_val = existing[save_key]
                existing_delay = existing_val[0] if isinstance(existing_val, list) else existing_val
                if best['delay'] != existing_delay:
                    updated += 1
            else:
                updated += 1

            if best['ext']:
                extended_count += 1
            if clustered:
                clustered_count += 1
            count_results[best['filter_label']] += 1

            if not dry_run:
                total_pct = round(10000 * (best['score'] / len(chart_notes) if len(chart_notes) > 0 else 0)) / 10000
                if USE_INI or hash_value is None:
                    write_delay(ini_path, best['delay'], had_delay_key)
                else:
                    try:
                        write_delay_hash(hash_value, existing_hash, delay=best['delay'])
                        try:
                            write_delay(ini_path, 0, had_delay_key)
                        except Exception as e:
                            print(f"{e}: couldn't zero ini for {ini_path}, rolling back hash entry.")
                            try:
                                remove_delay_hash(hash_value, existing_hash)
                            except Exception as e2:
                                print(f"{e2}: rollback failed too — {ini_path} may have the delay in both places.")
                            write_delay(ini_path, best['delay'], had_delay_key)
                    except Exception as e:
                        print(f"{e}: wasn't able to write delay to hash file for {ini_path}.")
                        write_delay(ini_path, best['delay'], had_delay_key)
                remove_from_list(existing, song_key(ini_path), debug, always_print=False)
                add_to_list(existing, save_key, best['delay'], total_pct)
                if updated > 0 and i % 50 == 0:
                    save_list(existing)
                    if not USE_INI:
                        json.dump(existing_hash, open(MODIF_PATH, "w"), indent=2)

    except KeyboardInterrupt:
        print("\n[!] Shutdown signal received.")
        cancel_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
    else:
        executor.shutdown(wait=True)

    print("Saving results...")
    save_list(existing)
    json.dump(existing_hash, open(MODIF_PATH, "w"), indent=2)
    print(count_results)

    total_time = time.perf_counter() - start_time
    eta_m, eta_s = divmod(total_time, 60)
    eta_h, eta_m = divmod(eta_m, 60)
    eta_h, eta_m, eta_s = int(eta_h), int(eta_m), int(eta_s)

    print("\n" + "=" * 60)
    if dry_run:
        print(bold("  DRY RUN — no files were changed"))
    ext_s = f"   {yellow(f'{extended_count} extended')}" if extended_count else ""
    inc_s = f"   {cyan(f'{inconclusive_count} inconclusive')}" if inconclusive_count else ""
    clu_s = f"   {yellow(f'{clustered_count} clustered')}" if clustered_count else ""
    print(f"  {green(f'{updated} updated')}   {red(f'{skipped} skipped')}{ext_s}{inc_s}{clu_s}")
    print(f"  Time elapsed: {eta_h} hour{'s' if eta_h != 1 else ''}, {eta_m} minute{'s' if eta_m != 1 else ''} and {eta_s} second{'s' if eta_s != 1 else ''}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

class _Tee:
    """Writes to multiple streams simultaneously (e.g. stdout + log file)."""
    def __init__(self, *streams):
        self.streams = streams
    def write(self, data: str) -> None:
        for s in self.streams:
            s.write(data)
    def flush(self) -> None:
        for s in self.streams:
            s.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-calibrate YARG song.ini delay values.")
    parser.add_argument("library",                   type=Path,       help="Path to your YARG song library")
    parser.add_argument("--dry-run",                 action="store_true", help="Preview changes without writing")
    parser.add_argument("--debug",                   action="store_true", help="Show scoring details for all target song files")
    parser.add_argument("--flip-sign",               action="store_true", help="Negate the computed delay")
    parser.add_argument("--skip-inconclusive",       action="store_true", help="Skip all results if below threshold (≥50%% of needed), shown in cyan")
    parser.add_argument("--max-delay",               type=int, default=MAX_DELAY_MS, help="Initial search range in ms")
    parser.add_argument("--skip-existing",           action="store_true", help="Skip songs that already have a delay key but that were not conclusive")
    parser.add_argument("--skip-existing-inconclusive", action="store_true", help="Skip songs that already have a delay key and that were inconclusive")
    parser.add_argument("--skip-existing-clustered", action="store_true", help="Skip songs that already have a delay key and that were clustered")
    parser.add_argument("--keep-skipped",            action="store_true", help="Keep songs that are skipped in the stored json file")
    parser.add_argument("--workers",                 type=int, default=WORKER_THREADS, help=f"Number of parallel worker threads (default: {WORKER_THREADS})")
    parser.add_argument("--delete-cons",             action="store_true", help="Delete CON files that are extracted. Destructive.")
    parser.add_argument("--delete-sngs",             action="store_true", help="Delete SNG files that are extracted. Destructive.")
    parser.add_argument("--overwrite",               action="store_true", help="Overwrite existing folder during CON and SNG extraction.")
    parser.add_argument("--skip-extracted",          action="store_true", help="Skip all song folders that were extracted from CON or SNG files.")
    parser.add_argument("--dump-raw",                action="store_true", help="Dump all the raw files found in CON or SNG packages.")
    parser.add_argument("--shutdown-after",          action="store_true", help="Shutdown system after calibration, useful to start and go to sleep irl.")
    parser.add_argument("--hard-score",              action="store_true", help="Use hard scoring (binary match within tolerance) instead of soft scoring.")
    parser.add_argument("--tolerance",               type=int, default=None, help=f"Onset match window in ms (default: {TOLERANCE_MS}). Try 10 for sharper discrimination.")
    parser.add_argument("--hop-length",              type=int, default=None, help=f"librosa hop length for onset detection (default: {HOP_LENGTH}). Use 256 for finer time resolution at ~2x CPU cost.")
    parser.add_argument("--boundary-bonus",          action="store_true", help="Add extra score weight to first/last 5%% of chart notes to help discriminate one-measure-off errors.")
    parser.add_argument("--streak-bonus", action="store_true", help="Reward consecutive runs of matched onsets.")
    parser.add_argument("--extract", action="store_true", help="Extracts CON and SNG files.")
    parser.add_argument("--extract-only", action="store_true", help="Extracts CON and SNG files without calibrating.")
    args = parser.parse_args()

    if not args.library.is_dir():
        sys.exit(red(f"Not a directory: {args.library}"))

    log_file = None
    if args.shutdown_after:
        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        log_file = open(Path(__file__).parent / f"yarg_sync_log_{timestamp}.txt", "w", encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, log_file)
        sys.stderr = _Tee(sys.__stderr__, log_file)

    interrupted = False
    try:
        process_library(
            args.library,
            dry_run=args.dry_run,
            debug=args.debug,
            max_delay=args.max_delay,
            flip_sign=args.flip_sign,
            skip_inconclusive=args.skip_inconclusive,
            skip_existing=args.skip_existing,
            skip_existing_inconclusive=args.skip_existing_inconclusive,
            skip_existing_clustered=args.skip_existing_clustered,
            keep_skipped=args.keep_skipped,
            workers=args.workers,
            delete_cons=args.delete_cons,
            delete_sngs=args.delete_sngs,
            overwrite=args.overwrite,
            skip_extracted=args.skip_extracted,
            dump_raw=args.dump_raw,
            hard_score=args.hard_score,
            tolerance=args.tolerance,
            hop_length=args.hop_length,
            boundary_bonus=args.boundary_bonus,
            streak_bonus=args.streak_bonus,
            extract=args.extract,
            extract_only=args.extract_only
        )
    except KeyboardInterrupt:
        interrupted = True

    if CACHE_PATH.exists():
        shutil.rmtree(CACHE_PATH)

    if args.shutdown_after and not interrupted:
        if log_file is not None:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__
            log_file.close()
        time.sleep(1)
        if os.name == "nt":
            subprocess.run(["shutdown", "/s", "/t", "0"])
        else:
            subprocess.run(["shutdown", "now"])


if __name__ == "__main__":
    import cProfile
    import pstats

    with cProfile.Profile() as pr:
        main()

    pstats.Stats(pr).sort_stats("cumtime").print_stats(50)
