"""Windows PC bridge for the ESP32 YouTube Music / media remote.

Requirements (recommended: 64-bit Python 3.11 or 3.12):
    py -m pip install winsdk pillow

Run:
    py pc_music_bridge.py

The ESP32 and PC must be on the same Wi-Fi/LAN. Allow Python or TCP port
8765 through Windows Firewall on a Private network when prompted.

HTTP API:
    GET /health   -> bridge health check
    GET /status   -> T|title|artist|position|duration|playing|art_id|current|next|previous
    GET /art.jpg  -> current 100x100 JPEG album art
    GET /cmd?c=PLAY|NEXT|PREV|VOLUP|VOLDN|SEEK:<seconds>
"""

from __future__ import annotations

import asyncio
import ctypes
import datetime
import io
import json
import re
import socket
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image, ImageOps
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions
except ImportError as exc:
    raise SystemExit(
        "Missing dependency or unsupported Python environment.\n"
        "Use 64-bit Python 3.11/3.12, then run:\n"
        "  py -m pip install winsdk pillow\n"
        f"Import error: {exc}"
    ) from exc


# ------------------------- Configuration -------------------------
PORT = 8765
ART_SIZE = 100
POLL_INTERVAL = 0.5
LYRICS_CACHE_LIMIT = 128
HTTP_USER_AGENT = "ESP32MusicRemote/2.0"

# Set True only if you deliberately want the PC to pause music when the
# ESP32 disconnects. Disabled by default to avoid unexpected playback changes.
PAUSE_ON_ESP_DISCONNECT = False
ESP_DISCONNECT_SECONDS = 12

# Windows virtual-key codes
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002


# ---------------------------- Shared state ----------------------------
STATE_LOCK = threading.Lock()
STATE = {
    "status": "T|Nothing playing||0|0|0|0|||\n",
    "art": None,
    "last_esp_seen": 0.0,
}
LOOP: asyncio.AbstractEventLoop | None = None
MANAGER = None
LYRICS_CACHE: OrderedDict[tuple[str, str], list[tuple[float, str]]] = OrderedDict()


def press_key(vk: int) -> None:
    """Send a media key through the Windows input API with extended key flag."""
    user32 = ctypes.windll.user32
    scan = user32.MapVirtualKeyW(vk, 0)
    user32.keybd_event(vk, scan, 0x0001, 0)
    time.sleep(0.01)
    user32.keybd_event(vk, scan, 0x0001 | 0x0002, 0)


def change_volume(up: bool) -> None:
    """Change Windows master volume via WM_APPCOMMAND and extended key event."""
    user32 = ctypes.windll.user32
    # 1. Send WM_APPCOMMAND broadcast (standard Windows shell volume)
    # APPCOMMAND_VOLUME_UP = 10, APPCOMMAND_VOLUME_DOWN = 9
    app_cmd = 10 if up else 9
    user32.SendMessageW(0xFFFF, 0x0319, 0, app_cmd << 16)
    # 2. Also send keybd_event with KEYEVENTF_EXTENDEDKEY (0x0001) as hardware fallback
    vk = VK_VOLUME_UP if up else VK_VOLUME_DOWN
    scan = user32.MapVirtualKeyW(vk, 0)
    user32.keybd_event(vk, scan, 0x0001, 0)
    user32.keybd_event(vk, scan, 0x0001 | 0x0002, 0)


# --------------- Comprehensive multi-script transliteration ---------------
# All non-Latin text must be romanized for the ILI9341 ASCII-only display.
# Pure Python, no external dependencies.

# -- Devanagari (U+0900–U+097F) --
_DEVA_MAP = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au", "अं": "an", "अः": "ah",
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f",
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ं": "n", "ँ": "n", "ः": "h",
    "्": "", "।": ".", "॥": ".",
}
_DEVA_CONSONANTS = set("कखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहक़ख़ग़ज़ड़ढ़फ़")
_DEVA_MATRAS = set("ािीुूृेैोौ्")

# -- Cyrillic (U+0400–U+04FF) --
_CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Ukrainian / Serbian extras
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ђ": "dj", "ј": "j",
    "љ": "lj", "њ": "nj", "ћ": "c", "џ": "dz",
}
# Build uppercase variants automatically
_CYRILLIC_MAP.update({k.upper(): v.capitalize() for k, v in _CYRILLIC_MAP.items() if v})
_CYRILLIC_MAP.update({k.upper(): "" for k, v in _CYRILLIC_MAP.items() if not v})

# -- Korean Hangul (U+AC00–U+D7AF) algorithmic decomposition --
_KR_INITIALS = [
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp",
    "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h",
]
_KR_MEDIALS = [
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o",
    "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu",
    "eu", "ui", "i",
]
_KR_FINALS = [
    "", "k", "k", "ks", "n", "nj", "nh", "t", "l",
    "lk", "lm", "lb", "ls", "lt", "lp", "lh", "m", "p",
    "ps", "t", "t", "ng", "t", "t", "k", "t", "p", "h",
]

# Standalone Jamo (U+3131–U+314E consonants, U+314F–U+3163 vowels)
_JAMO_MAP = {
    "\u3131": "g", "\u3132": "kk", "\u3133": "gs", "\u3134": "n", "\u3135": "nj",
    "\u3136": "nh", "\u3137": "d", "\u3138": "tt", "\u3139": "r", "\u313a": "lg",
    "\u313b": "lm", "\u313c": "lb", "\u313d": "ls", "\u313e": "lt", "\u313f": "lp",
    "\u3140": "lh", "\u3141": "m", "\u3142": "b", "\u3143": "pp", "\u3144": "bs",
    "\u3145": "s", "\u3146": "ss", "\u3147": "ng", "\u3148": "j", "\u3149": "jj",
    "\u314a": "ch", "\u314b": "k", "\u314c": "t", "\u314d": "p", "\u314e": "h",
    "\u314f": "a", "\u3150": "ae", "\u3151": "ya", "\u3152": "yae", "\u3153": "eo",
    "\u3154": "e", "\u3155": "yeo", "\u3156": "ye", "\u3157": "o", "\u3158": "wa",
    "\u3159": "wae", "\u315a": "oe", "\u315b": "yo", "\u315c": "u", "\u315d": "wo",
    "\u315e": "we", "\u315f": "wi", "\u3160": "yu", "\u3161": "eu", "\u3162": "ui",
    "\u3163": "i",
}

# -- Japanese Hiragana (U+3040–U+309F) & Katakana (U+30A0–U+30FF) --
_HIRAGANA_MAP = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "ゐ": "wi", "ゑ": "we", "を": "wo", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "di", "づ": "du", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    # Small kana (used in digraphs)
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ゎ": "wa",
}
# Small-kana codepoints used for digraph detection (hiragana)
_SMALL_KANA_H = {"\u3083", "\u3085", "\u3087"}  # ゃゅょ

# Build katakana map by shifting hiragana by 0x60
_KATAKANA_MAP = {}
for _hk, _hv in _HIRAGANA_MAP.items():
    _kk = chr(ord(_hk) + 0x60)
    _KATAKANA_MAP[_kk] = _hv
# Small katakana for digraphs
_SMALL_KANA_K = {chr(ord(c) + 0x60) for c in _SMALL_KANA_H}  # ャュョ

# Consonant base for digraph construction: the consonant of the preceding kana
# e.g. き(ki) + ゃ(ya) => kya. We strip the trailing vowel from the base.
def _kana_consonant(romaji: str) -> str:
    """Return consonant portion of a kana romaji for digraph merging."""
    # shi -> sh, chi -> ch, tsu -> ts, etc.
    for end in ("i", "u", "e", "o", "a"):
        if romaji.endswith(end) and len(romaji) > len(end):
            return romaji[: -len(end)]
    return romaji

# -- Thai (U+0E00–U+0E7F) --
_THAI_MAP = {
    "ก": "k", "ข": "kh", "ฃ": "kh", "ค": "kh", "ฅ": "kh", "ฆ": "kh",
    "ง": "ng", "จ": "ch", "ฉ": "ch", "ช": "ch", "ซ": "s", "ฌ": "ch",
    "ญ": "y", "ฎ": "d", "ฏ": "t", "ฐ": "th", "ฑ": "th", "ฒ": "th",
    "ณ": "n", "ด": "d", "ต": "t", "ถ": "th", "ท": "th", "ธ": "th",
    "น": "n", "บ": "b", "ป": "p", "ผ": "ph", "ฝ": "f", "พ": "ph",
    "ฟ": "f", "ภ": "ph", "ม": "m", "ย": "y", "ร": "r", "ล": "l",
    "ว": "w", "ศ": "s", "ษ": "s", "ส": "s", "ห": "h", "ฬ": "l",
    "อ": "o", "ฮ": "h",
    # Vowels and marks
    "ะ": "a", "ั": "a", "า": "a", "ำ": "am", "ิ": "i", "ี": "i",
    "ึ": "ue", "ื": "ue", "ุ": "u", "ู": "u", "เ": "e", "แ": "ae",
    "โ": "o", "ใ": "ai", "ไ": "ai",
    # Tone marks and misc (silent in romanization)
    "่": "", "้": "", "๊": "", "๋": "", "์": "", "ฺ": "",
    "ๆ": "", "ฯ": ".",
    # Thai digits
    "๐": "0", "๑": "1", "๒": "2", "๓": "3", "๔": "4",
    "๕": "5", "๖": "6", "๗": "7", "๘": "8", "๙": "9",
}

# -- Arabic (U+0600–U+06FF) --
_ARABIC_MAP = {
    "ا": "a", "أ": "a", "إ": "i", "آ": "aa", "ب": "b", "ت": "t", "ث": "th",
    "ج": "j", "ح": "h", "خ": "kh", "د": "d", "ذ": "dh", "ر": "r", "ز": "z",
    "س": "s", "ش": "sh", "ص": "s", "ض": "d", "ط": "t", "ظ": "z",
    "ع": "a", "غ": "gh", "ف": "f", "ق": "q", "ك": "k", "ل": "l",
    "م": "m", "ن": "n", "ه": "h", "و": "w", "ي": "y", "ى": "a",
    "ة": "h", "ء": "'",
    # Common vowel diacritics
    "\u064e": "a",  # fatha
    "\u064f": "u",  # damma
    "\u0650": "i",  # kasra
    "\u0651": "",   # shadda (gemination, contextual)
    "\u0652": "",   # sukun
    "\u064b": "an", # fathatan
    "\u064c": "un", # dammatan
    "\u064d": "in", # kasratan
    # Hamza forms
    "ؤ": "u", "ئ": "i",
    # Alef variants
    "ٱ": "a",
    # Arabic-Indic digits
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    # Punctuation
    "،": ",", "؛": ";", "؟": "?",
}


def transliterate_to_roman(text: str) -> str:
    """Transliterate non-Latin text to romanized ASCII.

    Handles Korean Hangul, Japanese Hiragana/Katakana, Cyrillic, Thai,
    Arabic, and Devanagari.  Characters already in ASCII are passed through.
    CJK ideographs (Chinese) are left as-is for the NFKD+ASCII fallback in
    clean() to strip, since a full pinyin dictionary is impractical here.
    """
    # Fast path: if entirely ASCII, skip processing
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        pass

    result: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        c = text[i]
        cp = ord(c)

        # --- ASCII passthrough ---
        if cp < 0x80:
            result.append(c)
            i += 1
            continue

        # --- Korean Hangul syllable (U+AC00–U+D7AF) ---
        if 0xAC00 <= cp <= 0xD7AF:
            si = cp - 0xAC00
            initial = si // (21 * 28)
            medial = (si % (21 * 28)) // 28
            final = si % 28
            result.append(_KR_INITIALS[initial])
            result.append(_KR_MEDIALS[medial])
            result.append(_KR_FINALS[final])
            i += 1
            continue

        # --- Korean standalone Jamo (U+3131–U+318E) ---
        if 0x3131 <= cp <= 0x318E and c in _JAMO_MAP:
            result.append(_JAMO_MAP[c])
            i += 1
            continue

        # --- Japanese Hiragana (U+3040–U+309F) ---
        if 0x3040 <= cp <= 0x309F:
            # Small tsu (っ) doubles next consonant
            if c == "\u3063":
                # Peek ahead for next kana's consonant
                if i + 1 < n:
                    nxt = text[i + 1]
                    rom = _HIRAGANA_MAP.get(nxt, "")
                    consonant = _kana_consonant(rom) if rom else ""
                    if consonant:
                        result.append(consonant[0])
                    # else: skip sokuon before non-kana
                i += 1
                continue
            rom = _HIRAGANA_MAP.get(c, "")
            if rom:
                # Digraph check: if next char is small ゃゅょ
                if i + 1 < n and text[i + 1] in _SMALL_KANA_H:
                    cons = _kana_consonant(rom)
                    small_rom = _HIRAGANA_MAP.get(text[i + 1], "")
                    result.append(cons + small_rom)
                    i += 2
                    continue
                result.append(rom)
            else:
                result.append(c)
            i += 1
            continue

        # --- Japanese Katakana (U+30A0–U+30FF) ---
        if 0x30A0 <= cp <= 0x30FF:
            # Small tsu (ッ)
            if c == "\u30C3":
                if i + 1 < n:
                    nxt = text[i + 1]
                    rom = _KATAKANA_MAP.get(nxt, "")
                    consonant = _kana_consonant(rom) if rom else ""
                    if consonant:
                        result.append(consonant[0])
                i += 1
                continue
            # Long vowel mark (ー) repeats previous vowel
            if c == "\u30FC":
                if result:
                    prev = result[-1]
                    for vc in reversed(prev):
                        if vc in "aeiou":
                            result.append(vc)
                            break
                i += 1
                continue
            rom = _KATAKANA_MAP.get(c, "")
            if rom:
                # Digraph check with small katakana ャュョ
                if i + 1 < n and text[i + 1] in _SMALL_KANA_K:
                    cons = _kana_consonant(rom)
                    small_rom = _KATAKANA_MAP.get(text[i + 1], "")
                    result.append(cons + small_rom)
                    i += 2
                    continue
                result.append(rom)
            else:
                result.append(c)
            i += 1
            continue

        # --- Cyrillic (U+0400–U+04FF) ---
        if 0x0400 <= cp <= 0x04FF:
            result.append(_CYRILLIC_MAP.get(c, c))
            i += 1
            continue

        # --- Thai (U+0E00–U+0E7F) ---
        if 0x0E00 <= cp <= 0x0E7F:
            result.append(_THAI_MAP.get(c, c))
            i += 1
            continue

        # --- Arabic (U+0600–U+06FF) ---
        if 0x0600 <= cp <= 0x06FF:
            result.append(_ARABIC_MAP.get(c, c))
            i += 1
            continue

        # --- Devanagari (U+0900–U+097F) ---
        if 0x0900 <= cp <= 0x097F:
            ch = c
            # Nukta combination (e.g. क + ़ = क़)
            if i + 1 < n and text[i + 1] == "\u093C":
                ch = text[i : i + 2]
                i += 1
            if ch in _DEVA_MAP:
                val = _DEVA_MAP[ch]
                if ch in _DEVA_CONSONANTS:
                    next_c = text[i + 1] if i + 1 < n else ""
                    if next_c not in _DEVA_MATRAS and next_c != "\u094D":
                        val += "a"
                result.append(val)
            else:
                result.append(ch)
            i += 1
            continue

        # --- Fallback: pass character through for NFKD stripping later ---
        result.append(c)
        i += 1

    return "".join(result)


def clean(text: str | None, maxlen: int = 60) -> str:
    """Convert text to safe single-line ASCII for the ESP32 protocol/display."""
    if not text:
        return ""
    # Transliterate any non-ASCII text (Korean, Japanese, Cyrillic, etc.)
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        text = transliterate_to_roman(text)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.replace("|", " ").replace("\r", " ").replace("\n", " ")
    text = " ".join(text.split())
    return text[:maxlen]


def parse_track_info(raw_title: str, raw_artist: str) -> tuple[str, str]:
    """Extract clean song title and artist from YouTube/browser/player titles."""
    title = raw_title or ""
    artist = raw_artist or ""

    # Common generic player/browser labels that aren't the real musical artist
    generic_artists = {
        "youtube", "youtube music", "google chrome", "brave", "spotify",
        "music", "msedge", "firefox", "unknown artist", "unknown", ""
    }
    if artist.strip().lower() in generic_artists:
        artist = ""

    # Remove VEVO and Topic suffixes from artist channel names
    artist = re.sub(r"\s*-\s*topic\b", "", artist, flags=re.IGNORECASE)
    artist = re.sub(r"vevo\b", "", artist, flags=re.IGNORECASE).strip()

    # Strip bracketed video tags: (Official Video), [Lyric Video], (Live at ...), etc.
    bracket_junk = [
        r"\((?:official|music|video|audio|lyric|lyrics|visualizer|remastered|hd|4k|live)[^\)]*\)",
        r"\[(?:official|music|video|audio|lyric|lyrics|visualizer|remastered|hd|4k|live)[^\]]*\]",
        r"\{(?:official|music|video|audio|lyric|lyrics|visualizer|remastered|hd|4k|live)[^\}]*\}",
        r"\b(?:official\s+video|official\s+music\s+video|official\s+audio|lyric\s+video|visualizer|remastered\s+\d{4}|4k|hd)\b",
    ]
    for pat in bracket_junk:
        title = re.sub(pat, "", title, flags=re.IGNORECASE)

    # Clean (feat. ...) inside parentheses
    title = re.sub(r"\s*[\(\[\{]\s*(?:feat|ft)\.?\s+[^\)\]\}]+[\)\]\}]", "", title, flags=re.IGNORECASE)

    # Check for ' | ' separator (e.g. "Song | Movie | Artist" on YouTube)
    if "|" in title:
        parts = [p.strip() for p in title.split("|") if p.strip()]
        if parts:
            title = parts[0]
            if not artist and len(parts) > 1:
                artist = parts[-1]

    # Check for ' - ' separator (e.g. "Artist - Title" or "Title - Artist")
    if " - " in title:
        parts = [p.strip() for p in title.split(" - ") if p.strip()]
        if len(parts) == 2:
            p0, p1 = parts[0], parts[1]
            if artist:
                p0_norm = re.sub(r"[^a-z0-9]", "", p0.lower())
                p1_norm = re.sub(r"[^a-z0-9]", "", p1.lower())
                art_norm = re.sub(r"[^a-z0-9]", "", artist.lower())
                if art_norm and (art_norm in p0_norm or p0_norm in art_norm):
                    title = p1
                elif art_norm and (art_norm in p1_norm or p1_norm in art_norm):
                    title = p0
            else:
                artist, title = p0, p1

    # Clean remaining standalone 'ft. Artist' or 'feat. Artist'
    title = re.sub(r"\b(?:feat|ft)\.?\s+[A-Za-z0-9\s]+(?=[-\(\[\{]|$)", "", title, flags=re.IGNORECASE)

    # Clean non-alphanumeric except basic punctuation
    clean_t = re.sub(r"[^\w\s\'-]", " ", title)
    clean_t = " ".join(clean_t.split()).strip()

    clean_a = re.sub(r"[^\w\s\'-]", " ", artist)
    clean_a = " ".join(clean_a.split()).strip()

    return clean_t or raw_title, clean_a or raw_artist


def clean_for_search(text: str) -> str:
    text = re.sub(r"[\(\[\{].*?[\)\]\}]", "", text or "")
    text = re.sub(r"\b(?:feat|ft)\.?\s+\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    return " ".join(text.split())


def parse_synced_lyrics(lrc_text: str) -> list[tuple[float, str]]:
    lines: list[tuple[float, str]] = []
    for line in lrc_text.splitlines():
        line = line.strip()
        # Support timestamps like [01:23.45] or [01:23.456] or multiple tags [00:10.00][00:20.00]
        matches = list(re.finditer(r"\[(\d+):(\d+(?:\.\d+)?)\]", line))
        if not matches:
            continue
        text_part = line[matches[-1].end():].strip()
        # Ignore metadata tags like [ar: ...], [ti: ...]
        if text_part and not (text_part.startswith("[") and text_part.endswith("]")):
            for m in matches:
                timestamp = int(m.group(1)) * 60 + float(m.group(2))
                lines.append((timestamp, text_part))
    lines.sort(key=lambda item: item[0])
    return lines


def parse_plain_lyrics(plain_text: str, duration: int) -> list[tuple[float, str]]:
    raw_lines = [line.strip() for line in plain_text.splitlines() if line.strip()]
    if not raw_lines:
        return []
    effective_duration = max(duration, len(raw_lines) * 3)
    interval = effective_duration / len(raw_lines)
    return [(index * interval, line) for index, line in enumerate(raw_lines)]


async def fetch_lyrics(title: str, artist: str, duration: int) -> list[tuple[float, str]]:
    key = (title, artist)
    if key in LYRICS_CACHE:
        LYRICS_CACHE.move_to_end(key)
        return LYRICS_CACHE[key]

    clean_title, clean_artist = parse_track_info(title, artist)
    headers = {"User-Agent": HTTP_USER_AGENT}

    def request_lyrics() -> list[tuple[float, str]]:
        deva_synced_candidate = None
        plain_candidates: list[tuple[int, list[tuple[float, str]]]] = []

        # 1. Exact LRCLIB endpoint if artist is known
        if clean_artist:
            try:
                params = {"track_name": clean_title, "artist_name": clean_artist}
                if duration > 0:
                    params["duration"] = str(duration)
                url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(params)
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=3.5) as response:
                    data = json.loads(response.read().decode("utf-8"))
                if data.get("syncedLyrics"):
                    syn_raw = data["syncedLyrics"]
                    synced = parse_synced_lyrics(syn_raw)
                    # Reject dummy / corrupted single-line entries like '[00:00.00]probe'
                    if len(synced) >= 2 or (len(synced) == 1 and duration <= 20):
                        ascii_count = sum(1 for c in syn_raw if "a" <= c.lower() <= "z")
                        if ascii_count >= 20:
                            return synced
                        deva_synced_candidate = synced
                if data.get("plainLyrics"):
                    plain = parse_plain_lyrics(data["plainLyrics"], duration)
                    if len(plain) >= 2:
                        plain_candidates.append((0, plain))
            except Exception:
                pass

        # 2. Search fallback with prioritized queries
        queries: list[tuple[str, dict[str, str]]] = []
        if clean_artist:
            queries.append(("search_params", {"track_name": clean_title, "artist_name": clean_artist}))
            queries.append(("query", {"q": f"{clean_artist} {clean_title}".strip()}))
        queries.append(("query", {"q": clean_title.strip()}))

        for q_type, q_params in queries:
            try:
                url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(q_params)
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=3.5) as response:
                    results = json.loads(response.read().decode("utf-8"))
                if isinstance(results, list) and results:
                    # Look for items with synced lyrics first
                    synced_items = [
                        item for item in results
                        if item.get("syncedLyrics")
                    ]
                    if synced_items:
                        # Prefer items with Roman/Latin alphabet and closest duration
                        def score_synced(item):
                            syn = item.get("syncedLyrics") or ""
                            dur = item.get("duration") or 0
                            dur_diff = abs(dur - duration) if duration > 0 else 0
                            ascii_count = sum(1 for c in syn if "a" <= c.lower() <= "z")
                            latin_penalty = 0 if ascii_count >= 20 else 1000
                            return (latin_penalty, dur_diff, -len(syn.splitlines()))

                        synced_items.sort(key=score_synced)
                        for item in synced_items:
                            parsed = parse_synced_lyrics(item["syncedLyrics"])
                            if len(parsed) >= 2:
                                return parsed

                    # Collect plain lyrics candidates as fallback
                    plain_items = [item for item in results if item.get("plainLyrics")]
                    if plain_items:
                        if duration > 0:
                            plain_items.sort(key=lambda x: abs((x.get("duration") or 0) - duration))
                        for item in plain_items:
                            parsed = parse_plain_lyrics(item["plainLyrics"], duration)
                            if len(parsed) >= 2:
                                plain_candidates.append((abs((item.get("duration") or 0) - duration), parsed))
            except Exception:
                pass

        if deva_synced_candidate:
            return deva_synced_candidate

        if plain_candidates:
            plain_candidates.sort(key=lambda x: x[0])
            return plain_candidates[0][1]

        return []

    loop = asyncio.get_running_loop()
    lyrics = await loop.run_in_executor(None, request_lyrics)
    LYRICS_CACHE[key] = lyrics
    LYRICS_CACHE.move_to_end(key)
    while len(LYRICS_CACHE) > LYRICS_CACHE_LIMIT:
        LYRICS_CACHE.popitem(last=False)

    if lyrics:
        print(f"[LYRICS] Loaded {len(lyrics)} lines for: '{clean_title}' by '{clean_artist or 'Unknown'}'")
    else:
        print(f"[LYRICS] No lyrics found for: '{clean_title}' by '{clean_artist or 'Unknown'}'")
    return lyrics


def get_lyrics_at_pos(
    lyrics: list[tuple[float, str]], position: float
) -> tuple[str, str, str]:
    if not lyrics:
        return "", "", ""
    index = -1
    for i, (timestamp, _text) in enumerate(lyrics):
        if timestamp <= position:
            index = i
        else:
            break

    def line_at(i: int) -> str:
        return clean(lyrics[i][1], maxlen=60) if 0 <= i < len(lyrics) else ""

    current = line_at(index)
    following = line_at(index + 1)
    previous = line_at(index - 1) if index > 0 else ""

    # If in a long instrumental break (> 10s gap), clear current after 7s to show ~ ... ~
    if index >= 0 and index + 1 < len(lyrics):
        next_ts = lyrics[index + 1][0]
        cur_ts = lyrics[index][0]
        if next_ts - cur_ts > 10.0 and position - cur_ts > 7.0:
            previous = current
            current = ""

    return previous, current, following


def get_active_session():
    """Find the best active media session, preferring one that is currently PLAYING."""
    if not MANAGER:
        return None
    try:
        cur = MANAGER.get_current_session()
        if cur:
            pb = cur.get_playback_info()
            if pb and pb.playback_status == PlaybackStatus.PLAYING:
                return cur
        sessions = MANAGER.get_sessions()
        for s in sessions:
            pb = s.get_playback_info()
            if pb and pb.playback_status == PlaybackStatus.PLAYING:
                return s
        return cur
    except Exception:
        return MANAGER.get_current_session() if MANAGER else None


async def handle_command(command: str) -> tuple[bool, str]:
    """Execute a validated command on the Windows media session."""
    session = get_active_session()
    try:
        if command == "VOLUP":
            change_volume(True)
        elif command == "VOLDN":
            change_volume(False)
        elif command == "PLAY":
            if session:
                await session.try_toggle_play_pause_async()
            else:
                press_key(VK_MEDIA_PLAY_PAUSE)
        elif command == "NEXT":
            if session:
                await session.try_skip_next_async()
            else:
                press_key(VK_MEDIA_NEXT_TRACK)
        elif command == "PREV":
            if session:
                await session.try_skip_previous_async()
            else:
                press_key(VK_MEDIA_PREV_TRACK)
        elif command.startswith("SEEK:"):
            if not session:
                return False, "No active media session"
            seconds = int(command[5:])
            if not 0 <= seconds <= 36000:
                return False, "Seek value out of range"
            await session.try_change_playback_position_async(seconds * 10_000_000)
        else:
            return False, "Unknown command"

        print(f"[CMD] {command}")
        return True, "OK"
    except Exception as exc:
        print(f"[CMD] Failed ({command}): {exc}")
        return False, str(exc)


async def fetch_thumbnail_jpeg(props) -> bytes | None:
    """Read Windows session thumbnail and convert it to a small baseline JPEG."""
    try:
        thumbnail = props.thumbnail
        if thumbnail is None:
            return None
        stream = await thumbnail.open_read_async()
        capacity = int(stream.size)
        if capacity <= 0 or capacity > 20_000_000:
            return None
        buffer = Buffer(capacity)
        await stream.read_async(buffer, buffer.capacity, InputStreamOptions.READ_AHEAD)
        reader = DataReader.from_buffer(buffer)
        raw = bytes(bytearray(reader.read_buffer(buffer.length)))
        if not raw:
            return None

        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.fit(source.convert("RGB"), (ART_SIZE, ART_SIZE))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85, progressive=False, optimize=True)
        return output.getvalue()
    except Exception as exc:
        print(f"[ART] Thumbnail error: {exc}")
        return None


async def update_loop() -> None:
    """Refresh current media metadata, progress, lyrics and album art."""
    last_track_key = None
    current_lyrics: list[tuple[float, str]] = []
    lyrics_task: asyncio.Task | None = None
    art_counter = 0
    art_id = 0
    art_retry_after = 0.0
    last_esp_disconnect_action = 0.0
    track_changed_time = 0.0
    current_jpeg_bytes: bytes | None = None

    tracked_pos = 0.0
    last_raw_pos = -1.0
    last_mono = time.monotonic()

    while True:
        now = time.monotonic()
        with STATE_LOCK:
            last_esp_seen = STATE["last_esp_seen"]

        if (
            PAUSE_ON_ESP_DISCONNECT
            and last_esp_seen > 0
            and time.time() - last_esp_seen > ESP_DISCONNECT_SECONDS
            and now - last_esp_disconnect_action > ESP_DISCONNECT_SECONDS
        ):
            last_esp_disconnect_action = now
            session = get_active_session()
            try:
                if session:
                    playback = session.get_playback_info()
                    if playback and playback.playback_status == PlaybackStatus.PLAYING:
                        await session.try_pause_async()
            except Exception as exc:
                print(f"[MEDIA] Could not pause after ESP disconnect: {exc}")

        status = "T|Nothing playing||0|0|0|0|||\n"
        try:
            session = get_active_session()
            if session is not None:
                props = await session.try_get_media_properties_async()
                title = clean(props.title) or "Unknown title"
                artist = clean(props.artist)

                playback = session.get_playback_info()
                is_playing = int(
                    bool(playback and playback.playback_status == PlaybackStatus.PLAYING)
                )
                playback_rate = (
                    playback.playback_rate
                    if (playback and playback.playback_rate)
                    else 1.0
                )

                raw_pos = 0.0
                duration = 0
                lut = None
                try:
                    timeline = session.get_timeline_properties()
                    if timeline:
                        raw_pos = max(0.0, timeline.position.total_seconds())
                        lut = timeline.last_updated_time
                        duration = max(
                            0, int((timeline.end_time - timeline.start_time).total_seconds())
                        )
                        if duration > 36000:
                            duration = 0
                except Exception:
                    pass

                now_dt = datetime.datetime.now(datetime.timezone.utc)
                now_mono = now

                if is_playing:
                    # Windows GSMTC provides position snapshot as of last_updated_time.
                    # When playing, extrapolate true current position:
                    if lut and lut.year >= 2020:
                        now_check = now_dt if lut.tzinfo is not None else datetime.datetime.utcnow()
                        elapsed = (now_check - lut).total_seconds()
                        if 0.0 <= elapsed < 86400:
                            pos_float = raw_pos + elapsed * playback_rate
                        else:
                            pos_float = raw_pos
                    else:
                        # Fallback for players that do not report last_updated_time
                        if abs(raw_pos - last_raw_pos) > 1.5:
                            tracked_pos = raw_pos
                        else:
                            tracked_pos += (now_mono - last_mono) * playback_rate
                        pos_float = tracked_pos
                else:
                    pos_float = raw_pos
                    tracked_pos = raw_pos

                last_raw_pos = raw_pos
                last_mono = now_mono

                if duration > 0 and pos_float > duration:
                    pos_float = float(duration)
                pos_float = max(0.0, pos_float)
                position = int(pos_float)

                track_key = (title, artist)
                if track_key != last_track_key:
                    last_track_key = track_key
                    current_lyrics = []
                    track_changed_time = now
                    art_retry_after = 0.0
                    tracked_pos = 0.0
                    if lyrics_task and not lyrics_task.done():
                        lyrics_task.cancel()
                    lyrics_task = asyncio.create_task(fetch_lyrics(title, artist, duration))
                    app_id = getattr(session, "source_app_user_model_id", "") or "Media Player"
                    print(f"[MEDIA] ({app_id}) Track: {title} — {artist or 'Unknown artist'}")

                if lyrics_task and lyrics_task.done() and not current_lyrics:
                    try:
                        current_lyrics = lyrics_task.result() or []
                    except asyncio.CancelledError:
                        pass
                    except Exception as exc:
                        print(f"[LYRICS] Task error: {exc}")

                # Check album art: retry actively during the first 3.5s of track change to catch late artwork
                if (art_id == 0 or now < track_changed_time + 3.5) and now >= art_retry_after:
                    jpeg = await fetch_thumbnail_jpeg(props)
                    if jpeg:
                        if jpeg != current_jpeg_bytes or art_id == 0:
                            current_jpeg_bytes = jpeg
                            art_counter += 1
                            art_id = art_counter
                            with STATE_LOCK:
                                STATE["art"] = jpeg
                            print(f"[ART] Updated ({len(jpeg)} bytes, id={art_id})")
                    else:
                        if art_id == 0:
                            with STATE_LOCK:
                                STATE["art"] = None
                        art_retry_after = now + 1.5

                previous, current, following = get_lyrics_at_pos(current_lyrics, pos_float)
                status = (
                    f"T|{title}|{artist}|{position}|{duration}|{is_playing}|{art_id}|"
                    f"{current}|{following}|{previous}\n"
                )
            else:
                if last_track_key is not None:
                    print("[MEDIA] No active Windows media session")
                last_track_key = None
                current_lyrics = []
                art_id = 0
                with STATE_LOCK:
                    STATE["art"] = None
        except Exception as exc:
            print(f"[MEDIA] Status update error: {exc}")

        with STATE_LOCK:
            STATE["status"] = status
        await asyncio.sleep(POLL_INTERVAL)


# ------------------------------ HTTP server ------------------------------
class RemoteHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    server_version = "ESP32MusicBridge/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        # Keep the console readable; meaningful events are logged explicitly.
        return

    def send_bytes(self, code: int, body: bytes, content_type: str = "text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("Keep-Alive", "timeout=30, max=1000")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        parsed = urlparse(self.path)

        with STATE_LOCK:
            STATE["last_esp_seen"] = time.time()

        if parsed.path == "/health":
            self.send_bytes(200, b"OK\n")
            return

        if parsed.path == "/status":
            with STATE_LOCK:
                body = STATE["status"].encode("ascii", "replace")
            self.send_bytes(200, body)
            return

        if parsed.path == "/art.jpg":
            with STATE_LOCK:
                art = STATE["art"]
            if art:
                self.send_bytes(200, art, "image/jpeg")
            else:
                self.send_bytes(404, b"No album art yet\n")
            return

        if parsed.path == "/cmd":
            command = parse_qs(parsed.query).get("c", [""])[0].strip().upper()
            allowed = {"PLAY", "NEXT", "PREV", "VOLUP", "VOLDN"}
            is_seek = command.startswith("SEEK:") and command[5:].isdigit()
            if command not in allowed and not is_seek:
                self.send_bytes(400, b"Invalid command\n")
                return
            if LOOP is None:
                self.send_bytes(503, b"Media loop is not ready\n")
                return
            try:
                future = asyncio.run_coroutine_threadsafe(handle_command(command), LOOP)
                ok, message = future.result(timeout=4.0)
                self.send_bytes(200 if ok else 409, (message + "\n").encode("utf-8"))
            except Exception as exc:
                print(f"[HTTP] Command request failed: {exc}")
                self.send_bytes(504, b"Command timed out or failed\n")
            return

        self.send_bytes(404, b"Not found\n")


def get_local_ipv4_addresses() -> list[str]:
    """Return likely LAN addresses, prioritizing the route used for internet traffic."""
    addresses: list[str] = []

    # UDP connect selects the interface Windows would use for an outbound route.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        candidate = probe.getsockname()[0]
        probe.close()
        if candidate and not candidate.startswith("127."):
            addresses.append(candidate)
    except OSError:
        pass

    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            candidate = result[4][0]
            if candidate and not candidate.startswith("127.") and candidate not in addresses:
                addresses.append(candidate)
    except OSError:
        pass

    return addresses


async def main() -> None:
    global LOOP, MANAGER
    LOOP = asyncio.get_running_loop()

    print("[START] Initializing Windows media-session access...")
    MANAGER = await MediaManager.request_async()

    server = RemoteHTTPServer(("0.0.0.0", PORT), Handler)
    server_thread = threading.Thread(
        target=server.serve_forever, name="ESP32-HTTP", daemon=True
    )
    server_thread.start()

    addresses = get_local_ipv4_addresses()
    print("\n" + "=" * 58)
    print(" ESP32 YouTube Music Remote — PC bridge is running")
    print("=" * 58)
    print(f"Port: {PORT}")
    print("Use one of these PC IPv4 addresses in the ESP32 sketch:")
    if addresses:
        for index, address in enumerate(addresses):
            marker = "  <-- recommended" if index == 0 else ""
            print(f"  {address}{marker}")
        print(f"\nTest from a phone on the same Wi-Fi: http://{addresses[0]}:{PORT}/health")
    else:
        print("  No LAN IPv4 address detected. Check that the PC is connected to Wi-Fi.")
    print("\nAllow Python through Windows Firewall on Private networks.")
    print("Press Ctrl+C to stop the bridge.")
    print("=" * 58 + "\n")

    try:
        await update_loop()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[STOP] Bridge stopped.")
