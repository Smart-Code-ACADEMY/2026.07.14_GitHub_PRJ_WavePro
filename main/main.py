#!/usr/bin/env python3
"""
Music Library App
=================
A single-file, modern, dark-mode (Apple-style) music library application.

Features:
- Add a root folder that contains one subfolder per category.
- All audio files inside those subfolders are loaded automatically.
- Each song shows its category (= the name of the folder it currently
  lives in). Renaming a category folder on disk updates the category
  everywhere automatically.
- Changing a song's category via the dropdown PHYSICALLY MOVES the file
  into the target folder. This is protected by a green/red "lock" button:
  green = locked (nothing can happen by accident), click to unlock (turns
  red), then change the category. After the move completes, the lock
  automatically turns green again.
- The move itself is crash-safe: the file is first copied to the target
  folder, the copy is verified (size match), only THEN is the original
  removed. Nothing is ever overwritten - name conflicts get a unique
  suffix instead.
- Star rating (0-5) per song, protected by a second lock (green/red) the
  same way. The rating is written DIRECTLY into the audio file's own
  metadata (ID3 / Vorbis comments / MP4 atoms depending on format), so it
  survives being moved anywhere, even outside the app.
- Built-in music player: play/pause, next/previous, seek, volume, repeat
  (off/all/one), category filter, search, double-click to play, "play all".
- Dark, modern, Apple-style UI.

Run with:
    pip install PySide6 mutagen
    python music_app.py

This is intentionally a SINGLE FILE so it can easily be frozen into a
standalone .exe later (e.g. with PyInstaller):
    pyinstaller --onefile --windowed music_app.py
"""

import os
import sys
import shutil
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

from PySide6.QtCore import QFileSystemWatcher, QObject, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QFont
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QSlider, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
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
# Library: Song data class, folder scanning, crash-safe move
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


def scan_library(root: Path) -> List[Song]:
    """Scans every subfolder of root and returns all songs found."""
    songs: List[Song] = []
    if not root.is_dir():
        return songs

    for folder in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        category = folder.name
        for file in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if not file.is_file():
                continue
            if file.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            title, artist = read_display_tags(file)
            rating = read_rating(file)
            duration = read_duration_seconds(file)
            songs.append(
                Song(path=file, category=category, title=title, artist=artist,
                     rating=rating, duration=duration)
            )
    return songs


def safe_move_song(song: Song, root: Path, target_category: str) -> Path:
    """
    Physically moves the file into the target category folder.
    Safety measures:
      - The original is NEVER deleted before the copy has been verified.
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
        # 1) Copy to a temporary name (the original stays untouched)
        shutil.copy2(src, tmp_dest)

        # 2) Verify (file size must match)
        if os.path.getsize(tmp_dest) != os.path.getsize(src):
            raise SafeMoveError("Verification failed (size mismatch)")

        # 3) Only now rename to the final name
        os.replace(tmp_dest, dest)

        # 4) Only after a verified copy, remove the original
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
        """Called when a song has been physically moved, so the player keeps
        the source in sync if it is currently loaded/playing."""
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
        self.setFixedSize(30, 30)
        f = QFont()
        f.setPointSize(13)
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
                " border-radius: 8px; color: #30D158; }"
                "QToolButton:hover { background: rgba(48,209,88,0.28); }"
            )
        else:
            self.setText("\U0001F513")  # open padlock
            self.setToolTip(self._tooltip_unlocked)
            self.setStyleSheet(
                "QToolButton { background: rgba(255,69,58,0.15); border: 1px solid #FF453A;"
                " border-radius: 8px; color: #FF453A; }"
                "QToolButton:hover { background: rgba(255,69,58,0.28); }"
            )


class StarRatingWidget(QWidget):
    """5 clickable stars. Locked (not editable) by default."""

    ratingChanged = Signal(int)

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
            btn.setText(char)
            color = "#FFD60A" if filled else "#5A5A5E"
            if not self._enabled_for_edit:
                color = "#3A3A3C" if not filled else "#8A7A2A"
            btn.setStyleSheet(
                f"QPushButton {{ border: none; background: transparent; color: {color}; font-size: 17px; }}"
            )
            btn.setEnabled(self._enabled_for_edit)


# ============================================================================
# Dark, Apple-style stylesheet
# ============================================================================
DARK_QSS = """
* {
    font-family: -apple-system, "SF Pro Text", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
    outline: none;
}

QMainWindow, QWidget#centralWidget {
    background-color: #1c1c1e;
}

QWidget {
    color: #f2f2f7;
    background-color: transparent;
}

QLabel#sectionTitle {
    font-size: 20px;
    font-weight: 600;
    color: #ffffff;
    padding: 4px 0px;
}

QLabel#subtleLabel {
    color: #8e8e93;
    font-size: 12px;
}

QLabel#pathLabel {
    color: #0a84ff;
    font-size: 12px;
    padding: 2px 8px;
    background: rgba(10,132,255,0.1);
    border-radius: 6px;
}

QToolBar, #topBar {
    background-color: #1c1c1e;
    border: none;
    padding: 10px;
    spacing: 8px;
}

QPushButton {
    background-color: #2c2c2e;
    border: 1px solid #3a3a3c;
    border-radius: 10px;
    padding: 8px 16px;
    color: #f2f2f7;
    font-size: 13px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: #3a3a3c;
    border: 1px solid #48484a;
}
QPushButton:pressed {
    background-color: #232325;
}
QPushButton:disabled {
    color: #5a5a5e;
    border: 1px solid #2c2c2e;
}

QPushButton#accentButton {
    background-color: #0a84ff;
    border: 1px solid #0a84ff;
    color: white;
    font-weight: 600;
}
QPushButton#accentButton:hover {
    background-color: #3399ff;
}

QPushButton#transportButton {
    background-color: transparent;
    border: none;
    border-radius: 22px;
    font-size: 18px;
    padding: 6px;
}
QPushButton#transportButton:hover {
    background-color: #2c2c2e;
}

QPushButton#playPauseButton {
    background-color: #ffffff;
    border-radius: 24px;
    color: #1c1c1e;
    font-size: 18px;
    min-width: 48px;
    min-height: 48px;
}
QPushButton#playPauseButton:hover {
    background-color: #e5e5ea;
}

QTableWidget {
    background-color: #1c1c1e;
    alternate-background-color: #202022;
    border: none;
    gridline-color: transparent;
    selection-background-color: rgba(10,132,255,0.25);
    selection-color: #ffffff;
}
QTableWidget::item {
    padding: 6px;
    border-bottom: 1px solid #2c2c2e;
}
QHeaderView::section {
    background-color: #1c1c1e;
    color: #8e8e93;
    border: none;
    border-bottom: 1px solid #2c2c2e;
    padding: 8px 6px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
}
QTableWidget::item:selected {
    background-color: rgba(10,132,255,0.22);
}

QComboBox {
    background-color: #2c2c2e;
    border: 1px solid #3a3a3c;
    border-radius: 8px;
    padding: 4px 10px;
    min-width: 110px;
    color: #f2f2f7;
}
QComboBox:disabled {
    color: #5a5a5e;
    background-color: #232325;
}
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox QAbstractItemView {
    background-color: #2c2c2e;
    border: 1px solid #3a3a3c;
    selection-background-color: #0a84ff;
    color: #f2f2f7;
    outline: none;
}

QListWidget {
    background-color: #232325;
    border: 1px solid #2c2c2e;
    border-radius: 10px;
    padding: 4px;
}
QListWidget::item {
    padding: 6px 8px;
    border-radius: 6px;
}
QListWidget::item:selected {
    background-color: rgba(10,132,255,0.25);
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0px;
}
QScrollBar::handle:vertical {
    background: #48484a;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover {
    background: #5a5a5e;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}

QSlider::groove:horizontal {
    height: 4px;
    background: #3a3a3c;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #0a84ff;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #ffffff;
    width: 12px;
    height: 12px;
    margin: -4px 0;
    border-radius: 6px;
}
QSlider::handle:horizontal:hover {
    background: #e5e5ea;
}

QLineEdit {
    background-color: #2c2c2e;
    border: 1px solid #3a3a3c;
    border-radius: 8px;
    padding: 6px 10px;
    color: #f2f2f7;
}
QLineEdit:focus {
    border: 1px solid #0a84ff;
}

QFrame#playerBar {
    background-color: #232325;
    border-top: 1px solid #2c2c2e;
}

QFrame#sidebar {
    background-color: #1c1c1e;
    border-right: 1px solid #2c2c2e;
}

QToolTip {
    background-color: #3a3a3c;
    color: #f2f2f7;
    border: 1px solid #48484a;
    padding: 4px 8px;
    border-radius: 6px;
}

QMessageBox {
    background-color: #2c2c2e;
}
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
        self.songs: List[Song] = []
        self.checked_categories: set = set()  # empty = show all

        self.player = PlayerController(self)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._on_dir_changed)
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(400)
        self._rescan_timer.timeout.connect(self.rescan_library)

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

        layout.addStretch()

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search by title or artist...")
        self.search_box.setFixedWidth(260)
        self.search_box.textChanged.connect(self._rebuild_table)
        layout.addWidget(self.search_box)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.rescan_library)
        layout.addWidget(refresh_btn)

        add_btn = QPushButton("Add Music Folder")
        add_btn.setObjectName("accentButton")
        add_btn.clicked.connect(self._choose_root_folder)
        layout.addWidget(add_btn)

        return bar

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
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

        hint = QLabel("Nothing checked = all categories are shown and played.")
        hint.setObjectName("subtleLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        play_all_btn = QPushButton("Play All")
        play_all_btn.clicked.connect(self._play_all_filtered)
        layout.addWidget(play_all_btn)

        return sidebar

    def _build_table(self) -> QWidget:
        self.table = QTableWidget(0, COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            ["Title", "Artist", "Category", "", "Rating", ""]
        )
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
        self.repeat_btn.setStyleSheet(
            "color: #0a84ff;" if new_mode != RepeatMode.OFF else ""
        )

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
        self.rescan_library()

        self.watcher.addPath(str(path))
        for sub in list_categories(path):
            self.watcher.addPath(str(path / sub))

    def _on_dir_changed(self, _changed_path: str):
        # Debounce: several events in quick succession -> rescan only once
        self._rescan_timer.start()

    def rescan_library(self):
        if self.root_path is None:
            return
        currently_playing = self.player.current_song()
        currently_playing_path = currently_playing.path if currently_playing else None

        self.songs = scan_library(self.root_path)

        watched = set(self.watcher.directories())
        wanted = {str(self.root_path)} | {str(self.root_path / c) for c in list_categories(self.root_path)}
        to_remove = list(watched - wanted)
        to_add = list(wanted - watched)
        if to_remove:
            self.watcher.removePaths(to_remove)
        if to_add:
            self.watcher.addPaths(to_add)

        self._rebuild_category_filter()
        self._rebuild_table()

        if currently_playing_path:
            for s in self.songs:
                if s.path == currently_playing_path:
                    break

    def _rebuild_category_filter(self):
        self.category_list.blockSignals(True)
        self.category_list.clear()
        categories = list_categories(self.root_path) if self.root_path else []
        for cat in categories:
            item = QListWidgetItem(cat)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if cat in self.checked_categories else Qt.Unchecked)
            self.category_list.addItem(item)
        self.category_list.blockSignals(False)
        self.checked_categories &= set(categories)

    def _on_category_filter_changed(self, item: QListWidgetItem):
        cat = item.text()
        if item.checkState() == Qt.Checked:
            self.checked_categories.add(cat)
        else:
            self.checked_categories.discard(cat)
        self._rebuild_table()

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------
    def _filtered_songs(self) -> List[Song]:
        search = self.search_box.text().strip().lower()
        result = []
        for s in self.songs:
            if self.checked_categories and s.category not in self.checked_categories:
                continue
            if search and search not in s.title.lower() and search not in s.artist.lower():
                continue
            result.append(s)
        return result

    def _rebuild_table(self):
        songs = self._filtered_songs()
        self.table.setRowCount(0)
        self.table.setRowCount(len(songs))
        all_categories = list_categories(self.root_path) if self.root_path else []

        for row, song in enumerate(songs):
            title_item = QTableWidgetItem(song.title)
            title_item.setData(Qt.UserRole, song.id)
            self.table.setItem(row, COL_TITLE, title_item)
            self.table.setItem(row, COL_ARTIST, QTableWidgetItem(song.artist))

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
            cat_lock.toggledLock.connect(
                lambda unlocked, combo=combo: combo.setEnabled(unlocked)
            )
            self.table.setCellWidget(row, COL_CATEGORY_LOCK, self._centered(cat_lock))

            star_widget = StarRatingWidget(song.rating)
            star_widget.set_editable(False)
            star_widget.ratingChanged.connect(
                lambda new_rating, s=song: self._on_rating_changed(s, new_rating)
            )
            self.table.setCellWidget(row, COL_RATING, star_widget)

            rating_lock = LockButton(
                "Locked: rating cannot be changed. Click to unlock.",
                "Unlocked: rating can now be set/changed."
            )
            rating_lock.toggledLock.connect(
                lambda unlocked, star=star_widget: star.set_editable(unlocked)
            )
            self.table.setCellWidget(row, COL_RATING_LOCK, self._centered(rating_lock))

            # Keep references so we can auto re-lock after a change
            combo.setProperty("lock_ref", cat_lock)
            star_widget.setProperty("lock_ref", rating_lock)

        self._highlight_playing_row(self.player.current_song())

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
        song = next((s for s in self.songs if s.id == song_id), None)
        if song:
            self.player.play_song(song, queue=self._filtered_songs())

    def _play_all_filtered(self):
        songs = self._filtered_songs()
        if songs:
            self.player.play_song(songs[0], queue=songs)

    def _highlight_playing_row(self, playing_song: Optional[Song]):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_TITLE)
            if item is None:
                continue
            is_playing = playing_song is not None and item.data(Qt.UserRole) == playing_song.id
            f = item.font()
            f.setBold(is_playing)
            item.setFont(f)
            item.setForeground(Qt.cyan if is_playing else Qt.white)

    # ------------------------------------------------------------------
    # Change category -> physically move the file
    # ------------------------------------------------------------------
    def _on_category_combo_changed(self, song: Song, combo: QComboBox, new_category: str):
        if new_category == song.category:
            return
        if not combo.isEnabled():
            return  # should not happen because of the lock, but just in case

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

        # Automatically re-lock (green)
        lock: LockButton = combo.property("lock_ref")
        if lock is not None:
            lock.set_locked(True)
        combo.setEnabled(False)

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
        sender = self.sender()
        if isinstance(sender, StarRatingWidget):
            lock: LockButton = sender.property("lock_ref")
            if lock is not None:
                lock.set_locked(True)
            sender.set_editable(False)
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