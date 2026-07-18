#!/usr/bin/env python3
"""
Music Library App
=================
Single-file, modern dark-mode (Apple-style) music library + player.

Run:
    pip install PySide6 mutagen
    python music_app.py

Build .exe:
    pyinstaller --onefile --windowed music_app.py
"""

import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.mp4 import MP4

import math

from PySide6.QtCore import (
    QFileSystemWatcher, QObject, QSettings, QStandardPaths, Qt, QThread,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import QFont, QPainter, QColor, QLinearGradient
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

ORG_NAME = "LocalMusicApps"
APP_NAME  = "MusicLibrary"

# ============================================================================
# Metadata helpers
# ============================================================================
RATING_TXXX_DESC = "RATING"
MP4_RATING_ATOM  = "----:com.apple.iTunes:RATING"
SUPPORTED_EXTENSIONS = {".mp3",".flac",".ogg",".m4a",".mp4",".wav",".wma",".aac",".oga"}


def read_display_tags(path: Path) -> Tuple[str, str]:
    title, artist = path.stem, ""
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.tags:
            t = audio.tags.get("title")
            a = audio.tags.get("artist")
            if t: title  = t[0]
            if a: artist = a[0]
    except Exception:
        pass
    return title, artist


def read_duration_seconds(path: Path) -> float:
    try:
        audio = MutagenFile(path)
        if audio and audio.info and hasattr(audio.info, "length"):
            return float(audio.info.length)
    except Exception:
        pass
    return 0.0


def read_rating(path: Path) -> int:
    ext = path.suffix.lower()
    try:
        if ext in (".mp3", ".wav", ".aac"):
            try:
                id3 = ID3(path)
            except ID3NoHeaderError:
                return 0
            for frame in id3.getall("TXXX"):
                if getattr(frame, "desc", "").upper() == RATING_TXXX_DESC:
                    try:
                        return _clamp(int(str(frame.text[0])))
                    except (ValueError, IndexError):
                        return 0
            return 0
        elif ext == ".flac":
            audio = FLAC(path)
            val = audio.get(RATING_TXXX_DESC.lower())
            return _clamp(_safe_int(val[0])) if val else 0
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(path)
            val = audio.get(RATING_TXXX_DESC.lower())
            return _clamp(_safe_int(val[0])) if val else 0
        elif ext in (".m4a", ".mp4"):
            audio = MP4(path)
            if audio.tags:
                val = audio.tags.get(MP4_RATING_ATOM)
                if val:
                    try:
                        return _clamp(int(val[0].decode("utf-8")))
                    except (ValueError, IndexError, AttributeError):
                        return 0
            return 0
        else:
            audio = MutagenFile(path)
            if audio and audio.tags and RATING_TXXX_DESC.lower() in audio.tags:
                return _clamp(_safe_int(audio.tags[RATING_TXXX_DESC.lower()][0]))
            return 0
    except Exception:
        return 0


def write_rating(path: Path, rating: int) -> bool:
    """Write rating (0-5) directly into the audio file's own metadata."""
    rating = _clamp(rating)
    ext = path.suffix.lower()
    try:
        if ext in (".mp3", ".wav", ".aac"):
            try:
                id3 = ID3(path)
            except ID3NoHeaderError:
                id3 = ID3()
            for f in [f for f in id3.getall("TXXX")
                      if getattr(f, "desc", "").upper() == RATING_TXXX_DESC]:
                id3.delall(f"TXXX:{f.desc}")
            id3.add(TXXX(encoding=3, desc=RATING_TXXX_DESC, text=[str(rating)]))
            id3.save(path)
            return True
        elif ext == ".flac":
            audio = FLAC(path)
            audio[RATING_TXXX_DESC.lower()] = [str(rating)]
            audio.save()
            return True
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(path)
            audio[RATING_TXXX_DESC.lower()] = [str(rating)]
            audio.save()
            return True
        elif ext in (".m4a", ".mp4"):
            audio = MP4(path)
            if audio.tags is None:
                audio.add_tags()
            audio.tags[MP4_RATING_ATOM] = [str(rating).encode("utf-8")]
            audio.save()
            return True
        else:
            audio = MutagenFile(path)
            if audio is None:
                return False
            if audio.tags is None:
                audio.add_tags()
            audio.tags[RATING_TXXX_DESC.lower()] = [str(rating)]
            audio.save()
            return True
    except Exception:
        return False


def _clamp(v: int) -> int:
    return max(0, min(5, v))


def _safe_int(v) -> int:
    try:
        return int(str(v))
    except (ValueError, TypeError):
        return 0


# ============================================================================
# Library helpers
# ============================================================================
@dataclass
class Song:
    path:     Path
    category: str
    title:    str
    artist:   str
    rating:   int
    duration: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def filename(self) -> str:
        return self.path.name


class SafeMoveError(Exception):
    pass


def list_categories(root: Path) -> List[str]:
    if not root.is_dir():
        return []
    return sorted(
        [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")],
        key=str.lower,
    )


def fast_list_files(root: Path) -> Dict[str, Tuple[str, int, float]]:
    result: Dict[str, Tuple[str, int, float]] = {}
    if not root.is_dir():
        return result
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        category = folder.name
        for file in folder.iterdir():
            if not file.is_file() or file.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                st = file.stat()
                result[str(file)] = (category, st.st_size, st.st_mtime)
            except OSError:
                continue
    return result


def scan_library(root: Path) -> List[Song]:
    songs: List[Song] = []
    for path_str, (category, _s, _m) in fast_list_files(root).items():
        file = Path(path_str)
        title, artist = read_display_tags(file)
        songs.append(Song(path=file, category=category, title=title,
                          artist=artist, rating=read_rating(file),
                          duration=read_duration_seconds(file)))
    return songs


def safe_move_song(song: "Song", root: Path, target_category: str) -> Path:
    target_dir = root / target_category
    if not target_dir.is_dir():
        raise SafeMoveError(f"Target folder does not exist: {target_dir}")
    src = song.path
    if not src.is_file():
        raise SafeMoveError(f"Source file not found: {src}")
    dest = target_dir / src.name
    if dest.resolve() == src.resolve():
        return src
    if dest.exists():
        stem, suffix = src.stem, src.suffix
        counter = 1
        while dest.exists():
            dest = target_dir / f"{stem} ({counter}){suffix}"
            counter += 1
    tmp = dest.with_name(dest.name + ".part")
    try:
        shutil.copy2(src, tmp)
        if os.path.getsize(tmp) != os.path.getsize(src):
            raise SafeMoveError("Verification failed (size mismatch)")
        os.replace(tmp, dest)
        os.remove(src)
    except Exception as e:
        if tmp.exists():
            try: os.remove(tmp)
            except OSError: pass
        raise SafeMoveError(str(e))
    return dest


# ============================================================================
# On-disk cache
# ============================================================================
def get_cache_path(root: Path) -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not base:
        base = str(Path.home() / ".music_library_app")
    bp = Path(base)
    try:
        bp.mkdir(parents=True, exist_ok=True)
    except OSError:
        bp = Path.home()
    h = hashlib.md5(str(root.resolve()).encode()).hexdigest()
    return bp / f"music_library_cache_{h}.json"


def load_cache(root: Path) -> Dict[str, dict]:
    p = get_cache_path(root)
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(root: Path, cache: Dict[str, dict]):
    p = get_cache_path(root)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


# ============================================================================
# Background scan worker
# ============================================================================
class ScanWorker(QThread):
    totalDetermined = Signal(int)
    progressUpdate  = Signal(int, int, object, str)
    batchReady      = Signal(list)
    songReady       = Signal(object)
    rowsRemoved     = Signal(list)
    scanFinished    = Signal(list, list, int)
    scanError       = Signal(str)

    def __init__(self, root: Path, previous_cache: Dict[str, dict], parent=None):
        super().__init__(parent)
        self.root = root
        self.previous_cache = previous_cache or {}
        self.new_cache: Dict[str, dict] = {}

    def run(self):
        try:
            current = fast_list_files(self.root)
        except Exception as e:
            self.scanError.emit(str(e)); return

        old = self.previous_cache
        old_paths, new_paths = set(old), set(current)
        common = old_paths & new_paths

        unchanged, changed = [], []
        for p in common:
            _, size, mtime = current[p]
            rec = old.get(p, {})
            if rec.get("size") == size and rec.get("mtime") is not None \
                    and abs(rec["mtime"] - mtime) < 1.0:
                unchanged.append(p)
            else:
                changed.append(p)

        added_paths   = list(new_paths - old_paths)
        missing_paths = list(old_paths - new_paths)

        missing_by_size: Dict[int, List[str]] = {}
        for p in missing_paths:
            missing_by_size.setdefault(old.get(p, {}).get("size"), []).append(p)

        moved_pairs, truly_added = [], []
        for p in added_paths:
            _, size, _ = current[p]
            cands = missing_by_size.get(size)
            if cands:
                old_p = cands.pop()
                if not cands: del missing_by_size[size]
                moved_pairs.append((old_p, p))
            else:
                truly_added.append(p)

        truly_missing = [p for lst in missing_by_size.values() for p in lst]
        new_cache: Dict[str, dict] = {}

        # Build the full ordered work list:
        #   Phase 1 – cached/moved  (fast, no disk tag read)
        #   Phase 2 – new/changed   (slow, must read tags from disk)
        # We stream ALL of them one-by-one so the UI always shows live progress.

        fast_items: List[Tuple[str, bool]] = (
            [(p, False) for p in unchanged] +
            [(new_p, False) for _, new_p in moved_pairs]
        )
        slow_items: List[str] = truly_added + changed

        total = len(fast_items) + len(slow_items)
        self.totalDetermined.emit(total)

        start = time.monotonic()
        done  = 0

        # ── Phase 1: cached songs (no tag reading, very fast) ──────────
        for p, _ in fast_items:
            cat, size, mtime = current[p]
            # Was this a moved file? find original key if needed
            rec = old.get(p)
            if rec is None:
                # moved: find original record
                for old_p, new_p in moved_pairs:
                    if new_p == p:
                        rec = old.get(old_p, {})
                        break
            if rec is None:
                rec = {}
            song = Song(
                path=Path(p), category=cat,
                title=rec.get("title", Path(p).stem),
                artist=rec.get("artist", ""),
                rating=rec.get("rating", 0),
                duration=rec.get("duration", 0.0),
                id=rec.get("id") or uuid.uuid4().hex,
            )
            new_cache[p] = {**rec, "size": size, "mtime": mtime, "id": song.id}
            done += 1
            elapsed = time.monotonic() - start
            rate    = done / elapsed if elapsed > 0.001 else 0
            eta     = (total - done) / rate if rate > 0 else None
            self.progressUpdate.emit(done, total, eta, song.title)
            self.songReady.emit(song)

        # ── Phase 2: new/changed songs (reads tags from disk) ──────────
        for p in slow_items:
            cat, size, mtime = current[p]
            po = Path(p)
            title, artist = read_display_tags(po)
            rating        = read_rating(po)
            duration      = read_duration_seconds(po)
            old_id        = old.get(p, {}).get("id")
            song = Song(path=po, category=cat, title=title, artist=artist,
                        rating=rating, duration=duration,
                        id=old_id or uuid.uuid4().hex)
            new_cache[p] = {"size": size, "mtime": mtime, "title": title,
                            "artist": artist, "rating": rating,
                            "duration": duration, "id": song.id}
            done += 1
            elapsed = time.monotonic() - start
            rate    = done / elapsed if elapsed > 0.001 else 0
            eta     = (total - done) / rate if rate > 0 else None
            self.progressUpdate.emit(done, total, eta, po.name)
            self.songReady.emit(song)

        removed_ids = [old[p]["id"] for p in truly_missing if "id" in old.get(p, {})]
        if removed_ids:
            self.rowsRemoved.emit(removed_ids)

        self.new_cache = new_cache
        self.scanFinished.emit(
            [new_cache.get(p, {}).get("title", Path(p).stem) for p in truly_added],
            [old[p].get("title", Path(p).stem) for p in truly_missing],
            len(moved_pairs),

        )


# ============================================================================
# Player
# ============================================================================
class RepeatMode(Enum):
    OFF     = 0
    ONE     = 1
    ALL     = 2
    REVERSE = 3   # play all songs in reverse order


class PlayerController(QObject):
    songChanged          = Signal(object)
    playbackStateChanged = Signal(bool)
    positionChanged      = Signal(int)
    durationChanged      = Signal(int)
    repeatModeChanged    = Signal(object)
    shuffleChanged       = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio  = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._queue: List[Song] = []
        self._index = -1
        self._repeat  = RepeatMode.OFF
        self._shuffle = False
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.positionChanged.connect(self.positionChanged.emit)
        self._player.durationChanged.connect(self.durationChanged.emit)
        self._player.mediaStatusChanged.connect(self._on_media_status)

    def current_song(self) -> Optional[Song]:
        return self._queue[self._index] if 0 <= self._index < len(self._queue) else None

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlayingState

    def set_repeat_mode(self, mode: RepeatMode):
        self._repeat = mode; self.repeatModeChanged.emit(mode)

    def repeat_mode(self) -> RepeatMode:
        return self._repeat

    def is_shuffle(self) -> bool:
        return self._shuffle

    def set_shuffle(self, enabled: bool):
        self._shuffle = enabled
        self.shuffleChanged.emit(enabled)

    def toggle_play_pause(self):
        if self._index < 0: return
        self._player.pause() if self.is_playing() else self._player.play()

    def next(self):
        if not self._queue: return
        if self._repeat == RepeatMode.ONE:
            self._load(autoplay=True); return
        if self._shuffle:
            import random
            candidates = [i for i in range(len(self._queue)) if i != self._index]
            if candidates:
                self._index = random.choice(candidates)
            elif self._repeat in (RepeatMode.ALL, RepeatMode.REVERSE):
                self._index = 0
            else:
                self._player.stop(); self.songChanged.emit(None); return
        elif self._repeat == RepeatMode.REVERSE:
            # REVERSE: auto-advance goes backwards
            if self._index - 1 >= 0:
                self._index -= 1
            else:
                self._player.stop(); self.songChanged.emit(None); return
        else:
            if self._index + 1 < len(self._queue):
                self._index += 1
            elif self._repeat == RepeatMode.ALL:
                self._index = 0
            else:
                self._player.stop(); self.songChanged.emit(None); return
        self._load(autoplay=True)

    def previous(self):
        if not self._queue: return
        if self._player.position() > 3000:
            self._player.setPosition(0); return
        if self._shuffle:
            import random
            candidates = [i for i in range(len(self._queue)) if i != self._index]
            if candidates:
                self._index = random.choice(candidates)
            else:
                self._index = 0
        elif self._repeat == RepeatMode.REVERSE:
            # In REVERSE mode, "previous" goes forward
            if self._index + 1 < len(self._queue):
                self._index += 1
            else:
                self._index = len(self._queue) - 1
        else:
            if self._index - 1 >= 0:
                self._index -= 1
            elif self._repeat == RepeatMode.ALL:
                self._index = len(self._queue) - 1
            else:
                self._index = 0
        self._load(autoplay=True)

    def seek(self, ms: int): self._player.setPosition(ms)

    def set_volume(self, v: int):
        self._audio.setVolume(max(0, min(100, v)) / 100.0)

    def play_song(self, song: Song, queue: Optional[List[Song]] = None):
        if queue is not None:
            self._queue = list(queue)
            self._index = self._queue.index(song) if song in self._queue else 0
        else:
            if song in self._queue:
                self._index = self._queue.index(song)
            else:
                self._queue = [song]; self._index = 0
        self._load(autoplay=True)

    def notify_song_path_changed(self, song: Song, new_path: Path):
        song.path = new_path
        if self.current_song() is song:
            was = self.is_playing()
            pos = self._player.position()
            self._player.setSource(QUrl.fromLocalFile(str(new_path)))
            if was: self._player.play()
            self._player.setPosition(pos)

    def _load(self, autoplay=False):
        song = self.current_song()
        if song is None: return
        self._player.setSource(QUrl.fromLocalFile(str(song.path)))
        self.songChanged.emit(song)
        if autoplay: self._player.play()

    def _on_state(self, state):
        self.playbackStateChanged.emit(state == QMediaPlayer.PlayingState)

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.next()


# ============================================================================
# LockButton widget
# ============================================================================
class LockButton(QToolButton):
    """
    Minimal edit-permission indicator.

    Green  =  locked / safe.   Shows a small pen icon in green.
              Nothing can be changed by accident.

    Red    =  unlocked / editing.  Same pen in red.
              Attention: an important change is now possible.

    Uses a simple pen glyph (✒ U+2712) which is clean, universally
    recognised as "edit", and renders crisply at small sizes.
    """
    toggledLock = Signal(bool)   # True = now unlocked

    # ✒  BLACK NIB — clean, minimal, universally understood as "edit"
    _ICON = "\u2712"

    _CSS_LOCKED = (
        "QToolButton{"
        "  background: transparent;"
        "  border: none;"
        "  color: #30D158;"          # green = safe
        "  font-size: 13px;"
        "  padding: 0px;"
        "}"
        "QToolButton:hover{ color: #4dde70; }"
    )

    _CSS_UNLOCKED = (
        "QToolButton{"
        "  background: transparent;"
        "  border: none;"
        "  color: #FF453A;"          # red = attention / editing active
        "  font-size: 13px;"
        "  padding: 0px;"
        "}"
        "QToolButton:hover{ color: #ff6961; }"
    )

    def __init__(self, tip_locked: str, tip_unlocked: str, parent=None):
        super().__init__(parent)
        self._locked       = True
        self._tip_locked   = tip_locked
        self._tip_unlocked = tip_unlocked
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(20, 20)
        self.clicked.connect(self._toggle)
        self._refresh()

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, v: bool, emit=False):
        self._locked = v
        self._refresh()
        if emit:
            self.toggledLock.emit(not v)

    def _toggle(self):
        self._locked = not self._locked
        self._refresh()
        self.toggledLock.emit(not self._locked)

    def _refresh(self):
        self.setText(self._ICON)
        if self._locked:
            self.setToolTip(self._tip_locked)
            self.setStyleSheet(self._CSS_LOCKED)
        else:
            self.setToolTip(self._tip_unlocked)
            self.setStyleSheet(self._CSS_UNLOCKED)


class StarRatingWidget(QWidget):
    """
    5 stars always visible:
    - No rating  → 5 clearly visible GRAY outline stars  ☆☆☆☆☆
    - With rating → filled GREEN stars + gray empties     ★★★☆☆
    Always directly clickable (no lock needed).
    Clicking the same active star resets to 0.
    """
    ratingChanged = Signal(int)
    COLOR_FILLED = "#FFD60A"   # yellow
    COLOR_EMPTY  = "#8e8e93"   # medium gray – clearly visible on dark background

    def __init__(self, rating: int = 0, parent=None):
        super().__init__(parent)
        self._rating   = _clamp(rating)
        self._editable = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        self._btns: List[QPushButton] = []
        for i in range(5):
            btn = QPushButton()
            btn.setFlat(True)
            btn.setFixedSize(26, 26)
            btn.setCursor(Qt.PointingHandCursor)
            # Use objectName so our specific style beats the global QPushButton rule
            btn.setObjectName("starBtn")
            btn.clicked.connect(lambda _checked=False, idx=i: self._on_click(idx))
            lay.addWidget(btn)
            self._btns.append(btn)
        lay.addStretch()
        self._refresh()

    def rating(self) -> int:
        return self._rating

    def set_rating(self, v: int):
        self._rating = _clamp(v)
        self._refresh()

    def set_editable(self, v: bool):
        self._editable = v
        self._refresh()

    def _on_click(self, idx: int):
        if not self._editable:
            return
        new = idx + 1
        if new == self._rating:
            new = 0   # click same active star → reset to 0
        self._rating = new
        self._refresh()
        self.ratingChanged.emit(self._rating)

    def _refresh(self):
        for i, btn in enumerate(self._btns):
            filled = i < self._rating
            color  = self.COLOR_FILLED if filled else self.COLOR_EMPTY
            char   = "\u2605" if filled else "\u2606"   # ★ / ☆
            btn.setText(char)
            # Use !important-equivalent: very specific inline style
            # The objectName selector #starBtn beats the global QPushButton rule
            btn.setStyleSheet(
                f"QPushButton#starBtn {{"
                f"  border: none;"
                f"  background: transparent;"
                f"  color: {color};"
                f"  font-size: 18px;"
                f"  font-weight: normal;"
                f"  padding: 0px;"
                f"}}"
                f"QPushButton#starBtn:hover {{"
                f"  background: rgba(255,255,255,0.08);"
                f"  border-radius: 4px;"
                f"}}"
                f"QPushButton#starBtn:disabled {{"
                f"  color: {color};"
                f"  background: transparent;"
                f"}}"
            )
            btn.setEnabled(self._editable)


# ============================================================================
# Loading dialog – shown whenever new/changed files are being read
# ============================================================================
# ============================================================================
# VU Meter widget – real-time dB level display
# ============================================================================
class VUMeter(QWidget):
    """
    Professional stereo VU meter (L / R bars).
    Driven by a QTimer that reads the current playback position
    and estimates the instantaneous level.  When a real audio
    level probe is unavailable (Qt6 limitation without a custom
    AudioSink), the meter simulates a realistic signal envelope
    that tracks the song's current position.

    The meter shows dBFS values from -60 dB (silence) to 0 dB (peak).
    Colour zones:
        -60 … -18 dB  →  green   (#30D158)
        -18 …  -6 dB  →  yellow  (#FFD60A)
         -6 …   0 dB  →  red     (#FF453A)
    """

    _DB_MIN = -60.0
    _DB_MAX  =   0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(54, 60)
        self.setMaximumWidth(70)

        # Current level for each channel, in dBFS (-60 … 0)
        self._level_L: float = self._DB_MIN
        self._level_R: float = self._DB_MIN

        # Peak hold
        self._peak_L: float = self._DB_MIN
        self._peak_R: float = self._DB_MIN
        self._peak_L_age: int = 0
        self._peak_R_age: int = 0

        # Decay parameters (called at ~30 fps)
        self._decay_rate  = 3.5   # dB per frame
        self._peak_hold   = 20    # frames before peak starts falling
        self._peak_decay  = 1.0

        # Timer drives repaints
        self._timer = QTimer(self)
        self._timer.setInterval(33)   # ~30 fps
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._playing = False
        self._pos_ms  = 0
        self._dur_ms  = 1

        # Smooth state (0..1 linear power)
        self._smooth_L = 0.0
        self._smooth_R = 0.0

    # ── Public API called by MainWindow ──────────────────────────────
    def set_playing(self, playing: bool):
        self._playing = playing
        if not playing:
            self._smooth_L = 0.0
            self._smooth_R = 0.0

    def set_position(self, pos_ms: int, dur_ms: int):
        self._pos_ms = pos_ms
        self._dur_ms = max(1, dur_ms)

    # ── Internal tick ─────────────────────────────────────────────────
    def _tick(self):
        if self._playing and self._dur_ms > 0:
            # Simulate a signal that varies with playback position.
            # Uses multiple sine waves at different frequencies to produce
            # a realistic-looking level that changes over time.
            t = self._pos_ms / 1000.0

            # Base level: pseudo-random but deterministic per second
            import random
            rng = random.Random(int(t * 4))   # changes 4× per second
            base = rng.uniform(0.35, 0.90)

            # Add fast variation
            vary_L = 0.5 + 0.5 * math.sin(t * 7.3 + 0.0)
            vary_R = 0.5 + 0.5 * math.sin(t * 6.1 + 1.1)

            target_L = base * (0.7 + 0.3 * vary_L)
            target_R = base * (0.7 + 0.3 * vary_R)

            # Smooth (attack fast, release slow)
            α_attack  = 0.5
            α_release = 0.15
            αL = α_attack if target_L > self._smooth_L else α_release
            αR = α_attack if target_R > self._smooth_R else α_release
            self._smooth_L += αL * (target_L - self._smooth_L)
            self._smooth_R += αR * (target_R - self._smooth_R)

            # Convert to dBFS
            self._level_L = self._linear_to_db(self._smooth_L)
            self._level_R = self._linear_to_db(self._smooth_R)
        else:
            # Decay to silence
            self._level_L = max(self._DB_MIN, self._level_L - self._decay_rate)
            self._level_R = max(self._DB_MIN, self._level_R - self._decay_rate)

        # Peak hold / decay
        for ch in ('L', 'R'):
            lvl = self._level_L if ch == 'L' else self._level_R
            peak_attr, age_attr = f'_peak_{ch}', f'_peak_{ch}_age'
            if lvl >= getattr(self, peak_attr):
                setattr(self, peak_attr, lvl)
                setattr(self, age_attr, 0)
            else:
                age = getattr(self, age_attr) + 1
                setattr(self, age_attr, age)
                if age > self._peak_hold:
                    new_peak = max(self._DB_MIN, getattr(self, peak_attr) - self._peak_decay)
                    setattr(self, peak_attr, new_peak)

        self.update()

    # ── Painting ──────────────────────────────────────────────────────
    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        # Background
        p.fillRect(0, 0, w, h, QColor("#1c1c1e"))

        bar_w   = (w - 14) // 2    # width of each channel bar
        gap     = 4                  # gap between L and R
        label_h = 14                 # height reserved for "L" / "R" labels
        bar_h   = h - label_h - 4   # usable bar height

        x_L = 4
        x_R = x_L + bar_w + gap

        self._draw_channel(p, x_L, label_h, bar_w, bar_h,
                           self._level_L, self._peak_L, "L")
        self._draw_channel(p, x_R, label_h, bar_w, bar_h,
                           self._level_R, self._peak_R, "R")

        p.end()

    def _draw_channel(self, p: QPainter,
                      x: int, y: int, w: int, h: int,
                      level_db: float, peak_db: float, label: str):
        # Draw track (background)
        p.fillRect(x, y, w, h, QColor("#2c2c2e"))

        # Filled portion (bottom = _DB_MIN, top = _DB_MAX)
        frac  = self._db_to_frac(level_db)
        fill_h = max(0, int(frac * h))
        fill_y = y + h - fill_h

        if fill_h > 0:
            # Gradient: green → yellow → red (bottom to top)
            grad = QLinearGradient(x, y + h, x, y)
            grad.setColorAt(0.00, QColor("#30D158"))   # green
            grad.setColorAt(0.65, QColor("#FFD60A"))   # yellow
            grad.setColorAt(0.85, QColor("#FF9F0A"))   # orange
            grad.setColorAt(1.00, QColor("#FF453A"))   # red
            from PySide6.QtGui import QBrush
            p.fillRect(x, fill_y, w, fill_h, QBrush(grad))

        # Peak marker
        pk_frac = self._db_to_frac(peak_db)
        pk_y    = y + h - max(1, int(pk_frac * h))
        pk_color = QColor("#FF453A") if peak_db > -6 else QColor("#FFD60A") if peak_db > -18 else QColor("#30D158")
        p.fillRect(x, pk_y, w, 2, pk_color)

        # dB label
        db_str = f"{level_db:.0f}"
        p.setPen(QColor("#8e8e93"))
        from PySide6.QtCore import QRect
        p.setFont(QFont("SF Mono, Consolas, monospace", 7))
        p.drawText(QRect(x, 0, w, 13), Qt.AlignCenter, db_str)

        # Channel label (L / R) below the bar
        bar_bottom = y + h + 2
        p.setPen(QColor("#6e6e73"))
        p.setFont(QFont("", 8, QFont.Bold))
        p.drawText(QRect(x, bar_bottom, w, 12), Qt.AlignCenter, label)

    @staticmethod
    def _linear_to_db(linear: float) -> float:
        if linear <= 0.0:
            return -60.0
        db = 20.0 * math.log10(max(1e-10, linear))
        return max(-60.0, min(0.0, db))

    @staticmethod
    def _db_to_frac(db: float) -> float:
        """Map dB (-60…0) to fraction (0…1) with log scaling."""
        db = max(-60.0, min(0.0, db))
        return (db + 60.0) / 60.0


class LoadingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Loading Library")
        self.setModal(True)
        self.setFixedSize(420, 120)
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)

        self.title_lbl = QLabel("Loading music library…")
        self.title_lbl.setObjectName("subtleLabel")
        lay.addWidget(self.title_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        lay.addWidget(self.bar)

        self.count_lbl = QLabel("")
        self.count_lbl.setObjectName("subtleLabel")
        lay.addWidget(self.count_lbl)

    def update_progress(self, done: int, total: int,
                        eta_s: Optional[float], name: str):
        pct = int(done / total * 100) if total else 0
        self.bar.setValue(pct)
        self.count_lbl.setText(f"{done} of {total} songs  —  {pct}%")


# ============================================================================
# Stylesheet
# ============================================================================
DARK_QSS = """
* { font-family: -apple-system,"SF Pro Text","Segoe UI","Helvetica Neue",Arial,sans-serif;
    outline: none; }

QMainWindow, QWidget#centralWidget { background: #1c1c1e; }
QWidget  { color: #f2f2f7; background: transparent; }
QDialog  { background: #1c1c1e; }

QLabel#sectionTitle { font-size:20px; font-weight:600; color:#fff; padding:4px 0; }
QLabel#subtleLabel  { color:#8e8e93; font-size:12px; }
QLabel#pathLabel    { color:#0a84ff; font-size:12px; padding:2px 8px;
                      background:rgba(10,132,255,.1); border-radius:6px; }
QLabel#statusLabel  { color:#8e8e93; font-size:12px; padding:2px 8px; }

#topBar    { background:#1c1c1e; border:none; padding:10px; spacing:8px; }
#filterBar { background:#232325; border-bottom:1px solid #2c2c2e; }

QPushButton {
    background:#2c2c2e; border:1px solid #3a3a3c; border-radius:10px;
    padding:8px 16px; color:#f2f2f7; font-size:13px; font-weight:500; }
QPushButton:hover    { background:#3a3a3c; border-color:#48484a; }
QPushButton:pressed  { background:#232325; }
QPushButton:disabled { color:#5a5a5e; border-color:#2c2c2e; }

QPushButton#accentButton { background:#0a84ff; border-color:#0a84ff;
                           color:#fff; font-weight:600; }
QPushButton#accentButton:hover { background:#3399ff; }

QPushButton#playFilteredButton { background:#30d158; border-color:#30d158;
                                 color:#fff; font-weight:600; }
QPushButton#playFilteredButton:hover { background:#4dde70; }

/* ── Transport buttons ── */
QPushButton#transportBtn {
    background: transparent;
    border: none;
    border-radius: 16px;
    color: #e5e5ea;
    font-size: 16px;
    font-weight: 500;
}
QPushButton#transportBtn:hover {
    background: rgba(255,255,255,0.10);
    color: #ffffff;
}
QPushButton#transportBtn:pressed {
    background: rgba(255,255,255,0.06);
}

QPushButton#transportBtnDim {
    background: transparent;
    border: none;
    border-radius: 14px;
    color: #6e6e73;
    font-size: 16px;
    font-weight: 600;
}
QPushButton#transportBtnDim:hover {
    background: rgba(255,255,255,0.08);
    color: #aeaeb2;
}

/* Play/Pause – filled circle, prominent */
QPushButton#playBtn {
    background: #ffffff;
    border: none;
    border-radius: 22px;
    color: #1c1c1e;
    font-size: 16px;
    font-weight: 700;
}
QPushButton#playBtn:hover  { background: #e5e5ea; }
QPushButton#playBtn:pressed{ background: #c7c7cc; }

/* Now-playing label */
QLabel#nowPlayingLabel {
    color: #f2f2f7;
    font-size: 13px;
    font-weight: 500;
}

/* Time labels */
QLabel#timeLabel {
    color: #8e8e93;
    font-size: 11px;
    font-family: "SF Mono", "Consolas", monospace;
}

/* Volume icon */
QLabel#volIcon {
    color: #8e8e93;
    font-size: 14px;
    padding-right: 4px;
}

QTableWidget {
    background:#1c1c1e; alternate-background-color:#202022;
    border:none; gridline-color:transparent;
    selection-background-color:rgba(10,132,255,.25); selection-color:#fff; }
QTableWidget::item { padding:6px 4px; border-bottom:1px solid #2c2c2e; }
QHeaderView::section {
    background:#1c1c1e; color:#8e8e93; border:none;
    border-bottom:1px solid #2c2c2e;
    padding:8px 6px; font-size:11px; font-weight:600; text-transform:uppercase; }
QTableWidget::item:selected { background:rgba(10,132,255,.22); }

QComboBox {
    background:#2c2c2e; border:1px solid #3a3a3c; border-radius:8px;
    padding:4px 10px; color:#f2f2f7; }
QComboBox:disabled { color:#5a5a5e; background:#232325; }
QComboBox::drop-down { border:none; width:18px; }
QComboBox QAbstractItemView {
    background:#2c2c2e; border:1px solid #3a3a3c;
    selection-background-color:#0a84ff; color:#f2f2f7; outline:none; }

QScrollBar:vertical {
    background: transparent; width: 8px; margin: 2px 2px 2px 0;
}
QScrollBar::handle:vertical {
    background: #3a3a3c; border-radius: 4px; min-height: 28px;
}
QScrollBar::handle:vertical:hover   { background: #58585c; }
QScrollBar::handle:vertical:pressed  { background: #6e6e73; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }

QSlider::groove:horizontal { height:4px; background:#3a3a3c; border-radius:2px; }
QSlider::sub-page:horizontal { background:#0a84ff; border-radius:2px; }
QSlider::handle:horizontal { background:#fff; width:12px; height:12px;
                              margin:-4px 0; border-radius:6px; }
QSlider::handle:horizontal:hover { background:#e5e5ea; }

QLineEdit { background:#2c2c2e; border:1px solid #3a3a3c; border-radius:8px;
            padding:5px 10px; color:#f2f2f7; }
QLineEdit:focus { border-color:#0a84ff; }

QProgressBar { background:#2c2c2e; border:1px solid #3a3a3c; border-radius:8px;
               text-align:center; color:#f2f2f7; height:18px; }
QProgressBar::chunk { background:#0a84ff; border-radius:8px; }

/* Star rating buttons – must override the global QPushButton rule */
QPushButton#starBtn {
    border: none;
    background: transparent;
    color: #8e8e93;
    font-size: 18px;
    font-weight: normal;
    padding: 0px;
    min-width: 26px;
    min-height: 26px;
}
QPushButton#starBtn:hover  { background: rgba(255,255,255,0.08); border-radius:4px; }
QPushButton#starBtn:disabled { background: transparent; color: #8e8e93; }

QFrame#playerBar { background:#232325; border-top:1px solid #2c2c2e; }

/* Path button in top bar */
QPushButton#pathBtn {
    background: rgba(10,132,255,0.10);
    border: 1px solid rgba(10,132,255,0.30);
    border-radius: 8px;
    color: #0a84ff;
    font-size: 12px;
    font-weight: 500;
    padding: 4px 10px;
    text-align: left;
}
QPushButton#pathBtn:hover {
    background: rgba(10,132,255,0.20);
    border-color: #0a84ff;
}

/* Toast strip */
QLabel#toastLabel { font-size: 12px; font-weight: 500; }

QToolTip { background:#3a3a3c; color:#f2f2f7; border:1px solid #48484a;
           padding:4px 8px; border-radius:6px; }
QMessageBox { background:#2c2c2e; }
"""


# ============================================================================
# Helpers
# ============================================================================
def _fmt_time(ms: int) -> str:
    s = max(0, ms // 1000)
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}"


def _centered(widget: QWidget, right_pad: int = 8) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(8, 0, right_pad, 0)
    lay.addStretch()
    lay.addWidget(widget)
    lay.addStretch()
    return w


# ============================================================================
# Column indices
# ============================================================================
COL_TITLE         = 0
COL_ARTIST        = 1
COL_CATEGORY      = 2
COL_CATEGORY_LOCK = 3
COL_RATING        = 4
COL_RATING_LOCK   = 5
COL_COUNT         = 6

ROW_HEIGHT = 38   # px – fits 20px lock icons with comfortable padding


# ============================================================================
# Main window
# ============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Music Library")
        self.resize(1200, 760)

        self.settings  = QSettings(ORG_NAME, APP_NAME)
        self.root_path: Optional[Path] = None
        self.cache: Dict[str, dict]    = {}
        self.songs_by_id: Dict[str, Song]          = {}
        self.row_items:   Dict[str, QTableWidgetItem] = {}
        self._highlighted_id: Optional[str]        = None
        self._last_cats: List[str]                  = []

        # active filter state
        self._filter_cat    = "All"
        self._filter_rating = 0      # 0 = show all
        self._filter_search = ""

        self.scan_worker: Optional[ScanWorker] = None
        self.pending_rescan = False
        self.last_added:   List[str] = []
        self.last_removed: List[str] = []

        self.player = PlayerController(self)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._on_dir_changed)
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(500)
        self._rescan_timer.timeout.connect(self._start_scan)

        self._build_ui()
        self._connect_player()

        last = self.settings.value("root_path", "")
        if last and Path(last).is_dir():
            self._set_root(Path(last))

    # ------------------------------------------------------------------
    # UI building
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget(); central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        vlay = QVBoxLayout(central)
        vlay.setContentsMargins(0, 0, 0, 0); vlay.setSpacing(0)
        vlay.addWidget(self._build_top_bar())
        vlay.addWidget(self._build_filter_bar())
        vlay.addWidget(self._build_table(), stretch=1)
        vlay.addWidget(self._build_player_bar())

    # ── top bar (compact, everything inline) ─────────────────────────
    def _build_top_bar(self) -> QWidget:
        wrapper = QWidget(); wrapper.setObjectName("topBar")
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Main bar row
        bar = QFrame(); bar.setObjectName("topBarInner")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 8, 14, 8); lay.setSpacing(10)

        title = QLabel("Music Library")
        title.setObjectName("sectionTitle")
        lay.addWidget(title)

        self.path_btn = QPushButton("No folder")
        self.path_btn.setObjectName("pathBtn")
        self.path_btn.setToolTip("Click to change the music folder")
        self.path_btn.setCursor(Qt.PointingHandCursor)
        self.path_btn.setMaximumWidth(260)
        self.path_btn.clicked.connect(self._choose_folder)
        lay.addWidget(self.path_btn)

        lay.addStretch()

        refresh_btn = QPushButton("\u21BA  Refresh")
        refresh_btn.clicked.connect(self._start_scan)
        lay.addWidget(refresh_btn)

        outer.addWidget(bar)

        # Permanent status strip — always visible, never a popup.
        # This is the single place where ALL status info appears:
        # loading progress, confirmations, warnings, errors.
        self.info_bar = QFrame()
        self.info_bar.setObjectName("infoBar")
        self.info_bar.setFixedHeight(32)
        info_lay = QHBoxLayout(self.info_bar)
        info_lay.setContentsMargins(16, 0, 16, 0)
        info_lay.setSpacing(10)

        self.status_dot = QLabel("\u25CF")
        self.status_dot.setFixedWidth(12)
        self.status_dot.setAlignment(Qt.AlignCenter)
        self.status_dot.setStyleSheet("color:#48484a;font-size:8px;")
        info_lay.addWidget(self.status_dot)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        info_lay.addWidget(self.status_label, stretch=1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setFixedWidth(180)
        self.progress_bar.setFixedHeight(5)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        info_lay.addWidget(self.progress_bar)

        self.song_count_label = QLabel("")
        self.song_count_label.setObjectName("subtleLabel")
        self.song_count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        info_lay.addWidget(self.song_count_label)

        self.changes_btn = QPushButton("View changes")
        self.changes_btn.setEnabled(False)
        self.changes_btn.setVisible(False)
        self.changes_btn.clicked.connect(self._show_changes)
        info_lay.addWidget(self.changes_btn)

        outer.addWidget(self.info_bar)

        self._status_reset_timer = QTimer(self)
        self._status_reset_timer.setSingleShot(True)
        self._status_reset_timer.timeout.connect(self._reset_status_style)

        return wrapper

    _STATUS_COLORS = {
        "idle":    ("#48484a", "#8e8e93"),
        "loading": ("#0a84ff", "#c7c7cc"),
        "success": ("#30D158", "#d1fae5"),
        "warning": ("#FF9F0A", "#ffe5b0"),
        "error":   ("#FF453A", "#ffd0ce"),
    }

    def _set_info(self, message: str, kind: str = "idle",
                  auto_reset: bool = True, duration_ms: int = 4000):
        dot_color, text_color = self._STATUS_COLORS.get(kind, self._STATUS_COLORS["idle"])
        self.status_dot.setStyleSheet(f"color:{dot_color};font-size:8px;")
        self.status_label.setStyleSheet(f"color:{text_color};font-size:12px;")
        self.status_label.setText(message)
        bg_map = {
            "idle":    "#232325", "loading": "#1c2535",
            "success": "#0d2a1a", "warning": "#2a1e0a", "error": "#2a0f0f",
        }
        self.info_bar.setStyleSheet(
            f"QFrame#infoBar{{background:{bg_map.get(kind,'#232325')};"
            f"border-bottom:1px solid {dot_color}33;}}")
        if auto_reset and kind not in ("idle", "loading"):
            self._status_reset_timer.start(duration_ms)

    def _reset_status_style(self):
        n = len(self.songs_by_id)
        txt = f"{n} songs  \u2022  Up to date" if n else "Ready"
        self._set_info(txt, "idle", auto_reset=False)
        self.song_count_label.setText(f"{n} songs" if n else "")

    def _show_toast(self, message: str, duration_ms: int = 3500, kind: str = "info"):
        kind_map = {"info": "idle", "success": "success",
                    "warning": "warning", "error": "error"}
        self._set_info(message, kind_map.get(kind, "idle"),
                       auto_reset=True, duration_ms=duration_ms)

    def _set_status(self, txt: str):
        """Legacy helper — routes to _set_info."""
        if "Loading" in txt or "Importing" in txt or "Checking" in txt:
            self._set_info(txt, "loading", auto_reset=False)
        else:
            self._set_info(txt, "idle", auto_reset=False)


    # ── filter bar (Excel-style) ──────────────────────────────────────
    def _build_filter_bar(self) -> QWidget:
        bar = QFrame(); bar.setObjectName("filterBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 12, 8); lay.setSpacing(8)

        # Search
        search_icon = QLabel("🔍"); search_icon.setObjectName("subtleLabel")
        lay.addWidget(search_icon)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search title or artist…")
        self.search_box.setFixedWidth(230)
        self.search_box.textChanged.connect(self._on_search_changed)
        lay.addWidget(self.search_box)

        lay.addSpacing(16)

        # Category filter
        cat_lbl = QLabel("Category:"); cat_lbl.setObjectName("subtleLabel")
        lay.addWidget(cat_lbl)
        self.cat_filter = QComboBox()
        self.cat_filter.setFixedWidth(155)
        self.cat_filter.addItem("All")
        self.cat_filter.currentTextChanged.connect(self._on_cat_filter_changed)
        lay.addWidget(self.cat_filter)

        lay.addSpacing(16)

        # Rating filter — exact star values, max = 5, NOT "5+"
        rat_lbl = QLabel("Min. Rating:"); rat_lbl.setObjectName("subtleLabel")
        lay.addWidget(rat_lbl)
        self.rat_filter = QComboBox()
        self.rat_filter.setFixedWidth(168)
        self.rat_filter.addItem("All ratings", 0)
        self.rat_filter.addItem("☆☆☆☆☆  No rating", -1)
        # 1★ = at least 1 star … 5★ = exactly 5 stars (max)
        star_labels = {
            1: "★☆☆☆☆  (1 star)",
            2: "★★☆☆☆  (2 stars)",
            3: "★★★☆☆  (3 stars)",
            4: "★★★★☆  (4 stars)",
            5: "★★★★★  (5 stars)",
        }
        for n, lbl in star_labels.items():
            self.rat_filter.addItem(lbl, n)
        self.rat_filter.currentIndexChanged.connect(self._on_rat_filter_changed)
        lay.addWidget(self.rat_filter)

        lay.addSpacing(16)

        # ── Play filtered button ──────────────────────────────────────
        self.play_filtered_btn = QPushButton("▶  Play Filtered")
        self.play_filtered_btn.setObjectName("playFilteredButton")
        self.play_filtered_btn.setToolTip(
            "Play all songs currently visible (respects active filters)")
        self.play_filtered_btn.clicked.connect(self._play_filtered)
        lay.addWidget(self.play_filtered_btn)

        lay.addStretch()

        reset_btn = QPushButton("Reset filters")
        reset_btn.clicked.connect(self._reset_filters)
        lay.addWidget(reset_btn)
        return bar

    # ── song table ────────────────────────────────────────────────────
    def _build_table(self) -> QWidget:
        self.table = QTableWidget(0, COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            ["Title", "Artist", "Category", "Edit", "Rating", "Edit"])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(COL_TITLE,         QHeaderView.Stretch)
        h.setSectionResizeMode(COL_ARTIST,        QHeaderView.Stretch)
        h.setSectionResizeMode(COL_CATEGORY,      QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_CATEGORY_LOCK, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_RATING,        QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_RATING_LOCK,   QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(self._on_double_click)
        return self.table

    # ── player bar ────────────────────────────────────────────────────
    def _build_player_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("playerBar")
        bar.setFixedHeight(110)
        outer = QHBoxLayout(bar)
        outer.setContentsMargins(16, 6, 16, 8)
        outer.setSpacing(12)

        # ── VU Meter (always visible, left side) ──────────────────────
        self.vu_meter = VUMeter()
        outer.addWidget(self.vu_meter)

        # ── Main controls (centre + right) ────────────────────────────
        main_col = QVBoxLayout()
        main_col.setSpacing(4)

        # ── Seek row ──────────────────────────────────────────────────
        seek_row = QHBoxLayout()
        seek_row.setSpacing(8)

        self.lbl_pos = QLabel("0:00")
        self.lbl_pos.setObjectName("timeLabel")
        self.lbl_pos.setFixedWidth(36)
        self.lbl_pos.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # Seek slider — clickable anywhere on the track
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setObjectName("seekSlider")
        # Make the slider jump to the clicked position (not just drag)
        self.seek_slider.setStyle(self.seek_slider.style())
        self.seek_slider.mousePressEvent = self._seek_mouse_press
        self.seek_slider.sliderMoved.connect(self.player.seek)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)

        self.lbl_dur = QLabel("0:00")
        self.lbl_dur.setObjectName("timeLabel")
        self.lbl_dur.setFixedWidth(36)
        self.lbl_dur.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        seek_row.addWidget(self.lbl_pos)
        seek_row.addWidget(self.seek_slider, stretch=1)
        seek_row.addWidget(self.lbl_dur)
        main_col.addLayout(seek_row)

        # ── Controls row ──────────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.setSpacing(0)

        # Now-playing info (left)
        self.lbl_now = QLabel("Nothing is playing")
        self.lbl_now.setObjectName("nowPlayingLabel")
        self.lbl_now.setMinimumWidth(180)
        self.lbl_now.setMaximumWidth(300)
        ctrl.addWidget(self.lbl_now, stretch=1)

        ctrl.addStretch(1)

        # ── All transport buttons: white-circle style ─────────────────
        def _circle_btn(text: str, size: int, tooltip: str,
                        bg: str = "#2c2c2e", fg: str = "#f2f2f7",
                        font_size: int = 15) -> QToolButton:
            btn = QToolButton()
            btn.setText(text)
            btn.setFixedSize(size, size)
            btn.setToolTip(tooltip)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QToolButton{{background:{bg};border:none;border-radius:{size//2}px;"
                f"color:{fg};font-size:{font_size}px;font-weight:600;}}"
                f"QToolButton:hover{{background:#3a3a3c;color:#ffffff;}}"
                f"QToolButton:pressed{{background:#232325;}}"
            )
            return btn

        # ── Repeat (4-state cycle: Off → All → Reverse → One) ────────
        self.rep_btn = _circle_btn("↻", 36, "Repeat: Off",
                                   bg="#2c2c2e", fg="#8e8e93", font_size=16)
        self.rep_btn.clicked.connect(self._cycle_repeat)
        ctrl.addWidget(self.rep_btn)

        ctrl.addSpacing(8)

        # ── Prev ──────────────────────────────────────────────────────
        prev_btn = _circle_btn("⏮", 40, "Previous", font_size=17)
        prev_btn.clicked.connect(self.player.previous)
        ctrl.addWidget(prev_btn)

        ctrl.addSpacing(6)

        # ── Play / Pause (largest, white) ─────────────────────────────
        self.pp_btn = QToolButton()
        self.pp_btn.setText("▶")
        self.pp_btn.setFixedSize(50, 50)
        self.pp_btn.setToolTip("Play / Pause")
        self.pp_btn.setCursor(Qt.PointingHandCursor)
        self.pp_btn.setStyleSheet(
            "QToolButton{background:#ffffff;border:none;border-radius:25px;"
            "color:#1c1c1e;font-size:19px;font-weight:800;}"
            "QToolButton:hover{background:#e5e5ea;}"
            "QToolButton:pressed{background:#c7c7cc;}"
        )
        self.pp_btn.clicked.connect(self.player.toggle_play_pause)
        ctrl.addWidget(self.pp_btn)

        ctrl.addSpacing(6)

        # ── Next ──────────────────────────────────────────────────────
        next_btn = _circle_btn("⏭", 40, "Next", font_size=17)
        next_btn.clicked.connect(self.player.next)
        ctrl.addWidget(next_btn)

        ctrl.addSpacing(8)

        # ── Shuffle – right of Next ────────────────────────────────────
        self.shuf_btn = _circle_btn("⇄", 36, "Shuffle: Off",
                                    bg="#2c2c2e", fg="#8e8e93", font_size=16)
        self.shuf_btn.clicked.connect(self._toggle_shuffle)
        ctrl.addWidget(self.shuf_btn)

        ctrl.addStretch(1)

        # ── Volume in a FIXED-WIDTH container so the icon change
        #    (🔇/🔉/🔊) never shifts the transport buttons ────────────
        vol_container = QFrame()
        vol_container.setFixedWidth(130)
        vol_layout = QHBoxLayout(vol_container)
        vol_layout.setContentsMargins(0, 0, 0, 0)
        vol_layout.setSpacing(4)

        self.vol_icon = QLabel("🔊")
        self.vol_icon.setObjectName("volIcon")
        self.vol_icon.setFixedWidth(22)
        self.vol_icon.setAlignment(Qt.AlignCenter)
        vol_layout.addWidget(self.vol_icon)

        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.setObjectName("volSlider")
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        vol_layout.addWidget(self.vol_slider, stretch=1)
        self.player.set_volume(80)

        ctrl.addWidget(vol_container)

        main_col.addLayout(ctrl)
        outer.addLayout(main_col, stretch=1)
        return bar

    # track whether user is dragging the slider
    _seek_dragging: bool = False

    def _seek_mouse_press(self, event):
        """Make a click anywhere on the seek bar jump to that position."""
        if event.button() == Qt.LeftButton and self.seek_slider.maximum() > 0:
            # Calculate the value corresponding to the click position
            slider = self.seek_slider
            ratio  = event.position().x() / slider.width()
            value  = int(ratio * slider.maximum())
            value  = max(slider.minimum(), min(slider.maximum(), value))
            slider.setValue(value)
            self.player.seek(value)
        # Also call the default handler so the handle follows
        QSlider.mousePressEvent(self.seek_slider, event)

    def _on_seek_pressed(self):
        self._seek_dragging = True

    def _on_seek_released(self):
        self._seek_dragging = False
        self.player.seek(self.seek_slider.value())

    def _on_volume_changed(self, value: int):
        self.player.set_volume(value)
        if value == 0:
            self.vol_icon.setText("🔇")
        elif value < 40:
            self.vol_icon.setText("🔉")
        else:
            self.vol_icon.setText("🔊")

    # ------------------------------------------------------------------
    # Player wiring
    # ------------------------------------------------------------------
    def _connect_player(self):
        self.player.songChanged.connect(self._on_song_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state)
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(
            lambda d: (self.seek_slider.setRange(0, max(0, d)),
                       self.lbl_dur.setText(_fmt_time(d)),
                       self.vu_meter.set_position(self.seek_slider.value(), max(1, d))))
        self.player.playbackStateChanged.connect(
            lambda playing: self.vu_meter.set_playing(playing))

    def _on_playback_state(self, playing: bool):
        self.pp_btn.setText("⏸" if playing else "▶")

    def _on_song_changed(self, song: Optional[Song]):
        if song is None:
            self.lbl_now.setText("Nothing is playing")
        else:
            ap = f"  ·  {song.artist}" if song.artist else ""
            self.lbl_now.setText(f"{song.title}{ap}  ·  {song.category}")
        self._highlight(song)

    def _on_pos(self, ms: int):
        if not getattr(self, '_seek_dragging', False):
            self.seek_slider.setValue(ms)
        self.lbl_pos.setText(_fmt_time(ms))
        dur = self.seek_slider.maximum()
        self.vu_meter.set_position(ms, max(1, dur))

    def _cycle_repeat(self):
        """Cycle: Off → All → Reverse → One → Off"""
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.REVERSE, RepeatMode.ONE]
        new   = order[(order.index(self.player.repeat_mode()) + 1) % 4]
        self.player.set_repeat_mode(new)

        cfg = {
            RepeatMode.OFF:     ("↻", "#8e8e93", "#2c2c2e", "Repeat: Off"),
            RepeatMode.ALL:     ("↻", "#0a84ff", "#1a3a5c", "Repeat: All  —  restarts from beginning"),
            RepeatMode.REVERSE: ("↺", "#0a84ff", "#1a3a5c", "Repeat: Reverse  —  plays backwards through list"),
            RepeatMode.ONE:     ("①", "#30D158", "#1a3a2c", "Repeat: One  —  repeats this song only"),
        }
        icon, fg, bg, tip = cfg[new]
        self.rep_btn.setText(icon)
        self.rep_btn.setToolTip(tip)
        r = self.rep_btn.width() // 2
        self.rep_btn.setStyleSheet(
            f"QToolButton{{background:{bg};border:none;border-radius:{r}px;"
            f"color:{fg};font-size:16px;font-weight:700;}}"
            f"QToolButton:hover{{background:#3a3a3c;color:#ffffff;}}"
            f"QToolButton:pressed{{background:#232325;}}"
        )

    def _toggle_shuffle(self):
        enabled = not self.player.is_shuffle()
        self.player.set_shuffle(enabled)
        r = self.shuf_btn.width() // 2
        if enabled:
            self.shuf_btn.setToolTip("Shuffle: On")
            self.shuf_btn.setStyleSheet(
                f"QToolButton{{background:#1a3a5c;border:none;border-radius:{r}px;"
                f"color:#0a84ff;font-size:16px;font-weight:700;}}"
                f"QToolButton:hover{{background:#234a70;}}"
                f"QToolButton:pressed{{background:#1a3a5c;}}"
            )
        else:
            self.shuf_btn.setToolTip("Shuffle: Off")
            self.shuf_btn.setStyleSheet(
                f"QToolButton{{background:#2c2c2e;border:none;border-radius:{r}px;"
                f"color:#8e8e93;font-size:16px;font-weight:600;}}"
                f"QToolButton:hover{{background:#3a3a3c;color:#ffffff;}}"
                f"QToolButton:pressed{{background:#232325;}}"
            )


    # ------------------------------------------------------------------
    # Folder management
    # ------------------------------------------------------------------
    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select main music folder (contains category subfolders)")
        if folder:
            self._set_root(Path(folder))

    def _set_root(self, path: Path):
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())
        self.root_path = path
        # Show only the folder name on the button, full path in tooltip
        self.path_btn.setText(f"📁  {path.name}")
        self.path_btn.setToolTip(str(path))
        self.settings.setValue("root_path", str(path))

        self.table.setRowCount(0)
        self.row_items.clear(); self.songs_by_id.clear()
        self._highlighted_id = None; self._last_cats = []
        self.cache = load_cache(path)

        self._rebuild_cat_filter()
        self.watcher.addPath(str(path))
        for sub in list_categories(path):
            self.watcher.addPath(str(path / sub))
        self._start_scan()

    def _on_dir_changed(self, _: str):
        self._rescan_timer.start()

    # ------------------------------------------------------------------
    # Background scanning
    # ------------------------------------------------------------------
    def _start_scan(self):
        if self.root_path is None: return
        if self.scan_worker and self.scan_worker.isRunning():
            self.pending_rescan = True; return
        self._set_status("Checking library…")
        w = ScanWorker(self.root_path, dict(self.cache))
        w.batchReady.connect(self._on_batch)
        w.songReady.connect(self._on_song_ready)
        w.totalDetermined.connect(self._on_total)
        w.progressUpdate.connect(self._on_progress)
        w.rowsRemoved.connect(self._on_removed)
        w.scanError.connect(self._on_scan_error)
        w.scanFinished.connect(lambda a, r, m, _w=w: self._on_finished(a, r, m, _w))
        w.finished.connect(lambda _w=w: self._on_thread_done(_w))
        self.scan_worker = w; w.start()

    def _on_thread_done(self, w: ScanWorker):
        if self.scan_worker is w: self.scan_worker = None
        if self.pending_rescan:
            self.pending_rescan = False
            QTimer.singleShot(50, self._start_scan)

    def _on_batch(self, songs: List[Song]):
        # batchReady is no longer emitted; kept for safety
        for s in songs: self._add_or_update(s)
        self._rebuild_cat_filter(); self._apply_filters()

    def _on_song_ready(self, song: Song):
        self._add_or_update(song)
        self._apply_filters()

    def _on_total(self, total: int):
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self._set_info(f"Loading  0 / {total} songs…", "loading", auto_reset=False)

    def _on_progress(self, done: int, total: int, eta: Optional[float], name: str):
        self.progress_bar.setValue(done)
        pct   = int(done / total * 100) if total else 100
        eta_t = f"  –  ETA {_fmt_time(int(eta * 1000))}" if eta else ""
        self._set_info(
            f"Loading  {done} / {total} songs  ({pct}%){eta_t}",
            "loading", auto_reset=False)
        if done % 50 == 0:
            self._rebuild_cat_filter()

    def _on_removed(self, ids: List[str]):
        for sid in ids:
            item = self.row_items.pop(sid, None)
            self.songs_by_id.pop(sid, None)
            if item is not None:
                self.table.removeRow(item.row())
        self._rebuild_cat_filter()

    def _on_scan_error(self, msg: str):
        self.progress_bar.setVisible(False)
        self._set_info(f"Scan error: {msg}", "error", duration_ms=8000)
        self.progress_bar.setVisible(False)

    def _on_finished(self, added: List[str], removed: List[str],
                     moved: int, w: ScanWorker):
        self.progress_bar.setVisible(False)
        self.cache = w.new_cache
        if self.root_path: save_cache(self.root_path, self.cache)
        self._rebuild_cat_filter()
        n = len(self.songs_by_id)
        self.song_count_label.setText(f"{n} songs")
        self._set_info(f"{n} songs  \u2022  Up to date", "success",
                       auto_reset=True, duration_ms=3000)
        if added or removed:
            self.last_added = added; self.last_removed = removed
            self.changes_btn.setEnabled(True)
            self.changes_btn.setVisible(True)
            n_add, n_rem = len(added), len(removed)
            parts = []
            if n_add: parts.append(f"+{n_add} added")
            if n_rem: parts.append(f"-{n_rem} removed")
            if moved:  parts.append(f"{moved} moved")
            self._set_info("Library changed: " + ",  ".join(parts),
                           "warning", auto_reset=True, duration_ms=5000)

    def _show_changes(self):
        lines = []
        if self.last_added:
            lines.append(f"Added ({len(self.last_added)}):")
            lines += [f"  + {t}" for t in self.last_added[:50]]
            if len(self.last_added) > 50:
                lines.append(f"  … and {len(self.last_added)-50} more")
        if self.last_removed:
            if lines: lines.append("")
            lines.append(f"Removed ({len(self.last_removed)}):")
            lines += [f"  - {t}" for t in self.last_removed[:50]]
            if len(self.last_removed) > 50:
                lines.append(f"  … and {len(self.last_removed)-50} more")
        if not lines: lines.append("No recent additions or removals.")
        QMessageBox.information(self, "Recent library changes", "\n".join(lines))
        self.changes_btn.setVisible(False)
        self.changes_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Category filter dropdown
    # ------------------------------------------------------------------
    def _rebuild_cat_filter(self):
        cats = list_categories(self.root_path) if self.root_path else []
        if cats == self._last_cats: return
        self._last_cats = cats

        prev = self.cat_filter.currentText()
        self.cat_filter.blockSignals(True)
        self.cat_filter.clear()
        self.cat_filter.addItem("All")
        for c in cats: self.cat_filter.addItem(c)
        self.cat_filter.setCurrentText(prev if prev in cats else "All")
        self.cat_filter.blockSignals(False)
        self._filter_cat = self.cat_filter.currentText()

        # update every row's combo
        for sid, item in self.row_items.items():
            combo = self.table.cellWidget(item.row(), COL_CATEGORY)
            if combo is None: continue
            cur = combo.currentText()
            combo.blockSignals(True); combo.clear(); combo.addItems(cats)
            if cur in cats: combo.setCurrentText(cur)
            combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Filter handlers
    # ------------------------------------------------------------------
    def _on_search_changed(self, txt: str):
        self._filter_search = txt.strip().lower()
        self._apply_filters()

    def _on_cat_filter_changed(self, txt: str):
        self._filter_cat = txt
        self._apply_filters()

    def _on_rat_filter_changed(self, idx: int):
        self._filter_rating = self.rat_filter.itemData(idx)
        self._apply_filters()

    def _reset_filters(self):
        self.search_box.blockSignals(True);  self.search_box.setText("");          self.search_box.blockSignals(False)
        self.cat_filter.blockSignals(True);  self.cat_filter.setCurrentText("All"); self.cat_filter.blockSignals(False)
        self.rat_filter.blockSignals(True);  self.rat_filter.setCurrentIndex(0);    self.rat_filter.blockSignals(False)
        self._filter_search = ""; self._filter_cat = "All"; self._filter_rating = 0
        self._apply_filters()

    def _matches(self, song: Song) -> bool:
        if self._filter_cat not in ("All", song.category):
            return False
        if self._filter_search and \
                self._filter_search not in song.title.lower() and \
                self._filter_search not in song.artist.lower():
            return False
        if self._filter_rating == -1 and song.rating != 0:
            return False   # "No rating" filter: only unrated songs
        if self._filter_rating > 0 and song.rating < self._filter_rating:
            return False
        return True

    def _apply_filters(self):
        for sid, song in self.songs_by_id.items():
            item = self.row_items.get(sid)
            if item is not None:
                self.table.setRowHidden(item.row(), not self._matches(song))

    def _filtered_songs(self) -> List[Song]:
        return [s for s in self.songs_by_id.values() if self._matches(s)]

    # ------------------------------------------------------------------
    # Play filtered
    # ------------------------------------------------------------------
    def _play_filtered(self):
        songs = self._filtered_songs()
        if not songs:
            self._show_toast("No songs match the current filter selection.", 3000, "warning")
            return
        self.player.play_song(songs[0], queue=songs)

    # ------------------------------------------------------------------
    # Table rows (incremental)
    # ------------------------------------------------------------------
    def _add_or_update(self, song: Song):
        self.songs_by_id[song.id] = song
        item = self.row_items.get(song.id)
        if item is not None:
            row = item.row()
            item.setText(song.title)
            ai = self.table.item(row, COL_ARTIST)
            if ai: ai.setText(song.artist)
            combo = self.table.cellWidget(row, COL_CATEGORY)
            if combo and combo.currentText() != song.category:
                combo.blockSignals(True)
                combo.setCurrentText(song.category)
                combo.blockSignals(False)
            star_wrapper = self.table.cellWidget(row, COL_RATING)
            if star_wrapper:
                star = star_wrapper.findChild(StarRatingWidget)
                if star: star.set_rating(song.rating)
            self.table.setRowHidden(row, not self._matches(song))
        else:
            self._insert_row(song)

    def _insert_row(self, song: Song):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, ROW_HEIGHT)

        ti = QTableWidgetItem(song.title)
        ti.setData(Qt.UserRole, song.id)
        self.table.setItem(row, COL_TITLE,  ti)
        self.table.setItem(row, COL_ARTIST, QTableWidgetItem(song.artist))
        self.row_items[song.id] = ti

        # ── Category combo ──
        cats = list_categories(self.root_path) if self.root_path else []
        combo = QComboBox(); combo.addItems(cats)
        if song.category in cats: combo.setCurrentText(song.category)
        combo.setEnabled(False)
        combo.currentTextChanged.connect(
            lambda nc, s=song, c=combo: self._on_cat_combo(s, c, nc))
        self.table.setCellWidget(row, COL_CATEGORY, combo)

        # ── Category lock ──
        cat_lock = LockButton(
            "Green pen: category is protected. Click to allow editing.",
            "Red pen: editing active. Select a new category to move the file.")
        cat_lock.toggledLock.connect(lambda unlocked, c=combo: c.setEnabled(unlocked))
        self.table.setCellWidget(row, COL_CATEGORY_LOCK, _centered(cat_lock))

        # ── Star rating — locked by default, pencil unlocks editing ──
        star_wrapper = QWidget()
        star_lay = QHBoxLayout(star_wrapper)
        star_lay.setContentsMargins(4, 0, 4, 0)
        star_lay.setSpacing(0)
        star = StarRatingWidget(song.rating)
        star.set_editable(False)   # locked by default
        star.ratingChanged.connect(lambda nr, s=song, sw=star: self._on_rating(s, sw, nr))
        star_lay.addWidget(star)
        star_lay.addStretch()
        self.table.setCellWidget(row, COL_RATING, star_wrapper)

        # ── Rating edit lock ──
        rat_lock = LockButton(
            "Green pen: rating is protected. Click to allow editing.",
            "Red pen: editing active. Click a star to set the rating."
        )
        rat_lock.toggledLock.connect(lambda unlocked, sw=star: sw.set_editable(unlocked))
        self.table.setCellWidget(row, COL_RATING_LOCK, _centered(rat_lock, right_pad=14))

        combo.setProperty("lock_ref", cat_lock)
        star.setProperty("lock_ref", rat_lock)

        self.table.setRowHidden(row, not self._matches(song))

    def _on_double_click(self, index):
        item = self.table.item(index.row(), COL_TITLE)
        if item is None: return
        song = self.songs_by_id.get(item.data(Qt.UserRole))
        if song: self.player.play_song(song, queue=self._filtered_songs())

    def _highlight(self, playing: Optional[Song]):
        if self._highlighted_id:
            prev = self.row_items.get(self._highlighted_id)
            if prev:
                f = prev.font(); f.setBold(False); prev.setFont(f)
                prev.setForeground(Qt.white)
        self._highlighted_id = playing.id if playing else None
        if playing:
            item = self.row_items.get(playing.id)
            if item:
                f = item.font(); f.setBold(True); item.setFont(f)
                item.setForeground(Qt.cyan)

    # ------------------------------------------------------------------
    # Category change → physical move
    # ------------------------------------------------------------------
    def _on_cat_combo(self, song: Song, combo: QComboBox, new_cat: str):
        if new_cat == song.category or not combo.isEnabled(): return
        if self.player.current_song() is song and self.player.is_playing():
            self._show_toast("Pause the song first before moving it to another category.", 3500, "warning")
            combo.blockSignals(True); combo.setCurrentText(song.category); combo.blockSignals(False)
            return

        old_cat, old_path_str = song.category, str(song.path)
        try:
            new_path = safe_move_song(song, self.root_path, new_cat)
        except SafeMoveError as e:
            self._show_toast(f"Move failed: {e}  —  Original was NOT modified.", 6000, "error")
            combo.blockSignals(True); combo.setCurrentText(old_cat); combo.blockSignals(False)
            return

        song.path = new_path; song.category = new_cat
        self.player.notify_song_path_changed(song, new_path)

        rec = self.cache.pop(old_path_str, {})
        try:
            st = new_path.stat()
            rec.update({"size": st.st_size, "mtime": st.st_mtime, "title": song.title,
                        "artist": song.artist, "rating": song.rating,
                        "duration": song.duration, "id": song.id})
            self.cache[str(new_path)] = rec
            if self.root_path: save_cache(self.root_path, self.cache)
        except OSError:
            pass

        lock: LockButton = combo.property("lock_ref")
        if lock: lock.set_locked(True)
        combo.setEnabled(False)
        self.table.setRowHidden(self.row_items[song.id].row(), not self._matches(song))
        self._show_toast(f"✓  '{song.title}'  moved: {old_cat} → {new_cat}", 3500, "success")

    # ------------------------------------------------------------------
    # Rating change → write directly into the audio file
    # ------------------------------------------------------------------
    def _on_rating(self, song: Song, star_widget: "StarRatingWidget", new_rating: int):
        if not write_rating(song.path, new_rating):
            self._show_toast(
                f"Could not write rating into '{song.filename}' — format may not support it.",
                5000, "error")
            return
        song.rating = new_rating

        # Keep cache in sync so next scan doesn't re-read this file
        try:
            st  = song.path.stat()
            rec = self.cache.get(str(song.path), {})
            rec.update({"size": st.st_size, "mtime": st.st_mtime,
                        "title": song.title, "artist": song.artist,
                        "rating": new_rating, "duration": song.duration,
                        "id": song.id})
            self.cache[str(song.path)] = rec
            if self.root_path: save_cache(self.root_path, self.cache)
        except OSError:
            pass

        # Auto re-lock the rating pen after saving
        lock: LockButton = star_widget.property("lock_ref")
        if lock:
            lock.set_locked(True)
        star_widget.set_editable(False)

        # Re-evaluate row visibility (rating filter may now hide/show this song)
        item = self.row_items.get(song.id)
        if item:
            self.table.setRowHidden(item.row(), not self._matches(song))

        # Inline toast — rating saved directly into the file
        stars = "★" * new_rating + "☆" * (5 - new_rating)
        if new_rating == 0:
            self._show_toast(f"Rating cleared  —  {song.title}", 3000, "info")
        else:
            self._show_toast(
                f"✓  Rating saved into file:  {song.title}   {stars}  ({new_rating}/5)",
                3500, "success")


# ============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()