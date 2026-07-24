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

ORG_NAME = "WavePro"
APP_NAME  = "Wave Pro - Music Studio"

# ============================================================================
# Metadata helpers
# ============================================================================
RATING_TXXX_DESC = "RATING"
MP4_RATING_ATOM  = "----:com.apple.iTunes:RATING"
SUPPORTED_EXTENSIONS = {".mp3",".flac",".ogg",".m4a",".mp4",".wav",".wma",".aac",".oga",".txt"}
TODO_EXTENSIONS      = {".txt"}   # placeholder files, not playable


def read_display_tags(path: Path) -> Tuple[str, str]:
    """
    Title  = ALWAYS the physical filename (without extension).
             This ensures the app always shows exactly what is on disk.
             Renaming the file in the OS immediately changes the title.
    Artist = from embedded audio tags (ID3 / Vorbis / MP4).
    """
    title  = path.stem   # always the physical filename
    artist = ""
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.tags:
            a = audio.tags.get("artist")
            if a:
                artist = a[0]
    except Exception:
        pass
    return title, artist


def write_display_tags(path: Path, title: str, artist: str) -> Tuple[bool, Path]:
    """
    Title  → physically RENAMES the file on disk (title = filename).
    Artist → writes the artist tag into the audio file's metadata.

    Returns (success, new_path). new_path may differ from the input path
    if the file was renamed (title changed).
    """
    new_path = path
    try:
        # ── Rename file if title changed ──
        current_stem = path.stem
        if title and title != current_stem:
            new_name = title + path.suffix
            candidate = path.parent / new_name
            # Avoid overwriting an existing file
            if candidate.exists() and candidate.resolve() != path.resolve():
                counter = 1
                while candidate.exists():
                    candidate = path.parent / f"{title} ({counter}){path.suffix}"
                    counter += 1
            os.rename(path, candidate)
            new_path = candidate

        # ── Write artist tag ──
        audio = MutagenFile(new_path, easy=True)
        if audio is not None:
            if audio.tags is None:
                audio.add_tags()
            audio.tags["artist"] = [artist]
            audio.save()

        return True, new_path
    except Exception:
        return False, path


def read_duration_seconds(path: Path) -> float:
    try:
        audio = MutagenFile(path)
        if audio and audio.info and hasattr(audio.info, "length"):
            return float(audio.info.length)
    except Exception:
        pass
    return 0.0


def read_rating(path: Path) -> int:
    """Read rating (0-5) from audio file metadata.

    For MP3: reads POPM frame first (what Windows Properties writes),
    then falls back to TXXX:RATING (what this app writes).
    This ensures ratings set via Windows Properties > Details > Rating
    are correctly displayed in the app.
    """
    # POPM byte → star mapping (Windows standard)
    def _popm_to_stars(byte_val: int) -> int:
        if byte_val == 0:   return 0
        if byte_val <= 31:  return 1
        if byte_val <= 95:  return 2
        if byte_val <= 159: return 3
        if byte_val <= 223: return 4
        return 5

    ext = path.suffix.lower()
    try:
        if ext in (".mp3", ".wav", ".aac"):
            try:
                id3 = ID3(path)
            except ID3NoHeaderError:
                return 0
            # First try POPM (Windows Properties writes this)
            popm_frames = id3.getall("POPM")
            if popm_frames:
                return _popm_to_stars(popm_frames[0].rating)
            # Fall back to TXXX:RATING (this app's custom tag)
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
    """Write rating (0-5) directly into the audio file's own metadata.

    For MP3 files, writes BOTH:
      - TXXX:RATING  (text "0"-"5", used by this app)
      - POPM         (Popularimeter, 0-255 byte, used by Windows Properties)

    Windows star-to-byte mapping:
      0 stars = 0,  1 star = 1,  2 stars = 64,
      3 stars = 128,  4 stars = 196,  5 stars = 255
    """
    rating = _clamp(rating)
    ext = path.suffix.lower()

    # Windows POPM byte values for each star level
    _STAR_TO_POPM = {0: 0, 1: 1, 2: 64, 3: 128, 4: 196, 5: 255}

    try:
        if ext in (".mp3", ".wav", ".aac"):
            try:
                id3 = ID3(path)
            except ID3NoHeaderError:
                id3 = ID3()
            # Remove old TXXX:RATING
            for f in [f for f in id3.getall("TXXX")
                      if getattr(f, "desc", "").upper() == RATING_TXXX_DESC]:
                id3.delall(f"TXXX:{f.desc}")
            # Write TXXX:RATING (for this app)
            id3.add(TXXX(encoding=3, desc=RATING_TXXX_DESC, text=[str(rating)]))
            # Write POPM (for Windows Properties > Details > Rating)
            from mutagen.id3 import POPM
            id3.delall("POPM")
            id3.add(POPM(email="Windows Media Player 9 Series",
                         rating=_STAR_TO_POPM.get(rating, 0), count=0))
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

        # ── Phase 1: cached songs (no tag reading for most fields, very fast)
        # IMPORTANT: title and artist are always re-read from the file, even
        # for "unchanged" files. This guarantees the app always shows what is
        # physically inside the audio file, regardless of whether the tags were
        # changed externally (by another app, OS, etc.).
        # Only rating and duration are taken from cache (they are either written
        # by this app or rarely change externally).
        for p, _ in fast_items:
            cat, size, mtime = current[p]
            rec = old.get(p)
            if rec is None:
                for old_p, new_p in moved_pairs:
                    if new_p == p:
                        rec = old.get(old_p, {}); break
            if rec is None:
                rec = {}
            path_obj = Path(p)
            # Always re-read title/artist from file
            title, artist = read_display_tags(path_obj)
            # Always re-read rating from file so changes made via Windows
            # Properties > Details > Rating are immediately visible.
            # Only duration is taken from cache (expensive, rarely changes).
            rating   = read_rating(path_obj)
            duration = rec.get("duration", 0.0)
            song = Song(
                path=path_obj, category=cat,
                title=title, artist=artist,
                rating=rating, duration=duration,
                id=rec.get("id") or uuid.uuid4().hex,
            )
            new_cache[p] = {**rec, "size": size, "mtime": mtime,
                            "title": title, "artist": artist, "id": song.id}
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
        start_idx = self._index
        attempts = 0
        while attempts < len(self._queue):
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
            # Skip .txt placeholder files
            song = self.current_song()
            if song and song.path.suffix.lower() not in TODO_EXTENSIONS:
                self._load(autoplay=True); return
            attempts += 1
        # All songs are .txt — nothing to play
        self._player.stop(); self.songChanged.emit(None)

    def previous(self):
        if not self._queue: return
        if self._player.position() > 3000:
            self._player.setPosition(0); return
        attempts = 0
        while attempts < len(self._queue):
            if self._shuffle:
                import random
                candidates = [i for i in range(len(self._queue)) if i != self._index]
                if candidates:
                    self._index = random.choice(candidates)
                else:
                    self._index = 0
            elif self._repeat == RepeatMode.REVERSE:
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
            # Skip .txt placeholder files
            song = self.current_song()
            if song and song.path.suffix.lower() not in TODO_EXTENSIONS:
                self._load(autoplay=True); return
            attempts += 1
        self._player.stop(); self.songChanged.emit(None)

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
        # .txt placeholder files can't play — skip silently
        if song.path.suffix.lower() in TODO_EXTENSIONS:
            self.songChanged.emit(song)   # update UI
            return   # caller (next/prev) handles skipping
        self._player.setSource(QUrl.fromLocalFile(str(song.path)))
        self.songChanged.emit(song)
        if autoplay: self._player.play()

    def _on_state(self, state):
        self.playbackStateChanged.emit(state == QMediaPlayer.PlayingState)

    def _on_media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.next()


# ============================================================================
# ============================================================================
# Star-colored combo box delegate

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
            base = rng.uniform(0.12, 0.55)

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
QHeaderView::section:horizontal:hover { background:#2c2c2e; color:#f2f2f7; }

/* Row number gutter (vertical header) — like PyCharm line numbers */
QHeaderView::section:vertical {
    background: #1c1c1e; color: #48484a; font-size: 10px; font-weight: 400;
    font-family: "SF Mono","Consolas",monospace; border: none;
    border-right: 1px solid #2c2c2e; padding: 0px 2px; }

/* Corner widget (top-left where row numbers meet column headers) */
QTableCornerButton::section {
    background: #1c1c1e; border: none;
    border-bottom: 1px solid #2c2c2e; border-right: 1px solid #2c2c2e; }

/* Sort indicator — handled via text ▲/▼ appended to header label */

QTableWidget::item:selected { background:rgba(10,132,255,.22); }

QComboBox {
    background:#2c2c2e; border:1px solid #3a3a3c; border-radius:8px;
    padding:4px 10px; color:#f2f2f7; }
QComboBox:disabled { color:#5a5a5e; background:#232325; }
QComboBox::drop-down { border:none; width:18px; }
QComboBox QAbstractItemView {
    background:#2c2c2e; border:1px solid #3a3a3c;
    selection-background-color:#0a84ff; outline:none; }
QComboBox QAbstractItemView::item {
    color:#f2f2f7; padding:4px 8px; min-height:22px; }

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

/* Quick-filter toggle buttons (Collab / Cover) */
QPushButton#quickFilterBtn {
    background: #2c2c2e;
    border: 1px solid #3a3a3c;
    border-radius: 8px;
    color: #8e8e93;
    font-size: 12px;
    font-weight: 500;
    padding: 0px 12px;
}
QPushButton#quickFilterBtn:hover { background: #3a3a3c; color: #f2f2f7; }
QPushButton#quickFilterBtn:checked {
    background: rgba(10,132,255,0.18);
    border-color: #0a84ff;
    color: #0a84ff;
    font-weight: 600;
}

/* Inline cell editor */
QLineEdit#cellEditor {
    background: #1c1c1e;
    border: 1px solid #0a84ff;
    border-radius: 4px;
    color: #f2f2f7;
    font-size: 13px;
    padding: 2px 6px;
}

/* Bulk edit bar — visually separated from filters */
QFrame#bulkEditBar {
    background: #232325;
    border-top: 1px solid #2c2c2e;
    border-bottom: 1px solid #2c2c2e;
}
QLabel#bulkEditTitle {
    color: #0a84ff;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
}

QFrame#playerBar { background:#232325; border-top:1px solid #2c2c2e; }
QFrame#navStrip  { background:#1c1c1e; border-top:1px solid #2c2c2e;
                   border-bottom:1px solid #2c2c2e; }

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


def _split_title(title: str) -> Tuple[str, str]:
    """Split a song title at the first ' - ' into (artist, song_name).
    If no dash found, returns ('', title)."""
    if " - " in title:
        parts = title.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return "", title


# ============================================================================
# Column indices
# ============================================================================
COL_ARTIST        = 0
COL_SONGNAME      = 1
COL_NAME_EDIT     = 2
COL_CATEGORY      = 3
COL_CATEGORY_LOCK = 4
COL_RATING        = 5
COL_RATING_LOCK   = 6
COL_DELETE        = 7
COL_COUNT         = 8

ROW_HEIGHT = 38   # px – fits 20px lock icons with comfortable padding


# ============================================================================
# Main window
# ============================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Wave Pro - Music Studio  |  Developed by Ivan Sicaja \u00a9 2026. All rights reserved.")
        self.resize(1300, 820)

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
        self._filter_collab = False  # show only collab songs
        self._filter_cover  = False  # show only cover songs

        self.scan_worker: Optional[ScanWorker] = None
        self.pending_rescan = False
        self.last_added:   List[str] = []
        self.last_removed: List[str] = []

        self.player = PlayerController(self)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._on_dir_changed)
        self.watcher.fileChanged.connect(self._on_file_changed)
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(600)
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
        vlay.addWidget(self._build_nav_strip())
        vlay.addWidget(self._build_player_bar())

    def _build_nav_strip(self) -> QWidget:
        """Strip between table and player: To-Do (left), scroll buttons (right)."""
        strip = QFrame()
        strip.setObjectName("navStrip")
        strip.setFixedHeight(32)
        lay = QHBoxLayout(strip)
        lay.setContentsMargins(14, 2, 14, 2)
        lay.setSpacing(6)

        def _nav_btn(text: str, tooltip: str, w: int = 28) -> QToolButton:
            btn = QToolButton()
            btn.setText(text)
            btn.setToolTip(tooltip)
            btn.setFixedSize(w, 24)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                "QToolButton{background:#2c2c2e;border:none;border-radius:6px;"
                "color:#8e8e93;font-size:12px;font-weight:600;}"
                "QToolButton:hover{background:#3a3a3c;color:#f2f2f7;}"
                "QToolButton:pressed{background:#232325;}"
            )
            return btn

        # ── To-Do on the LEFT ──
        add_todo_btn = QToolButton()
        add_todo_btn.setText("+ To-Do")
        add_todo_btn.setToolTip("Create a .txt placeholder for a song to download later")
        add_todo_btn.setFixedHeight(24)
        add_todo_btn.setCursor(Qt.PointingHandCursor)
        add_todo_btn.setStyleSheet(
            "QToolButton{background:rgba(10,132,255,0.12);border:1px solid rgba(10,132,255,0.3);"
            "border-radius:6px;color:#0a84ff;font-size:11px;font-weight:600;padding:0 10px;}"
            "QToolButton:hover{background:rgba(10,132,255,0.22);border-color:#0a84ff;}"
        )
        add_todo_btn.clicked.connect(self._add_todo)
        lay.addWidget(add_todo_btn)

        lay.addStretch()

        # ── Scroll buttons on the RIGHT ──
        top_btn = _nav_btn("\u2912 Top", "Scroll to top of list", 50)
        top_btn.clicked.connect(self._scroll_to_top)
        lay.addWidget(top_btn)

        self.focus_btn = _nav_btn("\u25ce Now", "Scroll to currently playing song", 58)
        self.focus_btn.clicked.connect(self._scroll_to_playing)
        lay.addWidget(self.focus_btn)

        bottom_btn = _nav_btn("\u2913 End", "Scroll to bottom of list", 50)
        bottom_btn.clicked.connect(self._scroll_to_bottom)
        lay.addWidget(bottom_btn)

        return strip

    # ── top bar (compact, everything inline) ─────────────────────────
    def _build_top_bar(self) -> QWidget:
        wrapper = QWidget(); wrapper.setObjectName("topBar")
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Main bar row
        bar = QFrame(); bar.setObjectName("topBarInner")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(10)

        title = QLabel("Wave Pro")
        title.setStyleSheet("font-size:22px;font-weight:700;color:#fff;padding:2px 0;")
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
        wrapper = QWidget()
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QFrame(); bar.setObjectName("filterBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 12, 8); lay.setSpacing(8)

        # Search
        search_icon = QLabel("🔍"); search_icon.setObjectName("subtleLabel")
        lay.addWidget(search_icon)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search by title…")
        self.search_box.setFixedWidth(230)
        self.search_box.textChanged.connect(self._on_search_changed)
        lay.addWidget(self.search_box)

        lay.addSpacing(16)

        # Category filter — checkbox-based multi-select
        cat_lbl = QLabel("Category:"); cat_lbl.setObjectName("subtleLabel")
        lay.addWidget(cat_lbl)
        self.cat_filter_btn = QPushButton("All")
        self.cat_filter_btn.setFixedWidth(155)
        self.cat_filter_btn.setToolTip("Click to select/deselect categories")
        self.cat_filter_btn.clicked.connect(self._show_cat_checkboxes)
        lay.addWidget(self.cat_filter_btn)
        self._cat_checks: Dict[str, bool] = {}   # category → checked

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
            self.rat_filter.setItemData(
                self.rat_filter.count() - 1, QColor("#FFD60A"), Qt.ForegroundRole)
        self.rat_filter.currentIndexChanged.connect(self._on_rat_filter_changed)
        lay.addWidget(self.rat_filter)

        lay.addSpacing(16)

        # ── Quick-filter toggle buttons: Collab / Cover ───────────────
        def _toggle_btn(label: str, tooltip: str) -> QPushButton:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setToolTip(tooltip)
            btn.setObjectName("quickFilterBtn")
            btn.setFixedHeight(28)
            return btn

        self.collab_btn = _toggle_btn(
            "Collab", "Show only songs whose title contains 'collab' (case-insensitive)")
        self.collab_btn.toggled.connect(self._on_collab_toggled)
        lay.addWidget(self.collab_btn)

        self.cover_btn = _toggle_btn(
            "Cover", "Show only songs whose title contains 'cover' (case-insensitive)")
        self.cover_btn.toggled.connect(self._on_cover_toggled)
        lay.addWidget(self.cover_btn)

        lay.addSpacing(16)

        # ── Play filtered button ──────────────────────────────────────
        self.play_filtered_btn = QPushButton("\u25b6  Play Filtered")
        self.play_filtered_btn.setObjectName("playFilteredButton")
        self.play_filtered_btn.setToolTip(
            "Play all songs currently visible (respects active filters)")
        self.play_filtered_btn.clicked.connect(self._play_filtered)
        lay.addWidget(self.play_filtered_btn)

        lay.addStretch()

        reset_btn = QPushButton("Reset filters")
        reset_btn.clicked.connect(self._reset_filters)
        lay.addWidget(reset_btn)

        # ── Bulk Edit row (separate line above filters) ──────────────
        bulk_bar = QFrame()
        bulk_bar.setObjectName("bulkEditBar")
        bulk_lay = QHBoxLayout(bulk_bar)
        bulk_lay.setContentsMargins(12, 4, 12, 4)
        bulk_lay.setSpacing(10)

        bulk_title = QLabel("BULK EDIT")
        bulk_title.setObjectName("bulkEditTitle")
        bulk_lay.addWidget(bulk_title)

        sep = QFrame(); sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet("color:#3a3a3c;"); sep.setFixedHeight(20)
        bulk_lay.addWidget(sep)

        self.bulk_cat_combo = QComboBox()
        self.bulk_cat_combo.setFixedWidth(140)
        self.bulk_cat_combo.addItem("Move to\u2026")
        self.bulk_cat_combo.setToolTip("Move ALL selected songs to this category")
        self.bulk_cat_combo.setEnabled(False)
        self.bulk_cat_combo.activated.connect(self._on_bulk_category)
        bulk_lay.addWidget(self.bulk_cat_combo)

        self.bulk_rat_combo = QComboBox()
        self.bulk_rat_combo.setFixedWidth(160)
        self.bulk_rat_combo.addItem("Set rating\u2026", -99)
        for n in range(6):
            stars = "\u2605" * n + "\u2606" * (5 - n) if n else "\u2606\u2606\u2606\u2606\u2606  Clear"
            self.bulk_rat_combo.addItem(f"{stars}  ({n})", n)
            if n > 0:
                self.bulk_rat_combo.setItemData(
                    self.bulk_rat_combo.count() - 1, QColor("#FFD60A"), Qt.ForegroundRole)

        self.bulk_rat_combo.setToolTip("Set rating for ALL selected songs")
        self.bulk_rat_combo.setEnabled(False)
        self.bulk_rat_combo.activated.connect(self._on_bulk_rating)
        bulk_lay.addWidget(self.bulk_rat_combo)

        self.bulk_status = QLabel("Select 2+ songs to enable")
        self.bulk_status.setObjectName("subtleLabel")
        bulk_lay.addWidget(self.bulk_status)

        bulk_lay.addStretch()

        # Add bulk bar FIRST (above), then filter bar (closer to songs)
        outer.addWidget(bulk_bar)
        outer.addWidget(bar)
        return wrapper

    # ── song table ────────────────────────────────────────────────────
    def _build_table(self) -> QWidget:
        self.table = QTableWidget(0, COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            ["Artist", "Song Name", "Edit", "Category", "Edit", "Rating", "Edit", ""])
        # Row numbers (queue numbers) — always visible on the left
        self.table.verticalHeader().setVisible(True)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.verticalHeader().setFixedWidth(42)
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.table.verticalHeader().setSectionsClickable(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # Enable sorting by clicking column headers
        self.table.setSortingEnabled(True)
        self.table.sortItems(COL_ARTIST, Qt.AscendingOrder)
        h = self.table.horizontalHeader()
        h.setSortIndicatorShown(False)  # we handle it via text ▲/▼
        h.sortIndicatorChanged.connect(self._on_sort_changed)
        self._base_headers = ["Artist", "Song Name", "Edit", "Category", "Edit", "Rating", "Edit", ""]
        # Set initial sort arrow
        self.table.horizontalHeaderItem(COL_ARTIST).setText("Artist \u25bc")
        h.setSectionResizeMode(COL_ARTIST,        QHeaderView.Stretch)
        h.setSectionResizeMode(COL_SONGNAME,      QHeaderView.Stretch)
        h.setSectionResizeMode(COL_NAME_EDIT,     QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_CATEGORY,      QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_CATEGORY_LOCK, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_RATING,        QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_RATING_LOCK,   QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_DELETE,        QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
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

        ctrl.addSpacing(12)

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

    # ------------------------------------------------------------------
    # Navigation: Focus / Scroll Top / Scroll Bottom
    # ------------------------------------------------------------------
    _saved_scroll_pos: int = -1

    def _scroll_to_playing(self):
        """Toggle: scroll to currently playing song (without changing selection),
        or return to the previous scroll position if pressed again."""
        current = self.player.current_song()
        if current is None:
            return
        item = self.row_items.get(current.id)
        if item is None:
            return

        current_scroll = self.table.verticalScrollBar().value()
        target_row = item.row()

        if self._saved_scroll_pos >= 0:
            # Return to saved position
            self.table.verticalScrollBar().setValue(self._saved_scroll_pos)
            self._saved_scroll_pos = -1
            self.focus_btn.setStyleSheet(
                "QToolButton{background:#2c2c2e;border:none;border-radius:15px;"
                "color:#8e8e93;font-size:14px;font-weight:600;}"
                "QToolButton:hover{background:#3a3a3c;color:#ffffff;}"
            )
        else:
            # Save current position and scroll to playing song
            self._saved_scroll_pos = current_scroll
            self.table.scrollToItem(item, QAbstractItemView.PositionAtCenter)
            self.focus_btn.setStyleSheet(
                "QToolButton{background:#1a3a5c;border:none;border-radius:15px;"
                "color:#0a84ff;font-size:14px;font-weight:700;}"
                "QToolButton:hover{background:#234a70;}"
            )

    def _scroll_to_top(self):
        """Scroll to the first visible row."""
        self.table.scrollToTop()

    def _scroll_to_bottom(self):
        """Scroll to the last visible row."""
        self.table.scrollToBottom()

    # ------------------------------------------------------------------
    # To-Do placeholder & Delete
    # ------------------------------------------------------------------
    def _add_todo(self):
        """Create a .txt placeholder in a 'To-Do' subfolder."""
        if not self.root_path:
            self._show_toast("Open a music folder first.", 3000, "warning")
            return

        # Block watcher to prevent duplicate detection
        self.watcher.blockSignals(True)

        # Auto-create To-Do subfolder if it doesn't exist
        todo_dir = self.root_path / "To-Do"
        if not todo_dir.exists():
            try:
                todo_dir.mkdir(parents=True, exist_ok=True)
                self.watcher.addPath(str(todo_dir))
                self._rebuild_cat_filter()
            except OSError as e:
                self.watcher.blockSignals(False)
                self._show_toast(f"Could not create To-Do folder: {e}", 5000, "error")
                return

        # Build autocomplete word list from existing song titles
        existing_titles = [s.title for s in self.songs_by_id.values()]

        # ── Custom dialog: name input + autocomplete + star rating ────
        from PySide6.QtWidgets import QDialog, QCompleter
        dlg = QDialog(self)
        dlg.setWindowTitle("Add To-Do")
        dlg.setFixedWidth(650)
        dlg_lay = QVBoxLayout(dlg)
        dlg_lay.setContentsMargins(20, 16, 20, 16)
        dlg_lay.setSpacing(12)

        lbl = QLabel("Song name to download later:")
        lbl.setStyleSheet("font-size:13px;")
        dlg_lay.addWidget(lbl)

        name_input = QLineEdit()
        name_input.setPlaceholderText("Type song name...")
        name_input.setMinimumHeight(32)
        # Autocomplete from existing titles (case-insensitive)
        completer = QCompleter(existing_titles, dlg)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setMaxVisibleItems(5)
        name_input.setCompleter(completer)
        dlg_lay.addWidget(name_input)

        # No rating for To-Do items (they are placeholders, not songs)

        # OK / Cancel buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("Create To-Do")
        ok_btn.setObjectName("accentButton")
        ok_btn.clicked.connect(dlg.accept)
        ok_btn.setDefault(True)
        btn_row.addWidget(ok_btn)
        dlg_lay.addLayout(btn_row)

        name_input.setFocus()
        if not dlg.exec():
            self.watcher.blockSignals(False)
            return

        name = name_input.text().strip()
        if not name:
            self.watcher.blockSignals(False)
            return
        rat_val = 0  # To-Do items have no rating

        # Create exactly ONE .txt placeholder file
        txt_path = todo_dir / f"{name}.txt"
        counter = 1
        while txt_path.exists():
            txt_path = todo_dir / f"{name} ({counter}).txt"
            counter += 1
        txt_path.write_text(f"To-Do: {name}\n", encoding="utf-8")

        # Add to table immediately (single entry, no watcher duplicate)
        song = Song(
            path=txt_path, category="To-Do",
            title=txt_path.stem, artist="",
            rating=rat_val, duration=0.0)
        self._add_or_update(song)

        # Cache
        st = txt_path.stat()
        self.cache[str(txt_path)] = {
            "size": st.st_size, "mtime": st.st_mtime,
            "title": song.title, "artist": "", "rating": rat_val,
            "duration": 0.0, "id": song.id}
        if self.root_path:
            save_cache(self.root_path, self.cache)

        self._rebuild_cat_filter()
        self._apply_filters()

        # Unblock watcher and cancel any pending rescans
        self.watcher.blockSignals(False)
        self._rescan_timer.stop()
        self._show_toast(f"\u2713  To-Do added: \"{name}\"", 3000, "success")
        self._log_change("TODO", name, "To-Do placeholder created")

        self._rebuild_cat_filter()
        self._apply_filters()
        self._show_toast(f"\u2713  To-Do added: \"{name}\"", 3000, "success")

    def _delete_song(self, song: Song):
        """Delete the physical file and remove the song from the table."""
        # Confirm deletion
        reply = QMessageBox.question(
            self, "Delete file",
            f"Permanently delete this file?\n\n{song.path.name}\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        # Stop playback if this song is playing
        if self.player.current_song() is song:
            self.player._player.stop()

        # Delete the physical file
        try:
            os.remove(song.path)
        except OSError as e:
            self._show_toast(f"Could not delete: {e}", 5000, "error")
            return

        # Remove from cache
        self.cache.pop(str(song.path), None)
        if self.root_path:
            save_cache(self.root_path, self.cache)

        # Remove from table
        item = self.row_items.pop(song.id, None)
        self.songs_by_id.pop(song.id, None)
        if item is not None:
            self.table.removeRow(item.row())

        self._update_queue_numbers()
        self._rebuild_play_queue()
        self._show_toast(f"\u2713  Deleted: {song.title}", 3000, "success")
        self._log_change("DEL", song.title, str(song.path))

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

    def _on_file_changed(self, changed_path: str):
        """Called when an individual audio file changes on disk.
        Re-adds it to the watcher (some OS remove it after a change)
        and triggers a rescan so tags are refreshed."""
        # Re-watch the file (some filesystems stop watching after a modify)
        if Path(changed_path).exists():
            self.watcher.addPath(changed_path)
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
        self.watcher.addPath(str(song.path))
        # Keep UI responsive — process pending events every 25 songs
        self._song_ready_count = getattr(self, '_song_ready_count', 0) + 1
        if self._song_ready_count % 25 == 0:
            QApplication.processEvents()

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
        """Show change history from CSV log."""
        log_path = self._changelog_path()
        lines = []
        if log_path.is_file():
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    rows = f.readlines()[-100:]
                lines = [r.strip() for r in rows if r.strip()]
            except Exception:
                pass
        if not lines:
            lines = ["No changes recorded yet."]

        display = "CHANGE HISTORY (last 100 entries)\n" + "=" * 50 + "\n\n"
        icons = {"ADD": "+", "DEL": "\u2716", "MOVE": "\u2192",
                 "RATING": "\u2605", "RENAME": "\u270E", "TODO": "\u2610"}
        for line in reversed(lines):
            parts = line.split(",", 3)
            if len(parts) >= 3:
                ts, action, title = parts[0], parts[1], parts[2]
                detail = parts[3] if len(parts) > 3 else ""
                icon = icons.get(action, "\u2022")
                display += f"{ts}  {icon} [{action}]  {title}"
                if detail: display += f"  \u2014  {detail}"
                display += "\n"
            else:
                display += line + "\n"

        QMessageBox.information(self, "Change History", display)

    def _changelog_path(self) -> Path:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        if not base: base = str(Path.home() / ".music_library_app")
        bp = Path(base); bp.mkdir(parents=True, exist_ok=True)
        return bp / "changelog.csv"

    def _log_change(self, action: str, title: str, detail: str = ""):
        """Append to CSV changelog. Actions: ADD, DEL, MOVE, RATING, RENAME, TODO"""
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self._changelog_path(), "a", encoding="utf-8") as f:
                f.write(f"{ts},{action},{title},{detail}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Category filter dropdown
    # ------------------------------------------------------------------
    def _rebuild_cat_filter(self):
        cats = list_categories(self.root_path) if self.root_path else []
        if cats == self._last_cats: return
        self._last_cats = cats

        # Initialize checkboxes — all checked by default (= "All")
        for c in cats:
            if c not in self._cat_checks:
                self._cat_checks[c] = True
        # Remove categories that no longer exist
        for c in list(self._cat_checks):
            if c not in cats:
                del self._cat_checks[c]

        self._update_cat_btn_label()

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

    def _on_collab_toggled(self, checked: bool):
        self._filter_collab = checked
        self._apply_filters()

    def _on_cover_toggled(self, checked: bool):
        self._filter_cover = checked
        self._apply_filters()

    def _show_cat_checkboxes(self):
        """Category filter popup with two selection modes:
        1. Click category NAME → select only that one (instant, closes popup)
        2. Use CHECKBOXES → multi-select, then click button to apply
        """
        from PySide6.QtWidgets import QMenu, QWidgetAction, QCheckBox

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#2c2c2e;border:1px solid #3a3a3c;"
            "border-radius:8px;padding:6px 0;}")

        cat_cbs: list = []
        self._pending_cat_change = False

        def _on_checkbox_toggled():
            """When any checkbox is toggled, signal that Apply is needed."""
            self._pending_cat_change = True
            self.cat_filter_btn.setText("Apply Selection")
            self.cat_filter_btn.setStyleSheet(
                "QPushButton{background:rgba(10,132,255,0.25);"
                "border:1.5px solid #0a84ff;border-radius:8px;"
                "color:#0a84ff;font-size:12px;font-weight:700;"
                "padding:4px 10px;}")

        def _select_only(cat_name: str):
            """Click on name = select ONLY this category, close popup."""
            for c in self._cat_checks:
                self._cat_checks[c] = (c == cat_name)
            self._pending_cat_change = False
            menu.close()

        # ── "All" row: checkbox + clickable text ──
        all_w = QWidget()
        all_lay = QHBoxLayout(all_w)
        all_lay.setContentsMargins(10, 3, 10, 3)
        all_lay.setSpacing(6)

        all_cb = QCheckBox()
        all_checked = all(self._cat_checks.get(c, True) for c in self._last_cats)
        all_cb.setChecked(all_checked)
        all_cb.setStyleSheet(
            "QCheckBox::indicator{width:14px;height:14px;border-radius:3px;"
            "border:1px solid #5a5a5e;background:#1c1c1e;}"
            "QCheckBox::indicator:checked{background:#0a84ff;border-color:#0a84ff;}")
        all_lay.addWidget(all_cb)

        all_lbl = QPushButton("All Categories")
        all_lbl.setStyleSheet(
            "QPushButton{background:transparent;border:none;color:#0a84ff;"
            "font-size:12px;font-weight:600;text-align:left;padding:2px;}"
            "QPushButton:hover{color:#3399ff;}")
        all_lbl.setCursor(Qt.PointingHandCursor)
        all_lbl.clicked.connect(lambda: (
            [cb.setChecked(True) for _, cb in cat_cbs],
            _on_checkbox_toggled()))
        all_lay.addWidget(all_lbl, stretch=1)

        wa_all = QWidgetAction(menu)
        wa_all.setDefaultWidget(all_w)
        menu.addAction(wa_all)
        menu.addSeparator()

        # ── Category rows ──
        for cat in self._last_cats:
            row_w = QWidget()
            row_lay = QHBoxLayout(row_w)
            row_lay.setContentsMargins(10, 3, 10, 3)
            row_lay.setSpacing(6)

            cb = QCheckBox()
            cb.setChecked(self._cat_checks.get(cat, True))
            cb.setStyleSheet(
                "QCheckBox::indicator{width:14px;height:14px;border-radius:3px;"
                "border:1px solid #5a5a5e;background:#1c1c1e;}"
                "QCheckBox::indicator:checked{background:#0a84ff;border-color:#0a84ff;}")
            cb.toggled.connect(lambda _: _on_checkbox_toggled())
            row_lay.addWidget(cb)

            lbl = QPushButton(cat)
            lbl.setStyleSheet(
                "QPushButton{background:transparent;border:none;color:#f2f2f7;"
                "font-size:12px;text-align:left;padding:2px 4px;}"
                "QPushButton:hover{color:#0a84ff;}")
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.clicked.connect(lambda _=False, c=cat: _select_only(c))
            row_lay.addWidget(lbl, stretch=1)

            cat_cbs.append((cat, cb))
            wa = QWidgetAction(menu)
            wa.setDefaultWidget(row_w)
            menu.addAction(wa)

        # Sync "All" checkbox with individual checkboxes
        def _sync_all():
            all_cb.blockSignals(True)
            all_cb.setChecked(all(cb.isChecked() for _, cb in cat_cbs))
            all_cb.blockSignals(False)
        for _, cb in cat_cbs:
            cb.toggled.connect(lambda _: _sync_all())
        all_cb.toggled.connect(lambda state: (
            [cb.setChecked(state) for _, cb in cat_cbs],
            _on_checkbox_toggled()))

        menu.exec(self.cat_filter_btn.mapToGlobal(
            self.cat_filter_btn.rect().bottomLeft()))

        # After popup closes: read checkbox states if multi-select was used
        if self._pending_cat_change:
            for cat, cb in cat_cbs:
                self._cat_checks[cat] = cb.isChecked()

        # Reset button style
        self.cat_filter_btn.setStyleSheet("")
        self._update_cat_btn_label()
        self._apply_filters()

    def _update_cat_btn_label(self):
        checked = [c for c, v in self._cat_checks.items() if v]
        if len(checked) == len(self._last_cats) or not checked:
            self.cat_filter_btn.setText("All")
        elif len(checked) == 1:
            self.cat_filter_btn.setText(checked[0])
        else:
            self.cat_filter_btn.setText(f"{len(checked)} selected")

    def _on_rat_filter_changed(self, idx: int):
        self._filter_rating = self.rat_filter.itemData(idx)
        self._apply_filters()

    def _reset_filters(self):
        self.search_box.blockSignals(True);  self.search_box.setText("");          self.search_box.blockSignals(False)
        self.rat_filter.blockSignals(True);  self.rat_filter.setCurrentIndex(0);    self.rat_filter.blockSignals(False)
        self.collab_btn.blockSignals(True);  self.collab_btn.setChecked(False);     self.collab_btn.blockSignals(False)
        self.cover_btn.blockSignals(True);   self.cover_btn.setChecked(False);      self.cover_btn.blockSignals(False)
        self._filter_search = ""; self._filter_rating = 0
        self._filter_collab = False; self._filter_cover = False
        # Reset all category checkboxes to checked
        for c in self._cat_checks:
            self._cat_checks[c] = True
        self._update_cat_btn_label()
        self._apply_filters()

    def _matches(self, song: Song) -> bool:
        # Category checkbox filter
        checked_cats = [c for c, v in self._cat_checks.items() if v]
        if checked_cats and len(checked_cats) < len(self._last_cats):
            if song.category not in checked_cats:
                return False
        if self._filter_search:
            artist_part, song_part = _split_title(song.title)
            combined = song.title.lower()
            if self._filter_search not in combined:
                return False
        if self._filter_rating == -1 and song.rating != 0:
            return False   # "No rating" filter: only unrated songs
        if self._filter_rating > 0 and song.rating < self._filter_rating:
            return False
        if self._filter_collab and "collab" not in song.title.lower():
            return False
        if self._filter_cover and "cover" not in song.title.lower():
            return False
        return True

    def _on_sort_changed(self, logical_index: int, order):
        """Update column headers with ▲/▼ sort arrows and rebuild play queue."""
        for i, base_name in enumerate(self._base_headers):
            if i == logical_index:
                arrow = " ▼" if order == Qt.AscendingOrder else " ▲"
                self.table.horizontalHeaderItem(i).setText(base_name + arrow)
            else:
                self.table.horizontalHeaderItem(i).setText(base_name)
        self._rebuild_play_queue()
        self._update_queue_numbers()

    def _apply_filters(self):
        for sid, song in self.songs_by_id.items():
            item = self.row_items.get(sid)
            if item is not None:
                self.table.setRowHidden(item.row(), not self._matches(song))
        self._update_queue_numbers()
        self._rebuild_play_queue()

    def _rebuild_play_queue(self):
        """Rebuild the player's queue from the current visible table order.
        Preserves the currently playing song's position in the new queue."""
        current = self.player.current_song()
        new_queue = self._visible_songs_in_order()
        if not new_queue:
            return
        self.player._queue = new_queue
        if current and current in new_queue:
            self.player._index = new_queue.index(current)
        elif new_queue:
            self.player._index = max(0, min(self.player._index, len(new_queue) - 1))

    def _update_queue_numbers(self):
        """Renumber visible rows 1,2,3... so Queue # always reflects the
        current filtered view, not the original row index."""
        vh = self.table.verticalHeader()
        queue_num = 0
        for visual in range(self.table.rowCount()):
            if not self.table.isRowHidden(visual):
                queue_num += 1
                # Set the vertical header label for this row
                vhi = self.table.verticalHeaderItem(visual)
                if vhi is None:
                    vhi = QTableWidgetItem()
                    self.table.setVerticalHeaderItem(visual, vhi)
                vhi.setText(str(queue_num))
            else:
                vhi = self.table.verticalHeaderItem(visual)
                if vhi:
                    vhi.setText("")

    def _visible_songs_in_order(self) -> List[Song]:
        """Return songs in their current visible table order (respects
        sorting and filters). This is what the play queue should use."""
        result = []
        for row in range(self.table.rowCount()):
            if self.table.isRowHidden(row):
                continue
            item = self.table.item(row, COL_ARTIST)
            if item is None:
                continue
            sid = item.data(Qt.UserRole)
            song = self.songs_by_id.get(sid)
            if song:
                result.append(song)
        return result

    def _filtered_songs(self) -> List[Song]:
        """Alias for visible songs in order."""
        return self._visible_songs_in_order()

    # ------------------------------------------------------------------
    # Play filtered
    # ------------------------------------------------------------------
    def _play_filtered(self):
        songs = self._visible_songs_in_order()
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
            artist_part, song_part = _split_title(song.title)
            item.setText(artist_part)
            sn_item = self.table.item(row, COL_SONGNAME)
            if sn_item: sn_item.setText(song_part)
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
        self.table.setSortingEnabled(False)
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setRowHeight(row, ROW_HEIGHT)

        artist_part, song_part = _split_title(song.title)

        ti = QTableWidgetItem(artist_part)
        ti.setData(Qt.UserRole, song.id)
        self.table.setItem(row, COL_ARTIST, ti)
        self.row_items[song.id] = ti

        sn = QTableWidgetItem(song_part)
        self.table.setItem(row, COL_SONGNAME, sn)

        # ── Name edit pen (edits both artist + song name) ──
        name_lock = LockButton(
            "Green pen: name is protected. Click to edit.",
            "Red pen: name editing active.")
        name_lock.toggledLock.connect(
            lambda unlocked, s=song, lk=name_lock: self._on_title_edit_unlock(s, lk, unlocked))
        self.table.setCellWidget(row, COL_NAME_EDIT, _centered(name_lock))

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

        # ── Disable editing for To-Do items (placeholders, not songs) ──
        is_todo = song.path.suffix.lower() in TODO_EXTENSIONS
        if is_todo:
            cat_lock.setVisible(False)
            rat_lock.setVisible(False)
            combo.setEnabled(False)

        # ── Delete button ──
        del_btn = QToolButton()
        del_btn.setText("\u2716")   # ✖
        del_btn.setToolTip("Delete this file permanently")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setFixedSize(20, 20)
        del_btn.setStyleSheet(
            "QToolButton{background:transparent;border:none;color:#6e6e73;font-size:12px;}"
            "QToolButton:hover{color:#FF453A;}"
        )
        del_btn.clicked.connect(lambda _=False, s=song: self._delete_song(s))
        self.table.setCellWidget(row, COL_DELETE, _centered(del_btn, right_pad=6))

        self.table.setRowHidden(row, not self._matches(song))
        self.table.setSortingEnabled(True)

    # ------------------------------------------------------------------
    # Selection & bulk operations
    # ------------------------------------------------------------------
    def _on_selection_changed(self):
        """Enable bulk controls when 2+ songs are selected."""
        sel_rows = set(idx.row() for idx in self.table.selectedIndexes())
        multi = len(sel_rows) >= 2
        self.bulk_cat_combo.setEnabled(multi)
        self.bulk_rat_combo.setEnabled(multi)
        if multi:
            self.bulk_status.setText(f"{len(sel_rows)} songs selected")
            cats = list_categories(self.root_path) if self.root_path else []
            self.bulk_cat_combo.blockSignals(True)
            self.bulk_cat_combo.clear()
            self.bulk_cat_combo.addItem("Move to\u2026")
            self.bulk_cat_combo.addItems(cats)
            self.bulk_cat_combo.blockSignals(False)
        else:
            self.bulk_status.setText("Select 2+ songs to enable")

    def _selected_songs(self) -> List[Song]:
        """Return songs from the currently selected rows."""
        seen = set()
        songs = []
        for idx in self.table.selectedIndexes():
            row = idx.row()
            if row in seen:
                continue
            seen.add(row)
            item = self.table.item(row, COL_ARTIST)
            if item is None:
                continue
            sid = item.data(Qt.UserRole)
            song = self.songs_by_id.get(sid)
            if song:
                songs.append(song)
        return songs

    def _on_bulk_category(self, idx: int):
        if idx <= 0:
            return
        new_cat = self.bulk_cat_combo.itemText(idx)
        songs = self._selected_songs()
        if not songs:
            return

        self.watcher.blockSignals(True)
        self._rescan_timer.stop()
        self.table.setSortingEnabled(False)
        self._set_info(f"Moving {len(songs)} files to '{new_cat}'\u2026", "loading", auto_reset=False)

        moved = 0
        for i, song in enumerate(songs):
            if song.category == new_cat:
                continue
            if self.player.current_song() is song and self.player.is_playing():
                continue
            old_path_str = str(song.path)
            try:
                new_path = safe_move_song(song, self.root_path, new_cat)
            except SafeMoveError:
                continue
            song.path = new_path
            song.category = new_cat
            self.player.notify_song_path_changed(song, new_path)
            self.cache.pop(old_path_str, None)
            try:
                st = new_path.stat()
                self.cache[str(new_path)] = {
                    "size": st.st_size, "mtime": st.st_mtime,
                    "title": song.title, "artist": song.artist,
                    "rating": song.rating, "duration": song.duration,
                    "id": song.id}
            except OSError:
                pass
            item = self.row_items.get(song.id)
            if item:
                combo = self.table.cellWidget(item.row(), COL_CATEGORY)
                if combo:
                    combo.blockSignals(True)
                    combo.setCurrentText(new_cat)
                    combo.blockSignals(False)
            moved += 1
            if (i + 1) % 30 == 0:
                QApplication.processEvents()

        if self.root_path:
            save_cache(self.root_path, self.cache)

        self.table.setSortingEnabled(True)
        self.bulk_cat_combo.setCurrentIndex(0)
        self._update_queue_numbers()
        self.watcher.blockSignals(False)
        self._rescan_timer.stop()
        self._set_info(
            f"\u2713  {moved} songs moved to '{new_cat}'",
            "success", duration_ms=3500)

    def _on_bulk_rating(self, idx: int):
        if idx <= 0:
            return
        new_rating = self.bulk_rat_combo.itemData(idx)
        if new_rating is None or new_rating < 0:
            return
        songs = self._selected_songs()
        if not songs:
            return

        # Block ALL file-change signals to prevent repeated rescans
        self.watcher.blockSignals(True)
        self._rescan_timer.stop()
        self.table.setSortingEnabled(False)
        self._set_info(f"Writing rating to {len(songs)} files\u2026", "loading", auto_reset=False)

        count = 0
        for i, song in enumerate(songs):
            if write_rating(song.path, new_rating):
                song.rating = new_rating
                try:
                    st = song.path.stat()
                    self.cache[str(song.path)] = {
                        "size": st.st_size, "mtime": st.st_mtime,
                        "title": song.title, "artist": song.artist,
                        "rating": new_rating, "duration": song.duration,
                        "id": song.id}
                except OSError:
                    pass
                item = self.row_items.get(song.id)
                if item:
                    star_wrap = self.table.cellWidget(item.row(), COL_RATING)
                    if star_wrap:
                        star = star_wrap.findChild(StarRatingWidget)
                        if star:
                            star.set_rating(new_rating)
                count += 1
            # Keep UI responsive during large bulk operations
            if (i + 1) % 30 == 0:
                QApplication.processEvents()

        # Save cache ONCE at the end
        if self.root_path:
            save_cache(self.root_path, self.cache)

        self.table.setSortingEnabled(True)
        self.bulk_rat_combo.setCurrentIndex(0)
        self._update_queue_numbers()

        # Unblock watcher and cancel any queued rescans
        self.watcher.blockSignals(False)
        self._rescan_timer.stop()

        stars = "\u2605" * new_rating + "\u2606" * (5 - new_rating)
        self._set_info(
            f"\u2713  Rating {stars} ({new_rating}/5) set for {count} songs",
            "success", duration_ms=3500)

    def _on_double_click(self, index):
        item = self.table.item(index.row(), COL_ARTIST)
        if item is None: return
        song = self.songs_by_id.get(item.data(Qt.UserRole))
        if song: self.player.play_song(song, queue=self._visible_songs_in_order())

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
    # ------------------------------------------------------------------
    # Title / Artist inline editing
    # ------------------------------------------------------------------
    def _on_title_edit_unlock(self, song: Song, lock: "LockButton", unlocked: bool):
        item = self.row_items.get(song.id)
        if item is None:
            lock.set_locked(True); return
        row = item.row()
        old_artist, old_songname = _split_title(song.title)

        def _commit_name(new_artist: str, new_songname: str):
            """Combine artist + song name, rename file if changed."""
            if new_artist and new_songname:
                new_title = f"{new_artist} - {new_songname}"
            elif new_songname:
                new_title = new_songname
            else:
                new_title = song.title

            # Update display
            a_part, s_part = _split_title(new_title)
            item.setText(a_part)
            sn_item = self.table.item(row, COL_SONGNAME)
            if sn_item: sn_item.setText(s_part)

            if new_title != song.title:
                old_path_str = str(song.path)
                old_title = song.title
                ok, new_path = write_display_tags(song.path, new_title, song.artist)
                if ok:
                    self.cache.pop(old_path_str, None)
                    song.path = new_path
                    song.title = new_title
                    self.player.notify_song_path_changed(song, new_path)
                    self._sync_cache(song)
                    self._set_info(f'\u2713  Renamed: "{new_title}"', "success")
                    self._log_change("RENAME", new_title, f"was: {old_title}")
                else:
                    # Revert display
                    a2, s2 = _split_title(song.title)
                    item.setText(a2)
                    if sn_item: sn_item.setText(s2)
                    self._set_info("Could not rename file.", "error")

        if not unlocked:
            # Pen clicked back to green → close editors and commit
            ed_artist = self.table.cellWidget(row, COL_ARTIST)
            ed_song   = self.table.cellWidget(row, COL_SONGNAME)
            new_a = ed_artist.text().strip() if isinstance(ed_artist, QLineEdit) else old_artist
            new_s = ed_song.text().strip() if isinstance(ed_song, QLineEdit) else old_songname
            self.table.removeCellWidget(row, COL_ARTIST)
            self.table.removeCellWidget(row, COL_SONGNAME)
            _commit_name(new_a, new_s or old_songname)
            return

        # Pen clicked to red → open editors in both columns
        from PySide6.QtWidgets import QCompleter
        existing = [s.title for s in self.songs_by_id.values()]

        ed_a = QLineEdit(old_artist)
        ed_a.setObjectName("cellEditor")
        ed_a.setPlaceholderText("Artist...")
        comp_a = QCompleter([_split_title(t)[0] for t in existing if _split_title(t)[0]], ed_a)
        comp_a.setCaseSensitivity(Qt.CaseInsensitive)
        comp_a.setFilterMode(Qt.MatchContains)
        comp_a.setMaxVisibleItems(5)
        ed_a.setCompleter(comp_a)
        self.table.setCellWidget(row, COL_ARTIST, ed_a)

        ed_s = QLineEdit(old_songname)
        ed_s.setObjectName("cellEditor")
        ed_s.setPlaceholderText("Song name...")
        comp_s = QCompleter([_split_title(t)[1] for t in existing], ed_s)
        comp_s.setCaseSensitivity(Qt.CaseInsensitive)
        comp_s.setFilterMode(Qt.MatchContains)
        comp_s.setMaxVisibleItems(5)
        ed_s.setCompleter(comp_s)
        self.table.setCellWidget(row, COL_SONGNAME, ed_s)
        ed_a.setFocus()

        def commit_enter():
            new_a = ed_a.text().strip()
            new_s = ed_s.text().strip() or old_songname
            self.table.removeCellWidget(row, COL_ARTIST)
            self.table.removeCellWidget(row, COL_SONGNAME)
            _commit_name(new_a, new_s)
            lock.set_locked(True)

        ed_a.returnPressed.connect(lambda: ed_s.setFocus())  # Tab to song name
        ed_s.returnPressed.connect(commit_enter)

    def _sync_cache(self, song: Song):
        """Update on-disk cache after any in-app change to a song."""
        try:
            st  = song.path.stat()
            rec = self.cache.get(str(song.path), {})
            rec.update({"size": st.st_size, "mtime": st.st_mtime,
                        "title": song.title, "artist": song.artist,
                        "rating": song.rating, "duration": song.duration,
                        "id": song.id})
            self.cache[str(song.path)] = rec
            if self.root_path: save_cache(self.root_path, self.cache)
        except OSError:
            pass


    def _on_cat_combo(self, song: Song, combo: QComboBox, new_cat: str):
        if new_cat == song.category or not combo.isEnabled(): return

        # Block watcher to prevent duplicate detection during move
        self.watcher.blockSignals(True)
        self.table.setSortingEnabled(False)

        old_cat, old_path_str = song.category, str(song.path)

        # If this song is currently playing: pause, save position, move, resume
        was_playing = (self.player.current_song() is song and self.player.is_playing())
        saved_pos = 0
        if was_playing:
            saved_pos = self.player._player.position()
            self.player._player.pause()

        try:
            new_path = safe_move_song(song, self.root_path, new_cat)
        except SafeMoveError as e:
            self._show_toast(f"Move failed: {e}", 6000, "error")
            combo.blockSignals(True); combo.setCurrentText(old_cat); combo.blockSignals(False)
            if was_playing:
                self.player._player.play()
            self.table.setSortingEnabled(True)
            self.watcher.blockSignals(False)
            return

        # Update song data immediately
        song.path = new_path; song.category = new_cat
        self.player.notify_song_path_changed(song, new_path)
        self.cache.pop(old_path_str, None)
        self._sync_cache(song)

        # Resume playback from the same position at the new path
        if was_playing:
            self.player._player.setSource(QUrl.fromLocalFile(str(new_path)))
            self.player._player.play()
            QTimer.singleShot(100, lambda p=saved_pos: self.player._player.setPosition(p))

        lock: LockButton = combo.property("lock_ref")
        if lock: lock.set_locked(True)
        combo.setEnabled(False)
        self.table.setRowHidden(self.row_items[song.id].row(), not self._matches(song))
        self.table.setSortingEnabled(True)

        # Unblock watcher and cancel any pending rescans
        self.watcher.blockSignals(False)
        self._rescan_timer.stop()
        self._show_toast(f"\u2713  '{song.title}'  moved: {old_cat} \u2192 {new_cat}", 3500, "success")
        self._log_change("MOVE", song.title, f"{old_cat} -> {new_cat}")

    # ------------------------------------------------------------------
    # Rating change → write directly into the audio file
    # ------------------------------------------------------------------
    def _on_rating(self, song: Song, star_widget: "StarRatingWidget", new_rating: int):
        item = self.row_items.get(song.id)

        # Block watcher to prevent intermediate rescan
        self.watcher.blockSignals(True)
        self.table.setSortingEnabled(False)

        if not write_rating(song.path, new_rating):
            self._show_toast(
                f"Could not write rating into '{song.path.name}' — format may not support it.",
                5000, "error")
            self.table.setSortingEnabled(True)
            return
        song.rating = new_rating
        self._sync_cache(song)

        # Auto re-lock the rating pen after saving
        lock: LockButton = star_widget.property("lock_ref")
        if lock:
            lock.set_locked(True)
        star_widget.set_editable(False)

        # Re-evaluate row visibility (rating filter may now hide/show this song)
        if item:
            self.table.setRowHidden(item.row(), not self._matches(song))

        # Re-enable sorting and unblock watcher
        self.table.setSortingEnabled(True)
        self.watcher.blockSignals(False)
        self._rescan_timer.stop()

        # Restore selection to the same song row
        if item:
            actual_row = item.row()
            self.table.selectRow(actual_row)
            self.table.scrollTo(self.table.model().index(actual_row, COL_ARTIST))

        # Inline toast — rating saved directly into the file
        stars = "\u2605" * new_rating + "\u2606" * (5 - new_rating)
        if new_rating == 0:
            self._show_toast(f"Rating cleared  \u2014  {song.title}", 3000, "info")
        else:
            self._show_toast(
                f"\u2713  Rating saved into file:  {song.title}   {stars}  ({new_rating}/5)",
                3500, "success")
            self._log_change("RATING", song.title, f"{new_rating}/5")


# ============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)

    # Set app icon
    icon_path = Path("../assets/01_media/01_icons/icon.ico")
    if not icon_path.exists():
        icon_path = Path("assets/01_media/01_icons/icon.ico")
    if icon_path.exists():
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))

    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()