#!/usr/bin/env python3
"""
Music Library App
=================
A single-file, modern, dark-mode (Apple-style) music library application.

Highlights of this version:
- Songs load in the BACKGROUND, one by one, with a progress window that
  shows count, percentage and estimated time remaining. The UI never freezes.
- A local on-disk CACHE remembers every song's tags and rating, so opening a
  huge library again is basically instant - only NEW or CHANGED files are
  actually re-read from disk.
- Only what is inside the category subfolders is scanned (nothing else).
- The library stays dynamically up to date: adding/removing files is detected
  and can be reviewed via "View changes". Moving/renaming a file (inside or
  outside the app) is recognized as a MOVE, never as delete + add.
- Filtering by category, by minimum star rating, and by search text no longer
  rebuilds the table, so your filter selection is NEVER lost and stays fast.
- Category = the current name of the subfolder a file lives in. Renaming a
  folder updates the category everywhere automatically.
- Changing a category via the dropdown physically MOVES the file (crash-safe:
  copy -> verify -> only then delete original; never overwrites anything).
- Rating (0-5) is written DIRECTLY into the audio file, so it survives moving.
- Green/red lock buttons guard both category changes and rating changes, so
  nothing happens by accident. After a change, the lock auto-returns to green.
- Unrated songs show 5 visible GRAY outline stars; ratings are YELLOW.
- Full player: play/pause, next/previous, seek, volume, repeat (off/all/one).

Run:
    pip install PySide6 mutagen
    python music_app.py

Build a standalone .exe later:
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

from PySide6.QtCore import (
    QFileSystemWatcher, QObject, QSettings, QStandardPaths, Qt, QThread,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import QFont
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider,
    QTableWidget, QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

ORG_NAME = "LocalMusicApps"
APP_NAME = "MusicLibrary"


# ============================================================================
# Metadata: read display tags, read/write the star rating directly into
# the audio file itself.
# ============================================================================
RATING_TXXX_DESC = "RATING"
MP4_RATING_ATOM = "----:com.apple.iTunes:RATING"

SUPPORTED_EXTENSIONS = {".mp3", ".flac", ".ogg", ".m4a", ".mp4", ".wav", ".wma", ".aac", ".oga"}


def read_display_tags(path: Path) -> Tuple[str, str]:
    """Returns (title, artist). Falls back to the filename without extension."""
    title = path.stem
    artist = ""
    try:
        audio = MutagenFile(path, easy=True)
        if audio and audio.tags:
            t = audio.tags.get("title")
            a = audio.tags.get("artist")
            if t:
                title = t[0]
            if a:
                artist = a[0]
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
            if val:
                return _clamp(_safe_int(val[0]))
            return 0

        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(path)
            val = audio.get(RATING_TXXX_DESC.lower())
            if val:
                return _clamp(_safe_int(val[0]))
            return 0

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
    """Writes the rating (0-5) directly into the file. Returns True on success."""
    rating = _clamp(rating)
    ext = path.suffix.lower()
    try:
        if ext in (".mp3", ".wav", ".aac"):
            try:
                id3 = ID3(path)
            except ID3NoHeaderError:
                id3 = ID3()
            to_remove = [f for f in id3.getall("TXXX") if getattr(f, "desc", "").upper() == RATING_TXXX_DESC]
            for f in to_remove:
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
# Library: Song data class, fast folder listing, crash-safe move
# ============================================================================
@dataclass
class Song:
    path: Path
    category: str          # = name of the subfolder the file currently lives in
    title: str
    artist: str
    rating: int
    duration: float = 0.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def filename(self) -> str:
        return self.path.name


class SafeMoveError(Exception):
    pass


def list_categories(root: Path) -> List[str]:
    """All direct subfolders of root = all categories (including empty ones)."""
    if not root.is_dir():
        return []
    return sorted(
        [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")],
        key=str.lower,
    )


def fast_list_files(root: Path) -> Dict[str, Tuple[str, int, float]]:
    """
    Cheaply lists every audio file under root's subfolders WITHOUT reading
    any tags - just path -> (category, size, mtime). Fast even for huge
    libraries because it only touches filesystem metadata.
    """
    result: Dict[str, Tuple[str, int, float]] = {}
    if not root.is_dir():
        return result
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        category = folder.name
        for file in folder.iterdir():
            if not file.is_file():
                continue
            if file.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                st = file.stat()
            except OSError:
                continue
            result[str(file)] = (category, st.st_size, st.st_mtime)
    return result


def scan_library(root: Path) -> List[Song]:
    """Full synchronous scan (tags included). Simple fallback / used in tests."""
    songs: List[Song] = []
    for path_str, (category, _size, _mtime) in fast_list_files(root).items():
        file = Path(path_str)
        title, artist = read_display_tags(file)
        rating = read_rating(file)
        duration = read_duration_seconds(file)
        songs.append(Song(path=file, category=category, title=title, artist=artist,
                           rating=rating, duration=duration))
    return songs


def safe_move_song(song: "Song", root: Path, target_category: str) -> Path:
    """
    Physically moves the file into the target category folder.
    Safety:
      - Original is NEVER deleted before the copy is verified (size match).
      - On a name conflict, a unique name is used - nothing is overwritten.
    Returns the new path, or raises SafeMoveError.
    """
    target_dir = root / target_category
    if not target_dir.is_dir():
        raise SafeMoveError(f"Target folder does not exist: {target_dir}")

    src = song.path
    if not src.is_file():
        raise SafeMoveError(f"Source file not found: {src}")

    dest = target_dir / src.name
    if dest.resolve() == src.resolve():
        return src  # already there

    if dest.exists():
        stem, suffix = src.stem, src.suffix
        counter = 1
        while dest.exists():
            dest = target_dir / f"{stem} ({counter}){suffix}"
            counter += 1

    tmp_dest = dest.with_name(dest.name + ".part")

    try:
        shutil.copy2(src, tmp_dest)
        if os.path.getsize(tmp_dest) != os.path.getsize(src):
            raise SafeMoveError("Verification failed (size mismatch)")
        os.replace(tmp_dest, dest)
        os.remove(src)
    except Exception as e:
        if tmp_dest.exists():
            try:
                os.remove(tmp_dest)
            except OSError:
                pass
        raise SafeMoveError(str(e))

    return dest


# ============================================================================
# On-disk cache (so a huge library only needs to be tag-read once)
# ============================================================================
def get_cache_path(root: Path) -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
    if not base:
        base = str(Path.home() / ".music_library_app")
    base_path = Path(base)
    try:
        base_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        base_path = Path.home()
    h = hashlib.md5(str(root.resolve()).encode("utf-8")).hexdigest()
    return base_path / f"music_library_cache_{h}.json"


def load_cache(root: Path) -> Dict[str, dict]:
    path = get_cache_path(root)
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(root: Path, cache: Dict[str, dict]):
    path = get_cache_path(root)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


# ============================================================================
# Background scan worker: reuses the cache, only truly reads tags for new or
# changed files (one at a time), and tells a genuine add/remove apart from a
# simple move/rename.
# ============================================================================
class ScanWorker(QThread):
    totalDetermined = Signal(int)
    progressUpdate = Signal(int, int, object, str)   # processed, total, eta_seconds|None, filename
    batchReady = Signal(list)                         # list[Song] - cached/moved, no tag reading needed
    songReady = Signal(object)                        # Song - slow path, emitted one by one
    rowsRemoved = Signal(list)                         # list[str] ids that no longer exist
    scanFinished = Signal(list, list, int)             # added_titles, removed_titles, moved_count
    scanError = Signal(str)

    def __init__(self, root: Path, previous_cache: Dict[str, dict], parent=None):
        super().__init__(parent)
        self.root = root
        self.previous_cache = previous_cache or {}
        self.new_cache: Dict[str, dict] = {}

    def run(self):
        try:
            current = fast_list_files(self.root)
        except Exception as e:
            self.scanError.emit(str(e))
            return

        old_cache = self.previous_cache
        old_paths = set(old_cache.keys())
        new_paths = set(current.keys())
        common = old_paths & new_paths

        unchanged_paths: List[str] = []
        changed_paths: List[str] = []
        for p in common:
            _cat, size, mtime = current[p]
            rec = old_cache.get(p, {})
            if rec.get("size") == size and rec.get("mtime") is not None and abs(rec["mtime"] - mtime) < 1.0:
                unchanged_paths.append(p)
            else:
                changed_paths.append(p)

        added_paths = list(new_paths - old_paths)
        missing_paths = list(old_paths - new_paths)

        # Match added vs missing by FILE SIZE to detect moves/renames so they
        # are never mistaken for delete + add.
        missing_by_size: Dict[int, List[str]] = {}
        for p in missing_paths:
            rec = old_cache.get(p, {})
            missing_by_size.setdefault(rec.get("size"), []).append(p)

        moved_pairs: List[Tuple[str, str]] = []
        truly_added: List[str] = []
        for p in added_paths:
            _cat, size, _mtime = current[p]
            candidates = missing_by_size.get(size)
            if candidates:
                old_p = candidates.pop()
                if not candidates:
                    del missing_by_size[size]
                moved_pairs.append((old_p, p))
            else:
                truly_added.append(p)

        truly_missing = [p for lst in missing_by_size.values() for p in lst]

        new_cache: Dict[str, dict] = {}
        batch_songs: List[Song] = []

        for p in unchanged_paths:
            cat, size, mtime = current[p]
            rec = old_cache[p]
            song = Song(path=Path(p), category=cat, title=rec.get("title", Path(p).stem),
                        artist=rec.get("artist", ""), rating=rec.get("rating", 0),
                        duration=rec.get("duration", 0.0), id=rec.get("id") or uuid.uuid4().hex)
            new_cache[p] = {**rec, "size": size, "mtime": mtime, "id": song.id}
            batch_songs.append(song)

        for old_p, new_p in moved_pairs:
            cat, size, mtime = current[new_p]
            rec = old_cache.get(old_p, {})
            song = Song(path=Path(new_p), category=cat, title=rec.get("title", Path(new_p).stem),
                        artist=rec.get("artist", ""), rating=rec.get("rating", 0),
                        duration=rec.get("duration", 0.0), id=rec.get("id") or uuid.uuid4().hex)
            new_cache[new_p] = {**rec, "size": size, "mtime": mtime, "id": song.id}
            batch_songs.append(song)

        if batch_songs:
            self.batchReady.emit(batch_songs)

        slow_paths = truly_added + changed_paths
        total = len(slow_paths)
        self.totalDetermined.emit(total)

        start_time = time.monotonic()
        for i, p in enumerate(slow_paths, start=1):
            cat, size, mtime = current[p]
            path_obj = Path(p)
            title, artist = read_display_tags(path_obj)
            rating = read_rating(path_obj)
            duration = read_duration_seconds(path_obj)
            old_id = old_cache.get(p, {}).get("id")
            song = Song(path=path_obj, category=cat, title=title, artist=artist,
                        rating=rating, duration=duration, id=old_id or uuid.uuid4().hex)
            new_cache[p] = {"size": size, "mtime": mtime, "title": title, "artist": artist,
                            "rating": rating, "duration": duration, "id": song.id}
            elapsed = time.monotonic() - start_time
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else None
            self.progressUpdate.emit(i, total, eta, path_obj.name)
            self.songReady.emit(song)

        removed_ids = [old_cache[p]["id"] for p in truly_missing if "id" in old_cache.get(p, {})]
        if removed_ids:
            self.rowsRemoved.emit(removed_ids)

        added_titles = [new_cache.get(p, {}).get("title", Path(p).stem) for p in truly_added]
        removed_titles = [old_cache[p].get("title", Path(p).stem) for p in truly_missing]

        self.new_cache = new_cache
        self.scanFinished.emit(added_titles, removed_titles, len(moved_pairs))


# ============================================================================
# Player controller
# ============================================================================
class RepeatMode(Enum):
    OFF = 0
    ONE = 1
    ALL = 2


class PlayerController(QObject):
    songChanged = Signal(object)          # Song or None
    playbackStateChanged = Signal(bool)   # True = currently playing
    positionChanged = Signal(int)         # ms
    durationChanged = Signal(int)         # ms
    repeatModeChanged = Signal(object)    # RepeatMode

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)

        self._queue: List[Song] = []
        self._index: int = -1
        self._repeat = RepeatMode.OFF

        self._player.playbackStateChanged.connect(self._on_state_changed)
        self._player.positionChanged.connect(self.positionChanged.emit)
        self._player.durationChanged.connect(self.durationChanged.emit)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)

    def set_queue(self, songs: List[Song], start_song: Optional[Song] = None):
        self._queue = list(songs)
        if start_song is not None and start_song in self._queue:
            self._index = self._queue.index(start_song)
        else:
            self._index = 0 if self._queue else -1
        if self._index >= 0:
            self._load_current(autoplay=True)

    def current_song(self) -> Optional[Song]:
        if 0 <= self._index < len(self._queue):
            return self._queue[self._index]
        return None

    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlayingState

    def set_repeat_mode(self, mode: RepeatMode):
        self._repeat = mode
        self.repeatModeChanged.emit(mode)

    def repeat_mode(self) -> RepeatMode:
        return self._repeat

    def toggle_play_pause(self):
        if self._index < 0:
            return
        if self.is_playing():
            self._player.pause()
        else:
            self._player.play()

    def play(self):
        if self._index >= 0:
            self._player.play()

    def pause(self):
        self._player.pause()

    def stop(self):
        self._player.stop()

    def next(self):
        if not self._queue:
            return
        if self._repeat == RepeatMode.ONE:
            self._load_current(autoplay=True)
            return
        if self._index + 1 < len(self._queue):
            self._index += 1
        elif self._repeat == RepeatMode.ALL:
            self._index = 0
        else:
            self._player.stop()
            self.songChanged.emit(None)
            return
        self._load_current(autoplay=True)

    def previous(self):
        if not self._queue:
            return
        if self._player.position() > 3000:
            self._player.setPosition(0)
            return
        if self._index - 1 >= 0:
            self._index -= 1
        elif self._repeat == RepeatMode.ALL:
            self._index = len(self._queue) - 1
        else:
            self._index = 0
        self._load_current(autoplay=True)

    def seek(self, position_ms: int):
        self._player.setPosition(position_ms)

    def set_volume(self, value_0_100: int):
        self._audio_output.setVolume(max(0, min(100, value_0_100)) / 100.0)

    def play_song(self, song: Song, queue: Optional[List[Song]] = None):
        if queue is not None:
            self.set_queue(queue, start_song=song)
        else:
            if song in self._queue:
                self._index = self._queue.index(song)
            else:
                self._queue = [song]
                self._index = 0
            self._load_current(autoplay=True)

    def notify_song_path_changed(self, song: Song, new_path: Path):
        song.path = new_path
        if self.current_song() is song:
            was_playing = self.is_playing()
            pos = self._player.position()
            self._player.setSource(QUrl.fromLocalFile(str(new_path)))
            if was_playing:
                self._player.play()
            self._player.setPosition(pos)

    def _load_current(self, autoplay: bool = False):
        song = self.current_song()
        if song is None:
            return
        self._player.setSource(QUrl.fromLocalFile(str(song.path)))
        self.songChanged.emit(song)
        if autoplay:
            self._player.play()

    def _on_state_changed(self, state):
        self.playbackStateChanged.emit(state == QMediaPlayer.PlayingState)

    def _on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.next()


# ============================================================================
# Widgets: lock button, star rating
# ============================================================================
class LockButton(QToolButton):
    """Lock icon button. Green = locked (safe), red = unlocked (editable)."""

    toggledLock = Signal(bool)  # True = now unlocked

    def __init__(self, tooltip_locked: str, tooltip_unlocked: str, parent=None):
        super().__init__(parent)
        self._locked = True
        self._tooltip_locked = tooltip_locked
        self._tooltip_unlocked = tooltip_unlocked
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(22, 22)
        f = QFont()
        f.setPointSize(10)
        self.setFont(f)
        self.clicked.connect(self._on_click)
        self._refresh()

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool, emit: bool = False):
        self._locked = locked
        self._refresh()
        if emit:
            self.toggledLock.emit(not self._locked)

    def _on_click(self):
        self._locked = not self._locked
        self._refresh()
        self.toggledLock.emit(not self._locked)

    def _refresh(self):
        if self._locked:
            self.setText("\U0001F512")  # locked padlock
            self.setToolTip(self._tooltip_locked)
            self.setStyleSheet(
                "QToolButton { background: rgba(48,209,88,0.15); border: 1px solid #30D158;"
                " border-radius: 6px; color: #30D158; }"
                "QToolButton:hover { background: rgba(48,209,88,0.28); }"
            )
        else:
            self.setText("\U0001F513")  # open padlock
            self.setToolTip(self._tooltip_unlocked)
            self.setStyleSheet(
                "QToolButton { background: rgba(255,69,58,0.15); border: 1px solid #FF453A;"
                " border-radius: 6px; color: #FF453A; }"
                "QToolButton:hover { background: rgba(255,69,58,0.28); }"
            )


class StarRatingWidget(QWidget):
    """
    5 stars. Unrated -> 5 clearly visible GRAY outline stars.
    Rated -> filled stars in YELLOW (the rest stay gray).
    Editable only while unlocked (set_editable).
    """

    ratingChanged = Signal(int)

    COLOR_FILLED = "#FFD60A"   # yellow
    COLOR_EMPTY = "#6e6e73"    # clearly visible gray

    def __init__(self, rating: int = 0, parent=None):
        super().__init__(parent)
        self._rating = rating
        self._enabled_for_edit = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        self._buttons = []
        for i in range(5):
            btn = QPushButton()
            btn.setFlat(True)
            btn.setFixedSize(22, 26)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, idx=i: self._star_clicked(idx))
            layout.addWidget(btn)
            self._buttons.append(btn)
        layout.addStretch()
        self._refresh()

    def rating(self) -> int:
        return self._rating

    def set_rating(self, rating: int):
        self._rating = max(0, min(5, rating))
        self._refresh()

    def set_editable(self, editable: bool):
        self._enabled_for_edit = editable
        self._refresh()

    def _star_clicked(self, idx: int):
        if not self._enabled_for_edit:
            return
        new_rating = idx + 1
        if new_rating == self._rating:
            new_rating = 0  # click the same star again to reset to 0
        self._rating = new_rating
        self._refresh()
        self.ratingChanged.emit(self._rating)

    def _refresh(self):
        for i, btn in enumerate(self._buttons):
            filled = i < self._rating
            char = "\u2605" if filled else "\u2606"  # filled / empty star
            color = self.COLOR_FILLED if filled else self.COLOR_EMPTY
            btn.setText(char)
            btn.setStyleSheet(
                f"QPushButton {{ border: none; background: transparent; color: {color}; font-size: 17px; }}"
                f"QPushButton:disabled {{ color: {color}; }}"
            )
            btn.setEnabled(self._enabled_for_edit)


# ============================================================================
# Loading window (count / percentage / ETA)
# ============================================================================
class LoadingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Importing Music")
        self.setModal(False)
        self.setFixedSize(460, 170)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)

        self.title_label = QLabel("Importing songs...")
        self.title_label.setObjectName("sectionTitle")
        layout.addWidget(self.title_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        self.detail_label = QLabel("")
        self.detail_label.setObjectName("subtleLabel")
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.eta_label = QLabel("")
        self.eta_label.setObjectName("subtleLabel")
        layout.addWidget(self.eta_label)

        layout.addStretch()

    def update_progress(self, processed: int, total: int, eta_seconds: Optional[float], current_name: str):
        pct = int(processed / total * 100) if total else 100
        self.progress_bar.setValue(pct)
        self.detail_label.setText(f"{processed} / {total} songs  ({pct}%)\n{current_name}")
        if eta_seconds is not None:
            self.eta_label.setText(f"Estimated time remaining: {format_time(int(eta_seconds * 1000))}")
        else:
            self.eta_label.setText("Estimating time remaining...")


# ============================================================================
# Dark, Apple-style stylesheet
# ============================================================================
DARK_QSS = """
* {
    font-family: -apple-system, "SF Pro Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    outline: none;
}

QMainWindow, QWidget#centralWidget { background-color: #1c1c1e; }
QWidget { color: #f2f2f7; background-color: transparent; }
QDialog { background-color: #1c1c1e; }

QLabel#sectionTitle { font-size: 20px; font-weight: 600; color: #ffffff; padding: 4px 0px; }
QLabel#subtleLabel { color: #8e8e93; font-size: 12px; }
QLabel#pathLabel { color: #0a84ff; font-size: 12px; padding: 2px 8px; background: rgba(10,132,255,0.1); border-radius: 6px; }
QLabel#statusLabel { color: #8e8e93; font-size: 12px; padding: 2px 8px; }

QToolBar, #topBar { background-color: #1c1c1e; border: none; padding: 10px; spacing: 8px; }

QPushButton {
    background-color: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 10px;
    padding: 8px 16px; color: #f2f2f7; font-size: 13px; font-weight: 500;
}
QPushButton:hover { background-color: #3a3a3c; border: 1px solid #48484a; }
QPushButton:pressed { background-color: #232325; }
QPushButton:disabled { color: #5a5a5e; border: 1px solid #2c2c2e; }

QPushButton#accentButton { background-color: #0a84ff; border: 1px solid #0a84ff; color: white; font-weight: 600; }
QPushButton#accentButton:hover { background-color: #3399ff; }

QPushButton#transportButton { background-color: transparent; border: none; border-radius: 22px; font-size: 18px; padding: 6px; }
QPushButton#transportButton:hover { background-color: #2c2c2e; }

QPushButton#playPauseButton { background-color: #ffffff; border-radius: 24px; color: #1c1c1e; font-size: 18px; min-width: 48px; min-height: 48px; }
QPushButton#playPauseButton:hover { background-color: #e5e5ea; }

QTableWidget {
    background-color: #1c1c1e; alternate-background-color: #202022; border: none;
    gridline-color: transparent; selection-background-color: rgba(10,132,255,0.25); selection-color: #ffffff;
}
QTableWidget::item { padding: 6px; border-bottom: 1px solid #2c2c2e; }
QHeaderView::section {
    background-color: #1c1c1e; color: #8e8e93; border: none; border-bottom: 1px solid #2c2c2e;
    padding: 8px 6px; font-size: 11px; font-weight: 600; text-transform: uppercase;
}
QTableWidget::item:selected { background-color: rgba(10,132,255,0.22); }

QComboBox {
    background-color: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 8px;
    padding: 4px 10px; min-width: 110px; color: #f2f2f7;
}
QComboBox:disabled { color: #5a5a5e; background-color: #232325; }
QComboBox::drop-down { border: none; width: 20px; }
QComboBox QAbstractItemView {
    background-color: #2c2c2e; border: 1px solid #3a3a3c; selection-background-color: #0a84ff; color: #f2f2f7; outline: none;
}

QListWidget { background-color: #232325; border: 1px solid #2c2c2e; border-radius: 10px; padding: 4px; }
QListWidget::item { padding: 6px 8px; border-radius: 6px; }
QListWidget::item:selected { background-color: rgba(10,132,255,0.25); }

QScrollBar:vertical { background: transparent; width: 10px; margin: 0px; }
QScrollBar::handle:vertical { background: #48484a; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #5a5a5e; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }

QSlider::groove:horizontal { height: 4px; background: #3a3a3c; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #0a84ff; border-radius: 2px; }
QSlider::handle:horizontal { background: #ffffff; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }
QSlider::handle:horizontal:hover { background: #e5e5ea; }

QLineEdit { background-color: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 8px; padding: 6px 10px; color: #f2f2f7; }
QLineEdit:focus { border: 1px solid #0a84ff; }

QProgressBar {
    background-color: #2c2c2e; border: 1px solid #3a3a3c; border-radius: 8px;
    text-align: center; color: #f2f2f7; height: 18px;
}
QProgressBar::chunk { background-color: #0a84ff; border-radius: 8px; }

QFrame#playerBar { background-color: #232325; border-top: 1px solid #2c2c2e; }
QFrame#sidebar { background-color: #1c1c1e; border-right: 1px solid #2c2c2e; }

QToolTip { background-color: #3a3a3c; color: #f2f2f7; border: 1px solid #48484a; padding: 4px 8px; border-radius: 6px; }
QMessageBox { background-color: #2c2c2e; }
"""


# ============================================================================
# Main window
# ============================================================================
COL_TITLE = 0
COL_ARTIST = 1
COL_CATEGORY = 2
COL_CATEGORY_LOCK = 3
COL_RATING = 4
COL_RATING_LOCK = 5
COL_COUNT = 6


def format_time(ms: int) -> str:
    total_seconds = max(0, ms // 1000)
    m, s = divmod(total_seconds, 60)
    return f"{m:02d}:{s:02d}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Music Library")
        self.resize(1180, 760)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self.root_path: Optional[Path] = None
        self.cache: Dict[str, dict] = {}
        self.songs_by_id: Dict[str, Song] = {}
        self.row_items: Dict[str, QTableWidgetItem] = {}
        self.checked_categories: set = set()  # empty = show all
        self.min_rating_filter: int = 0
        self._last_known_categories: List[str] = []
        self._currently_highlighted_id: Optional[str] = None

        self.scan_worker: Optional[ScanWorker] = None
        self.pending_rescan = False
        self.loading_dialog: Optional[LoadingDialog] = None
        self.last_added_titles: List[str] = []
        self.last_removed_titles: List[str] = []

        self.player = PlayerController(self)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._on_dir_changed)
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(500)
        self._rescan_timer.timeout.connect(self._start_scan)

        self._build_ui()
        self._connect_player_signals()

        last_root = self.settings.value("root_path", "")
        if last_root and Path(last_root).is_dir():
            self._set_root_path(Path(last_root))

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_top_bar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        body.addWidget(self._build_table(), stretch=1)
        body_widget = QWidget()
        body_widget.setLayout(body)
        root_layout.addWidget(body_widget, stretch=1)

        root_layout.addWidget(self._build_player_bar())

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        title = QLabel("Music Library")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        self.path_label = QLabel("No folder selected")
        self.path_label.setObjectName("pathLabel")
        layout.addWidget(self.path_label)

        self.status_label = QLabel("")
        self.status_label.setObjectName("statusLabel")
        layout.addWidget(self.status_label)

        self.changes_button = QPushButton("View changes")
        self.changes_button.setEnabled(False)
        self.changes_button.clicked.connect(self._show_changes_dialog)
        layout.addWidget(self.changes_button)

        layout.addStretch()

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search by title or artist...")
        self.search_box.setFixedWidth(260)
        self.search_box.textChanged.connect(self._apply_filters)
        layout.addWidget(self.search_box)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._start_scan)
        layout.addWidget(refresh_btn)

        add_btn = QPushButton("Add Music Folder")
        add_btn.setObjectName("accentButton")
        add_btn.clicked.connect(self._choose_root_folder)
        layout.addWidget(add_btn)

        return bar

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(230)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        label = QLabel("Categories (filter)")
        label.setObjectName("subtleLabel")
        layout.addWidget(label)

        self.category_list = QListWidget()
        self.category_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.category_list.itemChanged.connect(self._on_category_filter_changed)
        layout.addWidget(self.category_list, stretch=1)

        hint = QLabel("Nothing checked = all categories are shown. Your filter selection is kept.")
        hint.setObjectName("subtleLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        rating_label = QLabel("Minimum rating (filter)")
        rating_label.setObjectName("subtleLabel")
        layout.addWidget(rating_label)

        self.rating_filter_widget = StarRatingWidget(0)
        self.rating_filter_widget.set_editable(True)
        self.rating_filter_widget.ratingChanged.connect(self._on_rating_filter_changed)
        layout.addWidget(self.rating_filter_widget)

        rating_hint = QLabel("Click a star for a minimum. Click the same star again to show all ratings.")
        rating_hint.setObjectName("subtleLabel")
        rating_hint.setWordWrap(True)
        layout.addWidget(rating_hint)

        play_all_btn = QPushButton("Play All")
        play_all_btn.clicked.connect(self._play_all_filtered)
        layout.addWidget(play_all_btn)

        return sidebar

    def _build_table(self) -> QWidget:
        self.table = QTableWidget(0, COL_COUNT)
        self.table.setHorizontalHeaderLabels(["Title", "Artist", "Category", "", "Rating", ""])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setDefaultSectionSize(42)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_TITLE, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_ARTIST, QHeaderView.Stretch)
        header.setSectionResizeMode(COL_CATEGORY, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_CATEGORY_LOCK, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_RATING, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_RATING_LOCK, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(self._on_row_double_clicked)
        return self.table

    def _build_player_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("playerBar")
        bar.setFixedHeight(96)
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(20, 8, 20, 10)
        outer.setSpacing(4)

        seek_row = QHBoxLayout()
        self.time_current_label = QLabel("00:00")
        self.time_current_label.setObjectName("subtleLabel")
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self._on_seek_slider_moved)
        self.time_total_label = QLabel("00:00")
        self.time_total_label.setObjectName("subtleLabel")
        seek_row.addWidget(self.time_current_label)
        seek_row.addWidget(self.seek_slider, stretch=1)
        seek_row.addWidget(self.time_total_label)
        outer.addLayout(seek_row)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(14)

        self.now_playing_label = QLabel("Nothing is playing")
        self.now_playing_label.setObjectName("subtleLabel")
        self.now_playing_label.setMinimumWidth(260)
        controls_row.addWidget(self.now_playing_label, stretch=1)

        controls_row.addStretch()

        prev_btn = QPushButton("\u23EE")
        prev_btn.setObjectName("transportButton")
        prev_btn.clicked.connect(self.player.previous)
        controls_row.addWidget(prev_btn)

        self.play_pause_btn = QPushButton("\u25B6")
        self.play_pause_btn.setObjectName("playPauseButton")
        self.play_pause_btn.clicked.connect(self.player.toggle_play_pause)
        controls_row.addWidget(self.play_pause_btn)

        next_btn = QPushButton("\u23ED")
        next_btn.setObjectName("transportButton")
        next_btn.clicked.connect(self.player.next)
        controls_row.addWidget(next_btn)

        self.repeat_btn = QPushButton("\U0001F501")
        self.repeat_btn.setObjectName("transportButton")
        self.repeat_btn.setToolTip("Repeat: Off")
        self.repeat_btn.clicked.connect(self._cycle_repeat_mode)
        controls_row.addWidget(self.repeat_btn)

        controls_row.addStretch()

        vol_label = QLabel("\U0001F50A")
        controls_row.addWidget(vol_label)
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(120)
        self.volume_slider.valueChanged.connect(self.player.set_volume)
        controls_row.addWidget(self.volume_slider)
        self.player.set_volume(80)

        outer.addLayout(controls_row)
        return bar

    # ------------------------------------------------------------------
    # Player signals
    # ------------------------------------------------------------------
    def _connect_player_signals(self):
        self.player.songChanged.connect(self._on_player_song_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)

    def _on_player_song_changed(self, song: Optional[Song]):
        if song is None:
            self.now_playing_label.setText("Nothing is playing")
        else:
            artist_part = f" - {song.artist}" if song.artist else ""
            self.now_playing_label.setText(f"{song.title}{artist_part}  \u2022  {song.category}")
        self._highlight_playing_row(song)

    def _on_playback_state_changed(self, playing: bool):
        self.play_pause_btn.setText("\u23F8" if playing else "\u25B6")

    def _on_position_changed(self, pos_ms: int):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(pos_ms)
        self.time_current_label.setText(format_time(pos_ms))

    def _on_duration_changed(self, dur_ms: int):
        self.seek_slider.setRange(0, max(0, dur_ms))
        self.time_total_label.setText(format_time(dur_ms))

    def _on_seek_slider_moved(self, value: int):
        self.player.seek(value)

    def _cycle_repeat_mode(self):
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE]
        current = self.player.repeat_mode()
        new_mode = order[(order.index(current) + 1) % len(order)]
        self.player.set_repeat_mode(new_mode)
        labels = {RepeatMode.OFF: "Off", RepeatMode.ALL: "All", RepeatMode.ONE: "One"}
        self.repeat_btn.setToolTip(f"Repeat: {labels[new_mode]}")
        self.repeat_btn.setStyleSheet("color: #0a84ff;" if new_mode != RepeatMode.OFF else "")

    # ------------------------------------------------------------------
    # Folder / library
    # ------------------------------------------------------------------
    def _choose_root_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select your main music folder (must contain the category subfolders)"
        )
        if folder:
            self._set_root_path(Path(folder))

    def _set_root_path(self, path: Path):
        if self.watcher.directories():
            self.watcher.removePaths(self.watcher.directories())

        self.root_path = path
        self.path_label.setText(str(path))
        self.settings.setValue("root_path", str(path))

        self.table.setRowCount(0)
        self.row_items.clear()
        self.songs_by_id.clear()
        self.checked_categories.clear()
        self._last_known_categories = []
        self._currently_highlighted_id = None
        self.cache = load_cache(path)

        self._refresh_category_choices()
        self.watcher.addPath(str(path))
        for sub in list_categories(path):
            self.watcher.addPath(str(path / sub))

        self._start_scan()

    def _on_dir_changed(self, _changed_path: str):
        self._rescan_timer.start()  # debounce -> one rescan

    # ------------------------------------------------------------------
    # Background scanning
    # ------------------------------------------------------------------
    def _start_scan(self):
        if self.root_path is None:
            return
        if self.scan_worker is not None and self.scan_worker.isRunning():
            self.pending_rescan = True
            return

        self._update_status("Checking library...")
        worker = ScanWorker(self.root_path, dict(self.cache))
        worker.batchReady.connect(self._on_batch_ready)
        worker.songReady.connect(self._on_song_ready)
        worker.totalDetermined.connect(self._on_total_determined)
        worker.progressUpdate.connect(self._on_progress_update)
        worker.rowsRemoved.connect(self._on_rows_removed)
        worker.scanError.connect(self._on_scan_error)
        worker.scanFinished.connect(
            lambda added, removed, moved, w=worker: self._on_scan_finished(added, removed, moved, w)
        )
        worker.finished.connect(lambda w=worker: self._on_thread_finished(w))
        self.scan_worker = worker
        worker.start()

    def _on_thread_finished(self, worker: ScanWorker):
        if self.scan_worker is worker:
            self.scan_worker = None
        if self.pending_rescan:
            self.pending_rescan = False
            QTimer.singleShot(50, self._start_scan)

    def _on_batch_ready(self, songs: List[Song]):
        for song in songs:
            self._add_or_update_row(song)
        self._refresh_category_choices()
        self._apply_filters()
        self._update_status(f"{len(self.songs_by_id)} songs")

    def _on_song_ready(self, song: Song):
        self._add_or_update_row(song)
        self._apply_filters()

    def _on_total_determined(self, total: int):
        if total > 0:
            if self.loading_dialog is None:
                self.loading_dialog = LoadingDialog(self)
            self.loading_dialog.update_progress(0, total, None, "")
            self.loading_dialog.show()
            self.loading_dialog.raise_()
            self._update_status(f"Importing 0 / {total} songs...")
        else:
            if self.loading_dialog is not None:
                self.loading_dialog.close()
                self.loading_dialog = None

    def _on_progress_update(self, processed: int, total: int, eta_seconds: Optional[float], current_name: str):
        if self.loading_dialog is not None:
            self.loading_dialog.update_progress(processed, total, eta_seconds, current_name)
        pct = int(processed / total * 100) if total else 100
        eta_txt = f" - ETA {format_time(int(eta_seconds * 1000))}" if eta_seconds is not None else ""
        self._update_status(f"Importing {processed} / {total} songs ({pct}%){eta_txt}")

    def _on_rows_removed(self, ids: List[str]):
        for song_id in ids:
            item = self.row_items.pop(song_id, None)
            self.songs_by_id.pop(song_id, None)
            if item is not None:
                self.table.removeRow(item.row())
        self._refresh_category_choices()

    def _on_scan_error(self, message: str):
        if self.loading_dialog is not None:
            self.loading_dialog.close()
            self.loading_dialog = None
        self.statusBar().showMessage(f"Error while scanning library: {message}", 6000)
        self._update_status("Error")

    def _on_scan_finished(self, added_titles: List[str], removed_titles: List[str], moved_count: int, worker: ScanWorker):
        if self.loading_dialog is not None:
            self.loading_dialog.close()
            self.loading_dialog = None

        self.cache = worker.new_cache
        if self.root_path is not None:
            save_cache(self.root_path, self.cache)

        self._refresh_category_choices()
        self._update_status(f"{len(self.songs_by_id)} songs \u2022 Up to date")

        if added_titles or removed_titles:
            self.last_added_titles = added_titles
            self.last_removed_titles = removed_titles
            self.changes_button.setEnabled(True)
            self.statusBar().showMessage(
                f"Library changed: +{len(added_titles)} added, -{len(removed_titles)} removed"
                + (f" ({moved_count} moved/renamed, not counted as a change)" if moved_count else ""),
                6000,
            )

    def _update_status(self, text: str):
        self.status_label.setText(text)

    def _show_changes_dialog(self):
        lines = []
        if self.last_added_titles:
            lines.append(f"Added ({len(self.last_added_titles)}):")
            lines.extend(f"  + {t}" for t in self.last_added_titles[:50])
            if len(self.last_added_titles) > 50:
                lines.append(f"  ... and {len(self.last_added_titles) - 50} more")
        if self.last_removed_titles:
            if lines:
                lines.append("")
            lines.append(f"Removed ({len(self.last_removed_titles)}):")
            lines.extend(f"  - {t}" for t in self.last_removed_titles[:50])
            if len(self.last_removed_titles) > 50:
                lines.append(f"  ... and {len(self.last_removed_titles) - 50} more")
        if not lines:
            lines.append("No recent additions or removals.")
        QMessageBox.information(self, "Recent library changes", "\n".join(lines))

    def _refresh_category_choices(self):
        cats = list_categories(self.root_path) if self.root_path else []
        if cats == self._last_known_categories:
            return
        self._last_known_categories = cats

        self.category_list.blockSignals(True)
        self.category_list.clear()
        for cat in cats:
            item = QListWidgetItem(cat)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if cat in self.checked_categories else Qt.Unchecked)
            self.category_list.addItem(item)
        self.category_list.blockSignals(False)
        self.checked_categories &= set(cats)

        for song_id, item in self.row_items.items():
            row = item.row()
            combo = self.table.cellWidget(row, COL_CATEGORY)
            if combo is None:
                continue
            current = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(cats)
            if current in cats:
                combo.setCurrentText(current)
            combo.blockSignals(False)

    def _on_category_filter_changed(self, item: QListWidgetItem):
        cat = item.text()
        if item.checkState() == Qt.Checked:
            self.checked_categories.add(cat)
        else:
            self.checked_categories.discard(cat)
        self._apply_filters()

    def _on_rating_filter_changed(self, value: int):
        self.min_rating_filter = value
        self._apply_filters()

    # ------------------------------------------------------------------
    # Table row management (incremental - the table is never torn down on a
    # filter/search change, so the filter selection is never lost).
    # ------------------------------------------------------------------
    def _matches_filters(self, song: Song) -> bool:
        if self.checked_categories and song.category not in self.checked_categories:
            return False
        search = self.search_box.text().strip().lower()
        if search and search not in song.title.lower() and search not in song.artist.lower():
            return False
        if self.min_rating_filter > 0 and song.rating < self.min_rating_filter:
            return False
        return True

    def _apply_filters(self):
        for song_id, song in self.songs_by_id.items():
            item = self.row_items.get(song_id)
            if item is None:
                continue
            self.table.setRowHidden(item.row(), not self._matches_filters(song))

    def _filtered_songs(self) -> List[Song]:
        return [s for s in self.songs_by_id.values() if self._matches_filters(s)]

    def _add_or_update_row(self, song: Song):
        self.songs_by_id[song.id] = song
        existing_item = self.row_items.get(song.id)
        if existing_item is not None:
            row = existing_item.row()
            existing_item.setText(song.title)
            artist_item = self.table.item(row, COL_ARTIST)
            if artist_item is not None:
                artist_item.setText(song.artist)
            combo = self.table.cellWidget(row, COL_CATEGORY)
            if combo is not None and combo.currentText() != song.category:
                combo.blockSignals(True)
                combo.setCurrentText(song.category)
                combo.blockSignals(False)
            star_widget = self.table.cellWidget(row, COL_RATING)
            if star_widget is not None:
                star_widget.set_rating(song.rating)
            self.table.setRowHidden(row, not self._matches_filters(song))
        else:
            self._insert_song_row(song)

    def _insert_song_row(self, song: Song):
        row = self.table.rowCount()
        self.table.insertRow(row)

        title_item = QTableWidgetItem(song.title)
        title_item.setData(Qt.UserRole, song.id)
        self.table.setItem(row, COL_TITLE, title_item)
        self.table.setItem(row, COL_ARTIST, QTableWidgetItem(song.artist))
        self.row_items[song.id] = title_item

        all_categories = list_categories(self.root_path) if self.root_path else []
        combo = QComboBox()
        combo.addItems(all_categories)
        if song.category in all_categories:
            combo.setCurrentText(song.category)
        combo.setEnabled(False)
        combo.currentTextChanged.connect(
            lambda new_cat, s=song, c=combo: self._on_category_combo_changed(s, c, new_cat)
        )
        self.table.setCellWidget(row, COL_CATEGORY, combo)

        cat_lock = LockButton(
            "Locked: category cannot be changed. Click to unlock.",
            "Unlocked: category can now be changed (this will move the file)."
        )
        cat_lock.toggledLock.connect(lambda unlocked, combo=combo: combo.setEnabled(unlocked))
        self.table.setCellWidget(row, COL_CATEGORY_LOCK, self._centered(cat_lock))

        star_widget = StarRatingWidget(song.rating)
        star_widget.set_editable(False)
        star_widget.ratingChanged.connect(lambda new_rating, s=song: self._on_rating_changed(s, new_rating))
        self.table.setCellWidget(row, COL_RATING, star_widget)

        rating_lock = LockButton(
            "Locked: rating cannot be changed. Click to unlock.",
            "Unlocked: rating can now be set/changed."
        )
        rating_lock.toggledLock.connect(lambda unlocked, star=star_widget: star.set_editable(unlocked))
        self.table.setCellWidget(row, COL_RATING_LOCK, self._centered(rating_lock))

        combo.setProperty("lock_ref", cat_lock)
        star_widget.setProperty("lock_ref", rating_lock)

        self.table.setRowHidden(row, not self._matches_filters(song))

    @staticmethod
    def _centered(widget: QWidget) -> QWidget:
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch()
        layout.addWidget(widget)
        layout.addStretch()
        return wrapper

    def _on_row_double_clicked(self, index):
        row = index.row()
        item = self.table.item(row, COL_TITLE)
        if item is None:
            return
        song_id = item.data(Qt.UserRole)
        song = self.songs_by_id.get(song_id)
        if song:
            self.player.play_song(song, queue=self._filtered_songs())

    def _play_all_filtered(self):
        songs = self._filtered_songs()
        if songs:
            self.player.play_song(songs[0], queue=songs)

    def _highlight_playing_row(self, playing_song: Optional[Song]):
        if self._currently_highlighted_id is not None:
            prev_item = self.row_items.get(self._currently_highlighted_id)
            if prev_item is not None:
                f = prev_item.font()
                f.setBold(False)
                prev_item.setFont(f)
                prev_item.setForeground(Qt.white)

        self._currently_highlighted_id = playing_song.id if playing_song else None

        if playing_song is not None:
            item = self.row_items.get(playing_song.id)
            if item is not None:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
                item.setForeground(Qt.cyan)

    # ------------------------------------------------------------------
    # Change category -> physically move the file
    # ------------------------------------------------------------------
    def _on_category_combo_changed(self, song: Song, combo: QComboBox, new_category: str):
        if new_category == song.category:
            return
        if not combo.isEnabled():
            return

        if self.player.current_song() is song and self.player.is_playing():
            QMessageBox.warning(
                self, "Playback in progress",
                "This song is currently playing. Please pause it first "
                "before moving it to another category."
            )
            combo.blockSignals(True)
            combo.setCurrentText(song.category)
            combo.blockSignals(False)
            return

        old_category = song.category
        old_path_str = str(song.path)
        try:
            new_path = safe_move_song(song, self.root_path, new_category)
        except SafeMoveError as e:
            QMessageBox.critical(
                self, "Move failed",
                f"The song could not be safely moved:\n{e}\n\n"
                "The original file was NOT modified."
            )
            combo.blockSignals(True)
            combo.setCurrentText(old_category)
            combo.blockSignals(False)
            return

        song.path = new_path
        song.category = new_category
        self.player.notify_song_path_changed(song, new_path)

        # Keep the on-disk cache in sync immediately so the next scan doesn't
        # need to re-read this file at all.
        rec = self.cache.pop(old_path_str, {})
        try:
            st = new_path.stat()
            rec.update({"size": st.st_size, "mtime": st.st_mtime, "title": song.title,
                        "artist": song.artist, "rating": song.rating,
                        "duration": song.duration, "id": song.id})
            self.cache[str(new_path)] = rec
            if self.root_path is not None:
                save_cache(self.root_path, self.cache)
        except OSError:
            pass

        lock: LockButton = combo.property("lock_ref")
        if lock is not None:
            lock.set_locked(True)
        combo.setEnabled(False)

        self.table.setRowHidden(self.row_items[song.id].row(), not self._matches_filters(song))
        self.statusBar().showMessage(
            f"'{song.title}' moved from '{old_category}' to '{new_category}'.", 4000
        )

    # ------------------------------------------------------------------
    # Change rating -> write into the file
    # ------------------------------------------------------------------
    def _on_rating_changed(self, song: Song, new_rating: int):
        ok = write_rating(song.path, new_rating)
        if not ok:
            QMessageBox.warning(
                self, "Rating not saved",
                f"The rating could not be written into the file '{song.filename}' "
                "(unsupported format or write error)."
            )
            return
        song.rating = new_rating

        try:
            st = song.path.stat()
            rec = self.cache.get(str(song.path), {})
            rec.update({"size": st.st_size, "mtime": st.st_mtime, "title": song.title,
                        "artist": song.artist, "rating": new_rating,
                        "duration": song.duration, "id": song.id})
            self.cache[str(song.path)] = rec
            if self.root_path is not None:
                save_cache(self.root_path, self.cache)
        except OSError:
            pass

        sender = self.sender()
        if isinstance(sender, StarRatingWidget):
            lock: LockButton = sender.property("lock_ref")
            if lock is not None:
                lock.set_locked(True)
            sender.set_editable(False)

        item = self.row_items.get(song.id)
        if item is not None:
            self.table.setRowHidden(item.row(), not self._matches_filters(song))
        self.statusBar().showMessage(f"Rating for '{song.title}' saved: {new_rating}/5", 3000)


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    app.setApplicationName(APP_NAME)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()