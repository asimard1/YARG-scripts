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
    import json
    import argparse
    import re
    import struct
    import sys
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path
    from typing import Callable
    import shutil
    from PIL import Image
    import texture2ddecoder
    import numpy as np
except ImportError as e:
    pkg = str(e).split("'")[-2] if "'" in str(e) else "required dependencies"
    print(f"Error: Could not import {pkg}.")
    print("-" * 40)
    print("Please install the required libraries by running:")
    print("pip install librosa numpy soundfile scipy")
    print("-" * 40)
    sys.exit(1)


# --- Constants ---

# Delay search
MAX_DELAY_MS = 100     # initial search range (ms)
COARSE_STEP = 3        # coarse scan spacing before fine refinement
FINE_STEP = 1          # fine scan spacing
EXTEND_LIMIT = 5       # can extend to ±(EXTEND_LIMIT × MAX_DELAY_MS)
TOLERANCE_MS = 15      # onset match window (ms) — overridden by --tolerance
SOFT_SCORE_POWER = 2   # parameter to control the shape of the soft score curve
HOP_LENGTH = 512       # librosa hop length for onset detection — overridden by --hop-length
BOUNDARY_BONUS = False # whether to apply extra weight to first/last 5% of chart notes
BOUNDARY_PCT = 0.05    # fraction of notes at each end that receive the boundary bonus
STREAK_BONUS = False   # whether to apply a multiplier to notes that are isolated
STREAK_BASE = 5.0     # the base of the sigmoid used (\frac{1-a^{-x}}{1+a^{-x}})

# Audio - lowpass emphasizes kick/bass onsets; HPSS isolates percussive transients
LOWPASS_HZ = 200     # lowpass cutoff
SR = 22050           # target sample rate
AUDIO_CLIP_S = 1800  # max seconds of audio to read (None = unlimited)

# Display / runtime
MAX_SONG_NAME = 50
LIST_PATH = Path(__file__).parent / "yarg_sync_list.json"
WORKER_THREADS = 6
ETA_EMA_ALPHA = 0.1

# File format magic bytes
CON_MAGIC = (b"CON ", b"LIVE", b"PIRS")
SNG_MAGIC = b"SNGPKG"
STEM_CANDIDATES = [
    "bass", "drums", "drum", "drums_1", "drums_2", "drums_3", "drums_4",
    "rhythm", "guitar", "keys", "vocals", "backing", "song", "ssong",
    "channel-01", "channel-02", "channel-03", "channel-04", "band"
]
STEM_IGNORE = ["preview", "crowd"]
EXTENSION_LIST = [".ogg", ".opus", ".mp3", ".wav"]
OTHER_EXTENSIONS = [".ini", ".mid", ".bak", ".webm", ".mp4", ".png"]
EXCLUDED_CON_SUFFIXES = frozenset(EXTENSION_LIST + OTHER_EXTENSIONS)

# STFS block geometry
_STFS_BLOCK = 0x1000
_STFS_BASE = 0xC000   # data area start; block 0 is always at 0xC000

# Thresholds
TOTAL_PCT_MIN = 0.1  # overridden to 0.2 in hard-score mode


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


# --- File & library scanning ---

def _check_magic(path: Path, magics: tuple[bytes, ...], debug: bool=False) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(max(len(m) for m in magics))
            dprint(debug, f"Head: {str(head)}")
            return any(head.startswith(m) for m in magics)
    except Exception:
        return False


def is_con_file(path: Path, debug: bool=False) -> bool: return _check_magic(path, CON_MAGIC, debug)
def is_sng_file(path: Path) -> bool: return _check_magic(path, (SNG_MAGIC,))


def is_extracted_path(p: Path) -> bool:
    return any(part.endswith("_extracted") for part in p.parts)


def get_song_list(
    root: Path,
    skip_extracted: bool = False,
    debug: bool = False
) -> tuple[list[Path], int, int, int, list, list, list]:

    cons = sorted(
        p for p in root.rglob("*")
        if p.is_file()
        and p.suffix not in EXCLUDED_CON_SUFFIXES
        and is_con_file(p)
    )
    sngs = sorted(root.rglob("*.sng"))

    # Always exclude _extracted INIs whose parent CON/SNG is present
    # — pre_extract_all handles those, they'll be added back via extracted_map
    excluded_dirs = {Path(str(p) + "_extracted") for p in cons} | \
                    {Path(str(p) + "_extracted") for p in sngs}

    inis = sorted(
        p for p in root.rglob("song.ini")
        if not (skip_extracted and is_extracted_path(p))
        and p.parent not in excluded_dirs
    )

    songs: list[Path] = sorted(inis + cons + sngs)
    return songs, len(inis), len(cons), len(sngs), inis, cons, sngs


# --- STFS / CON container parsing (Xbox 360 packages) ---

def _stfs_block_offset(block: int, table_size_shift: int) -> int:
    """Convert logical block number to byte offset. table_size_shift=1 for CON (two hash tables per 170-block group)."""
    adjust = 0
    if block >= 0xAA:
        adjust += (block // 0xAA + 1) << table_size_shift
    if block >= 0x70E4:
        adjust += (block // 0x70E4 + 1) << table_size_shift
    return _STFS_BASE + (block + adjust) * _STFS_BLOCK


STFS_CHAIN_END = {0xFFFFFF, 0xFFFFFE}  # 0x000000 is NOT a sentinel


def _stfs_next_block(data: bytes, block: int, table_size_shift: int) -> int:
    group = block // 0xAA
    index = block % 0xAA
    hash_off = _stfs_block_offset(group * 0xAB, table_size_shift)
    entry_off = hash_off + index * 0x18
    if entry_off + 0x18 > len(data):
        return 0xFFFFFF
    next_block = int.from_bytes(data[entry_off + 0x15:entry_off + 0x18], "little")
    return next_block if next_block not in STFS_CHAIN_END else 0xFFFFFF


def _stfs_read_file(data: bytes, first_block: int, file_size: int,
                    table_size_shift: int, is_contiguous: bool = False,
                    debug: bool = False, dbg_name: str | None = None) -> bytes:
    if file_size == 0:
        print("File size is zero")
        return b""

    out = bytearray()
    remaining = file_size
    block = first_block
    seen: set[int] = set()
    guard = file_size // _STFS_BLOCK + 16
    crossed_boundary = False

    while remaining > 0 and guard > 0:
        if block in seen:
            dprint(debug, f"SEEN BLOCK {block}")
            break
        seen.add(block)
        if block >= 0xAA:
            crossed_boundary = True
        off = _stfs_block_offset(block, table_size_shift)
        take = min(remaining, _STFS_BLOCK)
        if off < 0 or off >= len(data):
            dprint(debug, f"BAD OFFSET: block={block} off={off} len(data)={len(data)}")
            break
        out.extend(data[off:off + take])
        remaining -= take
        guard -= 1
        if is_contiguous:
            block += 1
            continue
        nb = _stfs_next_block(data, block, table_size_shift)
        if nb == 0xFFFFFF:
            break
        block = nb

    if guard <= 0 and debug:
        print("GUARD EXPIRED")

    if dbg_name:
        flag = " [CROSSED 0xAA BOUNDARY]" if crossed_boundary else ""
        dprint(debug, f"[stfs-read] {dbg_name}: first_blk={first_block:#x} "
              f"want={file_size} got={len(out)}{flag}")

    return bytes(out)


def _debug_check_mid(name: str, raw: bytes, debug: bool = False) -> None:
    ok = raw[:4] == b"MThd"
    dprint(debug, f"[mid-check] {name}: {'OK' if ok else 'BAD HEADER'} "
          f"({len(raw)} bytes, starts {raw[:8]!r})")


def _stfs_utf16be(data: bytes, offset: int, max_chars: int) -> str:
    chars = []
    for i in range(max_chars):
        cp = struct.unpack(">H", data[offset + i*2:offset + i*2 + 2])[0]
        if cp == 0:
            break
        chars.append(chr(cp))
    return "".join(chars)


def _stfs_parse_header(data: bytes, debug: bool) -> dict:
    if data[:4] not in (b"CON ", b"LIVE", b"PIRS"):
        raise ValueError("Not an STFS package")
    info: dict = {}
    try:
        info["display_name"] = _stfs_utf16be(data, 0x0411, 64).strip()
    except Exception:
        info["display_name"] = ""
    try:
        # table_size_shift: 1 if ((EntryID + 0xFFF) & 0xF000) >> 0xC == 0xB else 0
        # We force it to 0 (matches observed CON behaviour)
        info["table_size_shift"] = int(os.environ.get("STFS_SHIFT_OVERRIDE", "0"))
        dprint(debug, f"[FORCED] table_size_shift = {info['table_size_shift']}")
        vd_base = 0x037A
        info["ftbl_count"] = struct.unpack("<H", data[vd_base + 0x02:vd_base + 0x04])[0]
        info["ftbl_start"] = int.from_bytes(data[vd_base + 0x04:vd_base + 0x07], "big")
    except Exception as exc:
        raise ValueError(f"Cannot parse STFS volume descriptor: {exc}")
    return info


def _stfs_list_files(data: bytes, info: dict, debug: bool = False) -> list[dict]:
    tss = info["table_size_shift"]
    entries = []
    seen: set[tuple] = set()

    for i in range(info["ftbl_count"]):
        blk_off = _stfs_block_offset(info["ftbl_start"] + i, tss)
        if blk_off < 0 or blk_off + _STFS_BLOCK > len(data):
            break
        blk = data[blk_off:blk_off + _STFS_BLOCK]

        for j in range(64):
            e = blk[j * 0x40:(j + 1) * 0x40]
            if len(e) < 0x40:
                break
            flags = e[0x28]
            if flags == 0:
                continue
            n_len = flags & 0x3F
            if n_len == 0 or n_len > 40:
                continue
            raw_name = e[:n_len]
            if any(b < 0x20 or b > 0x7E for b in raw_name):
                continue
            try:
                name = raw_name.decode("ascii")
            except UnicodeDecodeError:
                continue
            first = int.from_bytes(e[0x2F:0x32], "little")
            size = struct.unpack(">I", e[0x34:0x38])[0]
            if first > 0xFFFFFF or size == 0 or size > len(data):
                continue
            lower = name.lower()
            if "." not in lower:
                continue
            key = (lower, first, size)
            if key in seen:
                continue
            seen.add(key)
            alloc_blocks = int.from_bytes(e[0x29:0x2C], "little")
            entries.append({
                "name":          name,
                "is_dir":        bool(flags & 0x80),
                "first_blk":     first,
                "size":          size,
                "is_contiguous": bool(flags & 0x40),
                "alloc_blocks":  alloc_blocks,
            })
    return entries


# --- CON file kind classification and MOGG audio extraction ---

# Maps file extensions to routing kind for extraction/metadata handlers.


def _stfs_fallback_scan(data: bytes, debug: bool) -> tuple[bytes | None, bytes | None]:
    """Raw byte scan for MThd/OggS when the STFS file table fails."""
    mid_bytes = mogg_bytes = None

    mid_pos = data.find(b"MThd")
    if mid_pos >= 0:
        try:
            p = mid_pos
            _, hlen = struct.unpack(">4sI", data[p:p + 8])
            _, ntracks, _ = struct.unpack(">HHH", data[p + 8:p + 14])
            end = p + 8 + hlen
            for _ in range(ntracks):
                if data[end:end + 4] != b"MTrk":
                    break
                tlen = struct.unpack(">I", data[end + 4:end + 8])[0]
                end += 8 + tlen
            mid_bytes = data[mid_pos:]
        except Exception:
            pass

    ogg_pos = data.find(b"OggS")
    dprint(debug, f"raw OggS search returned {ogg_pos}")
    if ogg_pos >= 0:
        mogg_bytes = data[ogg_pos:]

    return mid_bytes, mogg_bytes


# --- CON file kind classification ---

_CON_FILE_KINDS = {
    (".mid",):             "mid",
    (".mogg", ".ogg"):     "mogg",
    (".png", ".png_xbox"): "art_png",
    (".jpg", ".jpeg"):     "art_jpg",
    (".dta",):             "dta",
    (".bin",):             "bin",
    (".milo_xbox",):       "milo",
    (".xvocab",):          "xvocab",
    (".voc",):             "voc",
    (".vnn",):             "vnn",
}

# Known-unused extensions — suppress "kind is none" warnings for these.


_CON_KNOWN_IGNORED = {".usr"}


def _classify_con_entry(name: str) -> str | None:
    nl = name.lower()
    for exts, kind in _CON_FILE_KINDS.items():
        if any(nl.endswith(e) for e in exts):
            return kind
    return None


# --- DTA metadata parsing (Rock Band songs.dta) ---

def _tokenize_dta(text: str):
    """Tokenize a Rock Band DTA S-expression: strip ; comments, return quoted strings, parens, and atoms."""
    text = re.sub(r";[^\r\n]*", "", text)
    return re.findall(r'"[^"]*"|\(|\)|[^\s()]+', text)


def _parse_dta_tree(tokens):
    """Parse DTA tokens into a nested list structure."""
    def parse_expr():
        if not tokens:
            return None
        token = tokens.pop(0)
        if token == "(":
            result = []
            while tokens and tokens[0] != ")":
                child = parse_expr()
                if child is not None:
                    result.append(child)
            if tokens and tokens[0] == ")":
                tokens.pop(0)
            return result
        return None if token == ")" else token

    roots = []
    while tokens:
        expr = parse_expr()
        if expr is not None:
            roots.append(expr)
    return roots


def _dta_convert_value(value):
    if not isinstance(value, str):
        return value
    value = value.strip()
    # Strip either matching double or single quotes.
    if (len(value) >= 2 and ((value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'")))):
        value = value[1:-1]
    if value.lower() == "true":  return True
    if value.lower() == "false": return False
    try: return int(value)
    except ValueError: pass
    try: return float(value)
    except ValueError: pass
    return value


def _dta_convert_list(values):
    return [_dta_convert_value(v) if not isinstance(v, list) else _dta_convert_node(v) for v in values]


def _dta_convert_node(node):
    if not isinstance(node, list) or not node:
        return _dta_convert_value(node)
    key = _dta_convert_value(node[0])
    if not isinstance(key, str):
        return _dta_convert_list(node)
    children = node[1:]
    if not children:
        return {key: None}
    if len(children) == 1 and not isinstance(children[0], list):
        return {key: _dta_convert_value(children[0])}
    if all(not isinstance(child, list) for child in children):
        return {key: _dta_convert_list(children)}
    # Nested block: (rank (drum 5) (bass 2))
    result = {}
    for child in children:
        if isinstance(child, list):
            child_data = _dta_convert_node(child)
            if isinstance(child_data, dict):
                for child_key, child_value in child_data.items():
                    # preserve duplicates as lists
                    if child_key in result:
                        if not isinstance(result[child_key], list):
                            result[child_key] = [result[child_key]]
                        result[child_key].append(child_value)
                    else:
                        result[child_key] = child_value
            else:
                result.setdefault("_values", []).append(child_data)
        else:
            result.setdefault("_values", []).append(_dta_convert_value(child))
    return {key: result}


def _parse_songs_dta_grouped(text: str) -> dict[str, dict]:
    """Parse a songs.dta into {shortname: metadata_dict}, one entry per song.
    A single-song CON produces one entry; a multi-song CON pack (e.g. Rock
    Band Network compilations) produces one entry per bundled song, keyed by
    its DTA shortname (e.g. 'spoonman2', 'UGC_5000196').
    """
    songs: dict[str, dict] = {}
    try:
        tokens = _tokenize_dta(text)
        tree = _parse_dta_tree(tokens)
    except Exception as e:
        print(f"DTA parse failed: {e}")
        return songs

    for root in tree:
        if not isinstance(root, list) or not root:
            continue
        parsed = _dta_convert_node(root)
        if not isinstance(parsed, dict):
            continue
        for key, value in parsed.items():
            if isinstance(value, dict):
                songs[key] = value

    return songs


def _parse_songs_dta(text: str) -> dict:
    """Parse Rock Band songs.dta (Lisp-like S-expressions) into a flat Python dict.
    Handles simple scalars (artist "Foo") and nested structures (tracks (drum (0 1)) ..).
    For multi-song packs this flattens all songs together (last one wins on
    conflicting keys) — use _parse_songs_dta_grouped() to keep songs separate.
    """
    meta: dict = {}
    for value in _parse_songs_dta_grouped(text).values():
        meta.update(value)
    return meta


# --- MIDI parsing ---

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


# --- Entry point ---


def notes_from_mid(mid_path: Path, debug: bool) -> dict[str, np.ndarray]:
    try:
        parsed = _parse_midi_file(mid_path.read_bytes())
        if parsed is None:
            return {}
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
        return result
    except Exception as e:
        dprint(debug, f"exception in notes_from_mid: {e}")
        return {}
    

# --- Chart parsing — .chart format ---


_MIDI_INSTRUMENT_TRACKS = {
    "PART GUITAR":      "guitar",
    "PART BASS":        "bass",
    "PART DRUMS":       "drums",
    "PART VOCALS":      "vocals",
    "PART KEYS":        "keys",
    "PART RHYTHM":      "rhythm",
    "PART GUITAR COOP": "guitar",
    "PART REAL_GUITAR": "guitar",
    "PART REAL_BASS":   "bass",
    "PART KEYS_X":      "keys",
}


def midi_metadata_from_bytes(data: bytes, debug: bool = False) -> dict:
    try:
        parsed = _parse_midi_file(data)
        if parsed is None:
            return {}
        tpq, bpm_map, all_tracks = parsed
        meta: dict = {}
        found_sections = []
        vocal_parts = 0
        last_tick = 0
        for track_events in all_tracks:
            track_name = None
            has_notes = False
            for event in track_events:
                last_tick = max(last_tick, event[1])
                if event[0] == "meta" and event[2] == 0x03:
                    track_name = event[3].decode("utf-8", errors="replace")
                elif event[0] == "meta" and event[2] in (0x01, 0x05):
                    text = event[3].decode("utf-8", errors="replace")
                    if text.lower().startswith("["):
                        found_sections.append(text)
                elif event[0] == "midi2" and (event[2] & 0xF0) == 0x90 and event[4] > 0:
                    has_notes = True
            if track_name:
                upper = track_name.upper()
                if upper in _MIDI_INSTRUMENT_TRACKS and has_notes:
                    meta[f"has_{_MIDI_INSTRUMENT_TRACKS[upper]}"] = True
                if upper.startswith("HARM"):
                    vocal_parts += 1
        if vocal_parts:
            meta["vocal_parts"] = vocal_parts
        if last_tick > 0:
            meta["song_length"] = int(_ticks_to_ms(np.array([last_tick], dtype=np.float64), tpq, bpm_map)[0])
        if found_sections:
            meta["sections"] = ",".join(dict.fromkeys(
                t[9:-1] for t in found_sections if t.lower().startswith("[section")
            ))
        return meta
    except Exception as exc:
        dprint(debug, f"midi_metadata failed: {exc}")
        return {}


def midi_metadata(mid_path: Path, debug: bool = False) -> dict:
    try:
        return midi_metadata_from_bytes(mid_path.read_bytes(), debug)
    except Exception:
        return {}
    


# --- STFS / CON parsing — Xbox 360 Rock Band packages ---


# --- Album art (Xbox PNG/BC1 texture decoding) ---

HEADER_SIZE = 32
BC1_BLOCK_SIZE = 8


def swap_bc1_endian(data: bytes) -> bytes:
    """Swap the RGB565 endpoints and the BC1 lookup bytes."""
    out = bytearray(len(data))
    for i in range(0, len(data), 8):
        block = bytearray(data[i:i + 8])
        if len(block) < 8:
            break
        # RGB565 endpoints
        block[0], block[1] = block[1], block[0]
        block[2], block[3] = block[3], block[2]
        # BC1 lookup table
        block[4], block[5] = block[5], block[4]
        block[6], block[7] = block[7], block[6]
        out[i:i + 8] = block
    return bytes(out)


def decode_png_xbox(raw: bytes):
    width = height = 256
    image_size = (width // 4) * (height // 4) * BC1_BLOCK_SIZE
    pixel_data = swap_bc1_endian(raw[HEADER_SIZE:HEADER_SIZE + image_size])
    rgba = texture2ddecoder.decode_bc1(pixel_data, width, height)
    return Image.frombytes("RGBA", (width, height), rgba, "raw", "BGRA")


# --- MOGG audio splitting (ffmpeg stem separation) ---

import math


def _db_to_gain(db: float) -> float:
    return math.pow(10.0, db / 20.0)


def _pan_coefficients(pan: float) -> tuple[float, float]:
    """
    Rock Band pan:
        -1 = full left
         0 = center
         1 = full right
    """
    pan = max(-1.0, min(1.0, float(pan)))
    left = (1.0 - pan) / 2.0
    right = (1.0 + pan) / 2.0
    return left, right

def _split_mogg(
    mogg_bytes: bytes,
    dest_dir: Path,
    debug: bool,
    song_info: dict | None,
    quality: int = 6,
    cancel_event: threading.Event | None = None,
) -> None:

    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found on PATH — install it with 'winget install Gyan.FFmpeg' to use _split_mogg()"
        )
    ffprobe_found = True
    if shutil.which("ffprobe") is None:
        print("ffprobe not found on PATH — reverting to soundfile")
        ffprobe_found = False

    # Locate OggS stream inside MOGG
    if len(mogg_bytes) >= 8:
        ogg_offset = struct.unpack("<I", mogg_bytes[4:8])[0]
        if 0 <= ogg_offset < len(mogg_bytes) and mogg_bytes[ogg_offset:ogg_offset + 4] == b"OggS":
            ogg_bytes = mogg_bytes[ogg_offset:]
        else:
            ogg_pos = mogg_bytes.find(b"OggS")
            ogg_bytes = mogg_bytes[ogg_pos:] if ogg_pos >= 0 else mogg_bytes
    else:
        ogg_bytes = mogg_bytes

    # Probe channel count via ffprobe (no full decode)
    def _get_channel_count(data: bytes) -> int:
        if ffprobe_found:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=channels",
                "-of", "default=nokey=1:noprint_wrappers=1",
                "pipe:0",
            ]
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except FileNotFoundError as e:
                raise RuntimeError(
                    f"Executable not found: {e.filename!r} while running: {' '.join(cmd)}"
                ) from e

            out, _ = proc.communicate(input=data)
            try:
                return int(out.strip())
            except Exception:
                return 2
        else:
            import soundfile as sf
            y_multi, sr_orig = sf.read(io.BytesIO(ogg_bytes), always_2d=True, dtype="float32")
            dprint(debug, f"mogg read OK: {y_multi.shape[1]} channels, sr={sr_orig}")
            return y_multi.shape[1]

    n_ch = _get_channel_count(ogg_bytes)
    dprint(debug, f"MOGG channels detected: {n_ch}")

    TEMP_STEM_NAMES = {
        "drum": "drums",
        "bass": "bass",
        "guitar": "guitar",
        "vocals": "vocals",
        "keys": "keys",
        "crowd": "crowd",
        "backing": "backing",
        "rhythm": "rhythm",
    }

    stem_names: dict[int, str | None] = {}
    stem_channels: dict[int, list[int]] = {}
    assigned_channels: set[int] = set()

    channel_vols = [0.0] * n_ch
    channel_pans = [0.0] * n_ch

    # Parse DTA-style tracks. Each entry can be either an int (single channel,
    # e.g. {"bass": 6}) or a dict wrapping a channel list (e.g. {"drum":
    # {"_values": [[0,1,2,3,4,5]]}}). A bad entry should be skipped, not
    # abort parsing of the rest of the track list.
    try:
        if song_info:
            tracks = (song_info.get("tracks") or {}).get("_values")
            if isinstance(tracks, list) and tracks and isinstance(tracks[0], list):
                group = tracks[0]
                for i, entry in enumerate(group):
                    if not isinstance(entry, dict):
                        continue
                    name = next(iter(entry.keys()), None)
                    if not name:
                        continue

                    try:
                        value = entry[name]
                        if isinstance(value, int):
                            channels = [value]
                        elif isinstance(value, dict):
                            raw = value.get("_values", [])
                            if raw and isinstance(raw[0], list):
                                channels = raw[0]
                            elif isinstance(raw, list):
                                channels = raw
                            else:
                                channels = []
                        else:
                            channels = []

                        if not isinstance(channels, list) or not channels:
                            continue

                        stem_name = TEMP_STEM_NAMES.get(name, name)
                        stem_names[i] = stem_name
                        stem_channels[i] = channels
                        for c in channels:
                            if isinstance(c, int):
                                assigned_channels.add(c)
                    except Exception as e:
                        dprint(debug, f"DTA parse error for {name!r}: {e}")
                        continue
    except Exception as e:
        dprint(debug, f"DTA parse error: {e}")

    try:
        if song_info:
            vols = song_info.get("vols")
            if isinstance(vols, dict):
                raw = vols.get("_values", [])
                if raw and isinstance(raw[0], list):
                    values = raw[0]
                    for i, value in enumerate(values[:n_ch]):
                        channel_vols[i] = float(value)

            pans = song_info.get("pans")
            if isinstance(pans, dict):
                raw = pans.get("_values", [])
                if raw and isinstance(raw[0], list):
                    values = raw[0]
                    for i, value in enumerate(values[:n_ch]):
                        channel_pans[i] = float(value)
    except Exception as e:
        dprint(debug, f"Failed reading pans/vols: {e}")

    # Any unassigned channels become "backing"
    unassigned = [c for c in range(n_ch) if c not in assigned_channels]
    if unassigned:
        stem_names[len(stem_names)] = "backing"
        stem_channels_extra = unassigned
    else:
        stem_channels_extra = []

    # Build ffmpeg filter graph
    filter_parts = []

    left_terms = []
    right_terms = []

    for ch in range(n_ch):
        gain = _db_to_gain(channel_vols[ch])
        left_pan, right_pan = _pan_coefficients(channel_pans[ch])

        if left_pan:
            left_terms.append(f"{gain * left_pan:.6f}*c{ch}")

        if right_pan:
            right_terms.append(f"{gain * right_pan:.6f}*c{ch}")

    left_expr = "+".join(left_terms) or "0"
    right_expr = "+".join(right_terms) or "0"

    filter_parts.append(
        f"[0:a]pan=stereo|c0={left_expr}|c1={right_expr}[mix]"
    )

    def _pan(channels: list[int], label: str) -> str:
        left_terms = []
        right_terms = []

        for ch in channels:
            gain = _db_to_gain(channel_vols[ch])
            left_pan, right_pan = _pan_coefficients(channel_pans[ch])

            if left_pan:
                left_terms.append(f"{gain * left_pan:.6f}*c{ch}")

            if right_pan:
                right_terms.append(f"{gain * right_pan:.6f}*c{ch}")

        if not right_terms:
            return (
                f"[0:a]pan=mono|"
                f"c0={'+'.join(left_terms) or '0*c0'}"
                f"[{label}]"
            )

        return (
            f"[0:a]pan=stereo|"
            f"c0={'+'.join(left_terms) or '0*c0'}|"
            f"c1={'+'.join(right_terms) or '0*c0'}"
            f"[{label}]"
        )

    output_args = [
        "-map", "[mix]",
        "-c:a", "libvorbis",
        "-q:a", str(quality),
        str(dest_dir / "song.ogg"),
    ]

    # Build stems from DTA
    for idx, name in stem_names.items():
        channels = stem_channels.get(idx, [])
        if not channels:
            continue

        label = f"stem{idx}"
        filter_parts.append(_pan(channels, label))

        out_path = dest_dir / f"{name}.ogg"
        output_args += [
            "-map", f"[{label}]",
            "-c:a", "libvorbis",
            "-q:a", str(quality),
            str(out_path),
        ]

    # Add backing stem if needed
    if stem_channels_extra:
        label = "stem_backing"
        filter_parts.append(_pan(stem_channels_extra, label))

        out_path = dest_dir / "backing.ogg"
        output_args += [
            "-map", f"[{label}]",
            "-c:a", "libvorbis",
            "-q:a", str(quality),
            str(out_path),
        ]

    done_event = threading.Event()

    cmd = ["ffmpeg", "-y", "-i", "pipe:0", "-filter_complex", ";".join(filter_parts)] + output_args
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Executable not found: {e.filename!r} while running: {' '.join(cmd)}"
        ) from e

    def _watch_cancel():
        if cancel_event is not None:
            cancel_event.wait()
            if not done_event.is_set():
                proc.kill()

    watcher = threading.Thread(target=_watch_cancel, daemon=True)
    watcher.start()

    try:
        stdout, stderr = proc.communicate(input=ogg_bytes)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')}")
    finally:
        done_event.set()
        watcher.join(timeout=1)
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("cancelled during mogg split")


# --- Chart parsing — MIDI format ---


# --- song.ini writing & extraction-cache helpers ---

def _write_song_ini(dest_dir: Path, name: str, artist: str, extra: dict,
                     shortname: str | None = None, dtaname: str | None = None) -> None:
    """Write song.ini from extracted metadata, skipping numeric-only keys and reserved fields."""
    SKIP_KEYS = {"name", "title", "artist", "delay", "song", "shortname", "dtaname"}
    lines = ["[song]", f"name = {name}", f"artist = {artist}", "delay = 0"]
    if shortname:
        lines.append(f"shortname = {shortname}")
    if dtaname:
        lines.append(f"dtaname = {dtaname}")
    lines += [f"{key} = {value}" for key, value in extra.items()
              if not str(key).isdigit() and key not in SKIP_KEYS]
    if not dest_dir.exists():
        dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "song.ini").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _has_audio(folder: Path) -> bool:
    return any(p.stat().st_size > 0 for p in folder.glob("*") if p.suffix.lower() in EXTENSION_LIST)


def find_audio_candidates(folder: Path, calibration=True) -> list[Path]:
    """Return audio files sorted by stem priority then alphabetically."""

    candidates = [
        p for p in folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in EXTENSION_LIST
    ]
    if calibration:
        candidates = [
            p for p in candidates
            if p.stem.lower() not in STEM_IGNORE
            and p.stem.lower() in STEM_CANDIDATES
        ]
    if len(candidates) == 0:
        dprint(True, "No audio candidates were found. Fallback to song audio.")
        to_add = [
            p for p in folder.rglob("song.*")
            if p.is_file()
            and p.suffix.lower() in EXTENSION_LIST
        ]
        candidates += to_add
    return candidates


def _is_extraction_cache_fresh(source: Path, dest: Path, require_chart_or_mid: bool = False) -> bool:
    # if not _has_audio(dest):
    #     return False
    # if require_chart_or_mid:
    #     has_chart = (dest / "notes.mid").exists() or (dest / "notes.chart").exists()
    #     return has_chart and source.stat().st_mtime <= dest.stat().st_mtime
    # return (dest / "notes.mid").exists() and source.stat().st_mtime <= dest.stat().st_mtime
    return False # still needs some work, can't check for unfinished .ogg files or deleted files


# --- CON extraction (Xbox 360 package -> folder) ---

def _sanitize_folder_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    return name or "song"


def _entry_suffix_after_basename(entry_name: str, basename: str) -> str:
    """Returns the part of the filename after the basename (before the extension)."""
    stem = entry_name.rsplit(".", 1)[0]
    return stem[len(basename):] if stem.lower().startswith(basename.lower()) else stem


def _set_kind_with_priority(files: dict, kind: str, name: str, raw: bytes, basename: str) -> None:
    """Store (name, raw) under `kind`, preferring exact basename matches over
    any variant with an underscore suffix (e.g. '_orig', '_alt', '_old')."""
    suffix = _entry_suffix_after_basename(name, basename)
    is_suffixed = suffix.startswith("_")

    existing = files.get(kind)
    if existing is None:
        files[kind] = (name, raw)
        return

    existing_suffix = _entry_suffix_after_basename(existing[0], basename)
    existing_is_suffixed = existing_suffix.startswith("_")

    if existing_is_suffixed and not is_suffixed:
        files[kind] = (name, raw)  # new one is the "clean" name, replace


def _entry_matches_song_basename(entry_name: str, basename: str) -> bool:
    """True if an STFS entry belongs to the given song basename.
    Requires an exact prefix match followed by '.' or '_' (or nothing) so
    'spoonman2' doesn't accidentally match 'spoonman20.mid'.
    """
    if not entry_name.lower().startswith(basename.lower()):
        return False
    rest = entry_name[len(basename):]
    return rest == "" or rest[0] in "._"


def _find_multi_song_dta(entries: list[dict], data: bytes, table_size_shift: int, debug: bool) -> dict[str, dict]:
    """Return {shortname: meta} if this CON bundles 2+ songs, else {}."""
    dta_entry = next((e for e in entries if e["name"].lower() == "songs.dta"), None)
    if not dta_entry:
        return {}
    raw = _stfs_read_file(data, dta_entry["first_blk"], dta_entry["size"], table_size_shift,
                          is_contiguous=dta_entry.get("alloc_blocks", 0) > 0, debug=debug)
    if not raw:
        return {}
    grouped = _parse_songs_dta_grouped(raw.decode("utf-8", errors="ignore"))
    return grouped if len(grouped) > 1 else {}


def con_extract_multi_to_folder(
    con_path: Path,
    songs_meta: dict[str, dict],
    entries: list[dict],
    data: bytes,
    table_size_shift: int,
    dump_raw: bool,
    dest_dir: Path | None = None,
    debug: bool = False,
    overwrite: bool = False,
    cancel_event: threading.Event | None = None,
    on_song_done: Callable[[Path, float, int, int], None] | None = None,
) -> list[Path]:
    """Extract a multi-song CON pack (e.g. RBN compilation) into one subfolder
    per bundled song under dest_dir. Returns the list of per-song folders."""
    print(f"Found files: {[e.get('name') for e in entries]}")
    # for e in entries:
    #     name = e["name"]
    #     if not name.endswith((".bin", ".mid", ".mogg", ".usr", ".vnn", ".voc", ".xvocab")):
    #         print(e["name"])
    base_dest = dest_dir or Path(str(con_path) + "_extracted")
    base_dest.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    t_prev = time.perf_counter()

    print(f"Songs metadata: {songs_meta}")
    for i, (shortname, meta) in enumerate(songs_meta.items()):
        # if shortname not in ["warpigs", "youshookme_live", "youngerbums"]:
        #     continue
        song_blob = meta.get("song")
        basename = shortname
        if isinstance(song_blob, dict) and isinstance(song_blob.get("name"), str) and song_blob["name"]:
            basename = song_blob["name"].rsplit("/", 1)[-1]

        title = meta.get("name") or basename
        artist = meta.get("artist") or "Unknown Artist"
        folder_name = _sanitize_folder_name(f"{artist} - {title}")
        song_dest = _prepare_con_extraction_folder(con_path, base_dest / folder_name, debug, overwrite, not not i)
        if song_dest is None:
            continue

        files: dict[str, tuple[str, bytes]] = {}
        for entry in entries:
            if entry["is_dir"] or entry["size"] == 0:
                continue
            if not _entry_matches_song_basename(entry["name"], basename):
                continue
            raw = _stfs_read_file(data, entry["first_blk"], entry["size"], table_size_shift,
                                  is_contiguous=entry.get("alloc_blocks", 0) > 0, debug=debug,
                                  dbg_name=f"{basename}/{entry['name']}")
            if not raw:
                continue
            if dump_raw:
                (song_dest / "raw").mkdir(parents=True, exist_ok=True)
                (song_dest / "raw" / entry["name"]).write_bytes(raw)
            kind = _classify_con_entry(entry["name"])
            if kind == "mid":
                _debug_check_mid(f"{basename}/{entry['name']}", raw, debug)
            if kind:
                _set_kind_with_priority(files, kind, entry["name"], raw, basename)

        art_png_info = files.get("art_png")
        if art_png_info:
            name, raw = art_png_info
            try:
                decode_png_xbox(raw).save(song_dest / "album.png")
            except Exception as e:
                dprint(debug, f"Failed to decode album art ({name}): {e}")

        mid_bytes = files.get("mid", (None, None))[1]
        mogg_bytes = files.get("mogg", (None, None))[1]

        dta_meta = dict(meta)
        if mid_bytes:
            for key, value in midi_metadata_from_bytes(mid_bytes, debug).items():
                dta_meta.setdefault(key, value)

        # in con_extract_multi_to_folder (~line 1228):
        song_info = _write_con_song_ini(con_path, song_dest, basename, dta_meta, dtaname=shortname, dump_raw=dump_raw, debug=debug)
        _write_song_assets(song_dest, mid_bytes, mogg_bytes, debug, song_info, cancel_event=cancel_event)
        (song_dest / ".extraction_complete").touch()
        results.append(song_dest)
        now = time.perf_counter()
        if on_song_done:
            on_song_done(song_dest, now - t_prev, i + 1, len(songs_meta))
        t_prev = now

    return results


def con_extract_to_folder(
    con_path: Path,
    dump_raw: bool,
    dest_dir: Path | None = None,
    debug: bool = False,
    overwrite: bool = False,
    cancel_event: threading.Event | None = None,
    on_song_done: Callable[[Path, float, int, int], None] | None = None,
) -> Path | None:
    data = con_path.read_bytes()
    info = _stfs_parse_header(data, debug)
    entries = _stfs_list_files(data, info, debug)

    # For RB1 export, entries is missing all the png stuff
    songs_meta = _find_multi_song_dta(entries, data, info["table_size_shift"], debug)
    if songs_meta:
        parent = dest_dir or Path(str(con_path) + "_extracted")
        con_extract_multi_to_folder(con_path, songs_meta, entries, data, info["table_size_shift"],
                                    dump_raw, parent, debug, overwrite, cancel_event, on_song_done)
        return parent

    dest_dir = _prepare_con_extraction_folder(con_path, dest_dir, debug, overwrite)
    if dest_dir is None:
        return None
    display, files = _extract_stfs_files(data, entries, con_path, dest_dir, debug, dump_raw)

    art_png_info = files.get("art_png")
    if art_png_info:
        name, raw = art_png_info
        dprint(debug, f"Album art: {name} ({len(raw)} bytes)")
        try:
            decode_png_xbox(raw).save(dest_dir / "album.png")
            dprint(debug, "Album art extracted successfully.")
        except Exception as e:
            dprint(debug, f"Failed to decode album art ({name}): {e}")

    # in con_extract_to_folder (~line 1275-1282):
    dtaname, dta_meta = _load_dta_metadata(files, debug)
    mid_bytes, mogg_bytes = _load_song_assets(data, files, debug)

    if mid_bytes:
        for key, value in midi_metadata_from_bytes(mid_bytes, debug).items():
            dta_meta.setdefault(key, value)

    song_info = _write_con_song_ini(con_path, dest_dir, display, dta_meta, dtaname=dtaname, dump_raw=dump_raw, debug=debug)
    _write_song_assets(dest_dir, mid_bytes, mogg_bytes, debug, song_info, cancel_event=cancel_event)
    (dest_dir / ".extraction_complete").touch()
    return dest_dir


def _prepare_con_extraction_folder(con_path: Path, dest_dir: Path | None, debug: bool, overwrite: bool,
                                   create_folder_with_number: bool = False) -> Path | None:
    if dest_dir is None:
        dest_dir = Path(str(con_path) + "_extracted")
    # try:
    #     print("stat:", dest_dir.stat())
    # except Exception as e:
    #     print("stat failed:", e)
    if dest_dir.exists():
        if not overwrite:
            print("Should we add a test here?")
            print(f"I want to create the folder: {dest_dir}")
            print(repr(str(dest_dir)))
            print(dest_dir.resolve())
            if not create_folder_with_number:
                raise FileExistsError(f"The folder '{dest_dir}' already exists.")
            else:
                dest_dir_temp = dest_dir
                i = 1
                while dest_dir_temp.exists():
                    dest_dir_temp = Path(str(dest_dir) + f"({i})")
                    i += 1
                dest_dir = dest_dir_temp
        if _is_extraction_cache_fresh(con_path, dest_dir):
            dprint(debug, "skipping extraction, cache is fresh")
            return None
    dprint(debug, f"creating folder {dest_dir}")

    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            if dest_dir.exists() and dest_dir.is_dir():
                # Iterate over items in reverse order to remove nested files/folders first
                for item in sorted(dest_dir.rglob("*"), reverse=True):
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        item.rmdir()
                dest_dir.rmdir()
                time.sleep(1/100)
            break
        except (PermissionError, OSError) as e:
            # Transient on Windows: AV or Explorer thumbnailing briefly locks a
            # just-written file (e.g. album.png). Wait and retry a few times.
            last_exc = e
            dprint(debug, f"folder busy, retrying ({attempt + 1}/5): {e}")
            time.sleep(0.3 * (attempt + 1))
    else:
        if last_exc is not None:
            raise last_exc

    dest_dir.mkdir(parents=True, exist_ok=True)
    dprint(debug, f"{dest_dir} created")
    return dest_dir


def _extract_stfs_files(data: bytes, entries: list, con_path: Path, dest_dir: Path, debug: bool, dump_raw: bool) -> tuple[str, dict[str, tuple[str, bytes]]]:
    files: dict[str, tuple[str, bytes]] = {}
    display = con_path.stem
    try:
        info = _stfs_parse_header(data, debug)
        display = info.get("display_name") or con_path.stem

        for entry in entries:
            if entry["is_dir"] or entry["size"] == 0:
                dprint(debug, f"[refused entry]\n      [entry] {entry}")
                continue

            is_contiguous = entry.get("alloc_blocks", 0) > 0
            raw = _stfs_read_file(data, entry["first_blk"], entry["size"],
                                  info["table_size_shift"], is_contiguous=is_contiguous, debug=debug,
                                  dbg_name=entry["name"])
            if not raw:
                continue

            if dump_raw:
                (dest_dir / "raw").mkdir(parents=True, exist_ok=True)
                (dest_dir / "raw" / entry["name"]).write_bytes(raw)
            kind = _classify_con_entry(entry["name"])
            if kind == "mid":
                _debug_check_mid(entry["name"], raw, debug)

            if kind is None:
                if not any(entry["name"].lower().endswith(ext) for ext in _CON_KNOWN_IGNORED):
                    dprint(debug, f"Kind is none: {entry['name']}. Song: {con_path.name}")
                continue

            files.setdefault(kind, (entry["name"], raw))
            dprint(debug, f"extracted {entry['name']} ({len(raw):,} bytes) as {kind}")

    except Exception as exc:
        print(exc)

    return display, files


def _load_dta_metadata(files: dict, debug: bool) -> tuple[str | None, dict]:
    dta_bytes = files.get("dta", (None, None))[1]
    if not dta_bytes:
        return None, {}
    grouped = _parse_songs_dta_grouped(dta_bytes.decode("utf-8", errors="ignore"))
    if not grouped:
        return None, {}
    dtaname, meta = next(iter(grouped.items()))
    return dtaname, meta


def _load_song_assets(data: bytes, files: dict, debug: bool) -> tuple[bytes | None, bytes | None]:
    mid_bytes = files.get("mid", (None, None))[1]
    mogg_bytes = files.get("mogg", (None, None))[1]
    if mid_bytes is not None and mogg_bytes is not None:
        return mid_bytes, mogg_bytes
    fb_mid, fb_mogg = _stfs_fallback_scan(data, debug)
    return mid_bytes or fb_mid, mogg_bytes or fb_mogg


def _write_song_assets(dest_dir: Path, mid_bytes: bytes | None, mogg_bytes: bytes | None,
                       debug: bool, song_info: dict | None, cancel_event: threading.Event | None = None) -> None:
    if mid_bytes:
        (dest_dir / "notes.mid").write_bytes(mid_bytes)
    if mogg_bytes:
        _split_mogg(mogg_bytes, dest_dir, debug, song_info=song_info, cancel_event=cancel_event)


def _write_con_song_ini(con_path: Path, dest_dir: Path, display: str, dta_meta: dict,
                        dtaname: str | None = None,
                        dump_raw: bool = False, debug: bool = False) -> dict | None:
    if dump_raw and debug:
        try:
            (dest_dir / "dta_meta_debug.json").write_text(
                json.dumps(dta_meta, indent=2, default=str), encoding="utf-8"
            )
        except Exception:
            pass

    con_parts = con_path.name.split(" - ")
    default_artist = con_parts[0] if len(con_parts) > 0 else "Unknown Artist"
    default_name = con_parts[-1] if len(con_parts) > 1 else display

    name = dta_meta.get("name") or dta_meta.get("title") or default_name
    name = str(name)
    if "/" in name:
        name = default_name
    artist = dta_meta.get("artist") or default_artist

    song_blob = dta_meta.get("song")
    shortname = None
    if isinstance(song_blob, dict) and isinstance(song_blob.get("name"), str) and song_blob["name"]:
        shortname = "/".join(song_blob["name"].split("/")[2:])

    key_change = {
        "year_released": "year", "album_name": "album", "author": "charter",
        "preview_start": "preview_start_time", "preview_end": "preview_end_time",
        "album_track_number": "album_track", "game_origin": "icon",
    }
    instrum_change = {
        "drum": "drums"
    }

    extra = {}
    for key, value in dta_meta.items():
        if key == "preview":
            if isinstance(value, list) and len(value) >= 2:
                extra["preview_start_time"] = value[0]
                extra["preview_end_time"] = value[1]
            continue
        if key == "rank":
            if isinstance(value, dict):
                for instrument, rank_val in value.items():
                    if instrument in STEM_CANDIDATES:
                        try:
                            instrument_name = instrument
                            if instrument in instrum_change:
                                instrument_name = instrum_change[instrument]
                            rank = int(rank_val)
                            n = 1000; m = (n-2)/15; k = -m/2+2
                            diff = max(0, min(16, round((rank-k)/m)))
                            extra[f"diff_{instrument_name}"] = diff
                        except (TypeError, ValueError):
                            pass
            continue
        extra[key_change.get(key, key)] = value

    _write_song_ini(dest_dir, name, artist, extra, shortname=shortname, dtaname=dtaname)
    return extra.get("song")


# --- SNG extraction (Clone Hero / YARG format) ---

def sng_extract_to_folder(
    sng_path: Path,
    dump_raw: bool,
    dest_dir: Path | None = None,
    debug: bool = False,
    overwrite: bool = False,
    cancel_event: threading.Event | None = None,  # ignored for now
    on_song_done: Callable[[Path, float], None] | None = None,  # ignored, one song per .sng
) -> Path:
    """Extract a .sng container to a folder."""
    dest_dir = _prepare_sng_extraction_folder(sng_path, dest_dir, overwrite, debug)
    data = sng_path.read_bytes()
    pos = 0

    # Header
    if len(data) < 26 or data[:6] != SNG_MAGIC:
        raise ValueError(f"Not an SNG file (bad magic): {sng_path}")

    pos += 6
    version = struct.unpack_from("<I", data, pos)[0]; pos += 4
    xor_mask = data[pos:pos + 16]; pos += 16
    dprint(debug, f"SNG version={version}, xor_mask={xor_mask.hex()}")

    # Metadata section
    _meta_len = struct.unpack_from("<Q", data, pos)[0]; pos += 8
    meta_count = struct.unpack_from("<Q", data, pos)[0]; pos += 8
    metadata: dict[str, str] = {}
    for _ in range(meta_count):
        key_len = struct.unpack_from("<i", data, pos)[0]; pos += 4
        key = data[pos:pos + key_len].decode("utf-8", errors="replace"); pos += key_len
        val_len = struct.unpack_from("<i", data, pos)[0]; pos += 4
        val = data[pos:pos + val_len].decode("utf-8", errors="replace"); pos += val_len
        metadata[key] = val
    dprint(debug, f"SNG metadata ({len(metadata)} keys): name={metadata.get('name')!r} artist={metadata.get('artist')!r}")

    # File index section
    _idx_len = struct.unpack_from("<Q", data, pos)[0]; pos += 8
    file_count = struct.unpack_from("<Q", data, pos)[0]; pos += 8
    file_metas: list[tuple[str, int, int]] = []
    for _ in range(file_count):
        fname_len = data[pos]; pos += 1
        fname = data[pos:pos + fname_len].decode("utf-8", errors="replace"); pos += fname_len
        contents_len = struct.unpack_from("<Q", data, pos)[0]; pos += 8
        contents_idx = struct.unpack_from("<Q", data, pos)[0]; pos += 8
        file_metas.append((fname, contents_len, contents_idx))

    dprint(debug, f"SNG file index: {file_count} entries")
    if debug:
        for fname, clen, cidx in file_metas:
            dprint(debug, f"{fname}: {clen:,} bytes @ abs offset {cidx}")

    pos += 8  # skip FileData section-length field
    _extract_sng_files(data, xor_mask, file_metas, dest_dir, debug)
    _write_sng_song_ini(sng_path, dest_dir, metadata, debug)
    (dest_dir / ".extraction_complete").touch()
    return dest_dir


# --- MIDI metadata extraction ---


def _sng_unmask(data: bytes, xor_mask: bytes) -> bytes:
    """Unmask file bytes. i resets to 0 for each file."""
    out = bytearray(len(data))
    for i, b in enumerate(data):
        xor_key = xor_mask[i % 16] ^ (i & 0xFF)
        out[i] = b ^ xor_key
    return bytes(out)


def _prepare_sng_extraction_folder(sng_path: Path, dest_dir: Path | None, overwrite: bool, debug: bool) -> Path:
    if dest_dir is None:
        dest_dir = Path(str(sng_path) + "_extracted")
    if dest_dir.exists() and not overwrite:
        if _is_extraction_cache_fresh(sng_path, dest_dir, require_chart_or_mid=True):
            dprint(debug, f"SNG: skipping extraction, cache is fresh: {dest_dir}")
            return dest_dir
        raise FileExistsError(f"The folder '{dest_dir}' already exists.")
    return dest_dir


def _extract_sng_files(data: bytes, xor_mask: bytes, file_metas: list[tuple[str, int, int]],
                       dest_dir: Path, debug: bool) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for fname, contents_len, contents_idx in file_metas:
        fname_safe = Path(fname).name
        if not fname_safe or fname_safe.startswith("."):
            dprint(debug, f"SNG: skipping unsafe filename '{fname}'")
            continue
        abs_end = contents_idx + contents_len
        if abs_end > len(data):
            dprint(debug, f"SNG: '{fname}' out of bounds, skipping")
            continue
        unmasked = _sng_unmask(data[contents_idx:abs_end], xor_mask)
        out_path = dest_dir / fname
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(unmasked)
        dprint(debug, f"SNG extracted '{fname}' ({contents_len:,} bytes)")


def _write_sng_song_ini(sng_path: Path, dest_dir: Path, metadata: dict[str, str], debug: bool) -> None:
    if (dest_dir / "song.ini").exists():
        return
    name = metadata.get("name") or sng_path.stem
    artist = metadata.get("artist") or "Unknown Artist"
    extra = {k: v for k, v in metadata.items() if k not in ("name", "artist", "delay")}
    _write_song_ini(dest_dir, name, artist, extra)
    dprint(debug, f"SNG wrote song.ini ({len(extra) + 3} lines)")


# --- Library orchestration ---

def _timed_extract(extract_fn, pkg_path, **kwargs):
    t0 = time.perf_counter()
    result = extract_fn(pkg_path, **kwargs)
    return result, time.perf_counter() - t0

def pre_extract_all(
    cons: list[Path],
    sngs: list[Path],
    overwrite: bool,
    debug: bool,
    workers: int,
    cancel_event: threading.Event,
    delete_cons: bool,
    delete_sngs: bool,
    dry_run: bool,
    dump_raw: bool
) -> dict[Path, Path]:
    """
    Pre-extract all CON/SNG files in parallel before calibration.
    Returns a dict mapping pkg_path -> extracted_folder / song.ini
    """
    
    if not cons and not sngs:
        return {}

    print(bold(f"\nExtracting {len(cons)} CON + {len(sngs)} SNG files..."))

    extracted: dict[Path, Path] = {}
    print_lock = threading.Lock()
    name_width = MAX_SONG_NAME
    executor = ThreadPoolExecutor(max_workers=workers)
    jobs = (
        [(p, "CON", con_extract_to_folder, delete_cons) for p in cons] +
        [(p, "SNG", sng_extract_to_folder, delete_sngs) for p in sngs]
    )

    pack_avg: dict[Path, float] = {}  # pkg_path -> EMA of per-song extraction time, for in-pack ETA

    def _print_song(pkg_path: Path, song_dest: Path, elapsed: float, index: int, total: int) -> None:
        display = f"{pkg_path.name} -> {song_dest.name}" if song_dest.name != pkg_path.name else pkg_path.name
        display = display[:name_width - 3] + "..." if len(display) > name_width else display
        padded = f"{display:<{name_width}}"
        audio_files = find_audio_candidates(song_dest, False)
        size_mb = sum(f.stat().st_size for f in audio_files) / (1024 * 1024)

        avg = pack_avg.get(pkg_path)
        avg = elapsed if avg is None else avg * (1 - ETA_EMA_ALPHA) + elapsed * ETA_EMA_ALPHA
        pack_avg[pkg_path] = avg

        remaining = total - index
        if remaining > 0:
            eta_sec = int(avg * remaining)
            eta_m, eta_s = divmod(eta_sec, 60)
            eta_h, eta_m = divmod(eta_m, 60)
            suffix = dim(f"  [ETA: {eta_h}h {eta_m:02}m {eta_s:02}s, {elapsed:.1f}s]")
        else:
            suffix = dim(f"  [{elapsed:.1f}s]")

        with print_lock:
            print(f"{green('ok  ')}  {padded}  song extracted  ({len(audio_files)} audio files, {size_mb:.1f} MB){suffix}")

    futures = {
        executor.submit(_timed_extract, extract_fn, pkg_path, dump_raw=dump_raw,
                        debug=debug, overwrite=overwrite,
                        cancel_event=cancel_event,
                        on_song_done=lambda song_dest, elapsed, index, total, p=pkg_path: _print_song(p, song_dest, elapsed, index, total)
                        ): (pkg_path, fmt_label, delete_file)
        for pkg_path, fmt_label, extract_fn, delete_file in jobs
    }

    try:
        total = len(futures)
        avg_time = -1.0

        for i, fut in enumerate(as_completed(futures), 1):
            pkg_path, fmt_label, delete_file = futures[fut]
            display = pkg_path.name[:name_width - 3] + "..." if len(pkg_path.name) > name_width else pkg_path.name
            padded = f"{display:<{name_width}}"
            try:
                song_folder, elapsed = fut.result()
                avg_time = elapsed if avg_time < 0 else avg_time * (1 - ETA_EMA_ALPHA) + elapsed * ETA_EMA_ALPHA

                remaining = total - i
                eta_sec = int(avg_time * remaining / workers) if avg_time > 0 and remaining > 0 else 0
                eta_m, eta_s = divmod(eta_sec, 60)
                eta_h, eta_m = divmod(eta_m, 60)

                # Multi-song CON packs land in subfolders and already printed one
                # line per song via the on_song_done callback as they finished.
                is_multi_song = (
                    fmt_label == "CON" and song_folder.is_dir()
                    and not (song_folder / "song.ini").exists()
                    and any(d.is_dir() and (d / "song.ini").exists() for d in song_folder.iterdir())
                )

                if is_multi_song:
                    song_subfolders = [d for d in song_folder.iterdir() if d.is_dir() and (d / "song.ini").exists()]
                    nb_songs_extracted = len(song_subfolders)
                    eta_str = dim(f"  [{avg_time / nb_songs_extracted:.1f}s/song]")
                    with print_lock:
                        print(dim(f"      {pkg_path.name}: {nb_songs_extracted} songs extracted{eta_str}  (done {i}/{total})"))
                    extracted[pkg_path] = song_subfolders[0] / "song.ini"
                else:
                    eta_str = dim(f"  [{eta_h}h{eta_m}m{eta_s:02}s left, {avg_time:.1f}s/file]")
                    audio_files = find_audio_candidates(song_folder, False)
                    size_mb = sum(f.stat().st_size for f in audio_files) / (1024 * 1024)
                    with print_lock:
                        print(f"{green('ok  ')}  {padded}  {fmt_label} extracted  ({len(audio_files)} audio files, {size_mb:.1f} MB){eta_str}  (done {i}/{total})")
                    extracted[pkg_path] = song_folder / "song.ini"

                if delete_file and not dry_run:
                    try:
                        pkg_path.unlink()
                    except Exception as e:
                        dprint(debug, f"could not delete {fmt_label}: {e}")

            except (KeyboardInterrupt, InterruptedError):
                raise
            except Exception as exc:
                print(f"{red('skip')}  {padded}  {fmt_label} extraction failed: {exc}  (done {i}/{total})")

    except (KeyboardInterrupt, InterruptedError):
        print("\n[!] Extraction interrupted.")
        cancel_event.set()
        executor.shutdown(wait=False, cancel_futures=True)
        for fut, (pkg_path, fmt_label, delete_file) in futures.items():
            dest_dir = Path(str(pkg_path) + "_extracted")
            if dest_dir.exists() and not _is_extraction_cache_fresh(pkg_path, dest_dir):
                _cleanup_interrupted_extraction(dest_dir)
        raise
    else:
        executor.shutdown(wait=True)

    return extracted


def _cleanup_interrupted_extraction(dest_dir: Path) -> None:
    """On interrupt, only remove unfinished songs. Completion is marked by
    .extraction_complete, written only after assets/stems are fully done —
    song.ini alone isn't proof, since it's written before the stems."""
    if (dest_dir / ".extraction_complete").exists():
        return  # single-song, already complete

    subfolders = [d for d in dest_dir.iterdir() if d.is_dir()]
    if not subfolders:
        shutil.rmtree(dest_dir)  # single-song, still incomplete
        return

    for d in subfolders:  # multi-song: remove only unfinished songs
        if not (d / ".extraction_complete").exists():
            shutil.rmtree(d)


def process_library(
    root: Path,
    dry_run: bool,
    debug: bool,
    workers: int = WORKER_THREADS,
    delete_cons: bool = False,
    delete_sngs: bool = False,
    overwrite: bool = False,
    skip_extracted: bool = False,
    dump_raw: bool = False
) -> None:
    print()
    t0 = time.perf_counter()
    if delete_cons and not dry_run:
        print(red(bold("  WARNING: --delete-cons is set. CON files will be permanently deleted after extraction.")))
        print("  Press Ctrl+C to abort...\n")

    print("Scanning songs...")
    # First scan — full, for display only
    all_songs, _, _, _, _, cons, sngs = get_song_list(
        root, skip_extracted=skip_extracted, debug=debug
    )
    print(f"Scan took {time.perf_counter() - t0:.0f} seconds.")
    if not all_songs:
        print("No songs found.")
        return

    cancel_event = threading.Event()
    _ = pre_extract_all(cons, sngs, overwrite, debug, workers,
                                        cancel_event, delete_cons, delete_sngs, dry_run, dump_raw)
    print(f"Extracted {len(cons) + len(sngs)} songs in {time.perf_counter() - t0:.0f} seconds.")

# --- DTA metadata parsing (Rock Band songs.dta) ---


# --- Entry point ---

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
    parser.add_argument("--workers",                 type=int, default=WORKER_THREADS, help=f"Number of parallel worker threads (default: {WORKER_THREADS})")
    parser.add_argument("--delete-cons",             action="store_true", help="Delete CON files that are extracted. Destructive.")
    parser.add_argument("--delete-sngs",             action="store_true", help="Delete SNG files that are extracted. Destructive.")
    parser.add_argument("--overwrite",               action="store_true", help="Overwrite existing folder during CON and SNG extraction.")
    parser.add_argument("--skip-extracted",          action="store_true", help="Skip all song folders that were extracted from CON or SNG files.")
    parser.add_argument("--dump-raw",                action="store_true", help="Dump all the raw files found in CON or SNG packages.")
    parser.add_argument("--shutdown-after",          action="store_true", help="Shutdown system after calibration, useful to run while sleeping.")
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
            workers=args.workers,
            delete_cons=args.delete_cons,
            delete_sngs=args.delete_sngs,
            overwrite=args.overwrite,
            skip_extracted=args.skip_extracted,
            dump_raw=args.dump_raw
        )
    except KeyboardInterrupt:
        interrupted = True

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
    main()

