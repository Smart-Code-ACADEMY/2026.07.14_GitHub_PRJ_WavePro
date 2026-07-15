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

from PySide6.QtCore import (
    QFileSystemWatcher, QObject, QSettings, QStandardPaths, Qt, QThread,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import QFont
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
        batch: List[Song] = []

        for p in unchanged:
            cat, size, mtime = current[p]
            rec = old[p]
            song = Song(path=Path(p), category=cat,
                        title=rec.get("title", Path(p).stem),
                        artist=rec.get("artist", ""),
                        rating=rec.get("rating", 0),
                        duration=rec.get("duration", 0.0),
                        id=rec.get("id") or uuid.uuid4().hex)
            new_cache[p] = {**rec, "size": size, "mtime": mtime, "id": song.id}
            batch.append(song)

        for old_p, new_p in moved_pairs:
            cat, size, mtime = current[new_p]
            rec = old.get(old_p, {})
            song = Song(path=Path(new_p), category=cat,
                        title=rec.get("title", Path(new_p).stem),
                        artist=rec.get("artist", ""),
                        rating=rec.get("rating", 0),
                        duration=rec.get("duration", 0.0),
                        id=rec.get("id") or uuid.uuid4().hex)
            new_cache[new_p] = {**rec, "size": size, "mtime": mtime, "id": song.id}
            batch.append(song)

        if batch:
            self.batchReady.emit(batch)

        slow = truly_added + changed
        self.totalDetermined.emit(len(slow))
        start = time.monotonic()
        for i, p in enumerate(slow, 1):
            cat, size, mtime = current[p]
            po = Path(p)
            title, artist = read_display_tags(po)
            rating   = read_rating(po)
            duration = read_duration_seconds(po)
            old_id   = old.get(p, {}).get("id")
            song = Song(path=po, category=cat, title=title, artist=artist,
                        rating=rating, duration=duration,
                        id=old_id or uuid.uuid4().hex)
            new_cache[p] = {"size": size, "mtime": mtime, "title": title,
                            "artist": artist, "rating": rating,
                            "duration": duration, "id": song.id}
            elapsed = time.monotonic() - start
            rate    = i / elapsed if elapsed > 0 else 0
            eta     = (len(slow) - i) / rate if rate > 0 else None
            self.progressUpdate.emit(i, len(slow), eta, po.name)
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
    OFF = 0
    ONE = 1
    ALL = 2


class PlayerController(QObject):
    songChanged          = Signal(object)
    playbackStateChanged = Signal(bool)
    positionChanged      = Signal(int)
    durationChanged      = Signal(int)
    repeatModeChanged    = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._player = QMediaPlayer(self)
        self._audio  = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._queue: List[Song] = []
        self._index = -1
        self._repeat = RepeatMode.OFF
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

    def toggle_play_pause(self):
        if self._index < 0: return
        self._player.pause() if self.is_playing() else self._player.play()

    def next(self):
        if not self._queue: return
        if self._repeat == RepeatMode.ONE:
            self._load(autoplay=True); return
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
    """Green = locked (safe), red = unlocked (editable)."""
    toggledLock = Signal(bool)   # True = now unlocked

    def __init__(self, tip_locked: str, tip_unlocked: str, parent=None):
        super().__init__(parent)
        self._locked = True
        self._tip_locked   = tip_locked
        self._tip_unlocked = tip_unlocked
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(20, 20)
        f = QFont(); f.setPointSize(9); self.setFont(f)
        self.clicked.connect(self._toggle)
        self._refresh()

    def is_locked(self) -> bool:
        return self._locked

    def set_locked(self, v: bool, emit=False):
        self._locked = v; self._refresh()
        if emit: self.toggledLock.emit(not v)

    def _toggle(self):
        self._locked = not self._locked
        self._refresh()
        self.toggledLock.emit(not self._locked)

    def _refresh(self):
        if self._locked:
            self.setText("\U0001F512")
            self.setToolTip(self._tip_locked)
            self.setStyleSheet(
                "QToolButton{background:rgba(48,209,88,.15);border:1px solid #30D158;"
                "border-radius:5px;color:#30D158;padding:1px;}"
                "QToolButton:hover{background:rgba(48,209,88,.28);}")
        else:
            self.setText("\U0001F513")
            self.setToolTip(self._tip_unlocked)
            self.setStyleSheet(
                "QToolButton{background:rgba(255,69,58,.15);border:1px solid #FF453A;"
                "border-radius:5px;color:#FF453A;padding:1px;}"
                "QToolButton:hover{background:rgba(255,69,58,.28);}")


# ============================================================================
# StarRatingWidget
# ============================================================================
class StarRatingWidget(QWidget):
    """
    5 stars: gray outline when unrated, yellow when rated.
    Min = 0 (all gray), Max = 5 (all yellow).
    Clicking the active star again resets to 0.
    Editable only after the corresponding lock is opened.
    """
    ratingChanged = Signal(int)
    COLOR_FILLED = "#FFD60A"
    COLOR_EMPTY  = "#6e6e73"

    def __init__(self, rating: int = 0, parent=None):
        super().__init__(parent)
        self._rating   = _clamp(rating)
        self._editable = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(1)
        self._btns: List[QPushButton] = []
        for i in range(5):
            btn = QPushButton()
            btn.setFlat(True)
            btn.setFixedSize(22, 22)
            btn.setCursor(Qt.PointingHandCursor)
            # critical: use default-arg capture to avoid closure-over-loop-var bug
            btn.clicked.connect(lambda _checked=False, idx=i: self._on_click(idx))
            lay.addWidget(btn)
            self._btns.append(btn)
        lay.addStretch()
        self._refresh()

    # public API -------------------------------------------------------
    def rating(self) -> int:
        return self._rating

    def set_rating(self, v: int):
        self._rating = _clamp(v)
        self._refresh()

    def set_editable(self, v: bool):
        self._editable = v
        self._refresh()

    # internal ---------------------------------------------------------
    def _on_click(self, idx: int):
        if not self._editable:
            return
        new = idx + 1
        if new == self._rating:
            new = 0          # click active star again → reset to 0
        self._rating = new
        self._refresh()
        self.ratingChanged.emit(self._rating)

    def _refresh(self):
        for i, btn in enumerate(self._btns):
            filled = i < self._rating
            color  = self.COLOR_FILLED if filled else self.COLOR_EMPTY
            btn.setText("\u2605" if filled else "\u2606")
            btn.setStyleSheet(
                f"QPushButton{{border:none;background:transparent;"
                f"color:{color};font-size:16px;}}"
                f"QPushButton:disabled{{color:{color};}}"
            )
            # Enable buttons only when editable so clicks actually reach _on_click.
            # The colour override in :disabled ensures stars look the same either way.
            btn.setEnabled(self._editable)


# ============================================================================
# Loading dialog
# ============================================================================
class LoadingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Importing Music")
        self.setModal(False); self.setFixedSize(460, 160)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20); lay.setSpacing(10)
        lbl = QLabel("Importing songs…"); lbl.setObjectName("sectionTitle")
        lay.addWidget(lbl)
        self.bar = QProgressBar(); self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self.detail = QLabel(""); self.detail.setObjectName("subtleLabel")
        self.detail.setWordWrap(True); lay.addWidget(self.detail)
        self.eta_lbl = QLabel(""); self.eta_lbl.setObjectName("subtleLabel")
        lay.addWidget(self.eta_lbl); lay.addStretch()

    def update_progress(self, done: int, total: int,
                        eta_s: Optional[float], name: str):
        pct = int(done / total * 100) if total else 100
        self.bar.setValue(pct)
        self.detail.setText(f"{done} / {total} songs  ({pct}%)\n{name}")
        self.eta_lbl.setText(
            f"Estimated time remaining: {_fmt_time(int(eta_s * 1000))}"
            if eta_s is not None else "Estimating time remaining…")


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

QPushButton#transportButton { background:transparent; border:none;
                              border-radius:22px; font-size:18px; padding:6px; }
QPushButton#transportButton:hover { background:#2c2c2e; }

QPushButton#playPauseButton { background:#fff; border-radius:24px; color:#1c1c1e;
                              font-size:18px; min-width:48px; min-height:48px; }
QPushButton#playPauseButton:hover { background:#e5e5ea; }

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

QScrollBar:vertical { background:transparent; width:10px; }
QScrollBar::handle:vertical { background:#48484a; border-radius:5px; min-height:24px; }
QScrollBar::handle:vertical:hover { background:#5a5a5e; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }

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

QFrame#playerBar { background:#232325; border-top:1px solid #2c2c2e; }

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


def _centered(widget: QWidget) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(6, 0, 6, 0)
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
COL_COUNT         = 5

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
        self.loading_dialog: Optional[LoadingDialog] = None
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

    # ── top bar ──────────────────────────────────────────────────────
    def _build_top_bar(self) -> QWidget:
        bar = QFrame(); bar.setObjectName("topBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 10, 16, 10); lay.setSpacing(10)

        title = QLabel("Music Library"); title.setObjectName("sectionTitle")
        lay.addWidget(title)

        self.path_label = QLabel("No folder selected")
        self.path_label.setObjectName("pathLabel")
        lay.addWidget(self.path_label)

        self.status_label = QLabel("")
        self.status_label.setObjectName("statusLabel")
        lay.addWidget(self.status_label)

        self.changes_btn = QPushButton("View changes")
        self.changes_btn.setEnabled(False)
        self.changes_btn.clicked.connect(self._show_changes)
        lay.addWidget(self.changes_btn)

        lay.addStretch()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._start_scan)
        lay.addWidget(refresh_btn)

        add_btn = QPushButton("Add Music Folder")
        add_btn.setObjectName("accentButton")
        add_btn.clicked.connect(self._choose_folder)
        lay.addWidget(add_btn)
        return bar

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
        self.rat_filter.setFixedWidth(150)
        self.rat_filter.addItem("All ratings", 0)
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
            ["Title", "Artist", "Category", "", "Rating  ★"])
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
        self.table.doubleClicked.connect(self._on_double_click)
        return self.table

    # ── player bar ────────────────────────────────────────────────────
    def _build_player_bar(self) -> QWidget:
        bar = QFrame(); bar.setObjectName("playerBar"); bar.setFixedHeight(92)
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(20, 6, 20, 8); outer.setSpacing(4)

        seek_row = QHBoxLayout()
        self.lbl_pos = QLabel("00:00"); self.lbl_pos.setObjectName("subtleLabel")
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderMoved.connect(self.player.seek)
        self.lbl_dur = QLabel("00:00"); self.lbl_dur.setObjectName("subtleLabel")
        seek_row.addWidget(self.lbl_pos)
        seek_row.addWidget(self.seek_slider, stretch=1)
        seek_row.addWidget(self.lbl_dur)
        outer.addLayout(seek_row)

        ctrl = QHBoxLayout(); ctrl.setSpacing(12)
        self.lbl_now = QLabel("Nothing is playing")
        self.lbl_now.setObjectName("subtleLabel")
        self.lbl_now.setMinimumWidth(260)
        ctrl.addWidget(self.lbl_now, stretch=1)
        ctrl.addStretch()

        prev_btn = QPushButton("\u23EE"); prev_btn.setObjectName("transportButton")
        prev_btn.clicked.connect(self.player.previous); ctrl.addWidget(prev_btn)

        self.pp_btn = QPushButton("\u25B6"); self.pp_btn.setObjectName("playPauseButton")
        self.pp_btn.clicked.connect(self.player.toggle_play_pause)
        ctrl.addWidget(self.pp_btn)

        next_btn = QPushButton("\u23ED"); next_btn.setObjectName("transportButton")
        next_btn.clicked.connect(self.player.next); ctrl.addWidget(next_btn)

        self.rep_btn = QPushButton("\U0001F501")
        self.rep_btn.setObjectName("transportButton")
        self.rep_btn.setToolTip("Repeat: Off")
        self.rep_btn.clicked.connect(self._cycle_repeat)
        ctrl.addWidget(self.rep_btn)

        ctrl.addStretch()
        ctrl.addWidget(QLabel("\U0001F50A"))
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100); self.vol_slider.setValue(80)
        self.vol_slider.setFixedWidth(110)
        self.vol_slider.valueChanged.connect(self.player.set_volume)
        ctrl.addWidget(self.vol_slider)
        self.player.set_volume(80)
        outer.addLayout(ctrl)
        return bar

    # ------------------------------------------------------------------
    # Player wiring
    # ------------------------------------------------------------------
    def _connect_player(self):
        self.player.songChanged.connect(self._on_song_changed)
        self.player.playbackStateChanged.connect(
            lambda playing: self.pp_btn.setText("\u23F8" if playing else "\u25B6"))
        self.player.positionChanged.connect(self._on_pos)
        self.player.durationChanged.connect(
            lambda d: (self.seek_slider.setRange(0, max(0, d)),
                       self.lbl_dur.setText(_fmt_time(d))))

    def _on_song_changed(self, song: Optional[Song]):
        if song is None:
            self.lbl_now.setText("Nothing is playing")
        else:
            ap = f" - {song.artist}" if song.artist else ""
            self.lbl_now.setText(f"{song.title}{ap}  \u2022  {song.category}")
        self._highlight(song)

    def _on_pos(self, ms: int):
        if not self.seek_slider.isSliderDown():
            self.seek_slider.setValue(ms)
        self.lbl_pos.setText(_fmt_time(ms))

    def _cycle_repeat(self):
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE]
        new   = order[(order.index(self.player.repeat_mode()) + 1) % 3]
        self.player.set_repeat_mode(new)
        labels = {RepeatMode.OFF: "Off", RepeatMode.ALL: "All", RepeatMode.ONE: "One"}
        self.rep_btn.setToolTip(f"Repeat: {labels[new]}")
        self.rep_btn.setStyleSheet("color:#0a84ff;" if new != RepeatMode.OFF else "")

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
        self.path_label.setText(str(path))
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
        for s in songs: self._add_or_update(s)
        self._rebuild_cat_filter(); self._apply_filters()
        self._set_status(f"{len(self.songs_by_id)} songs")

    def _on_song_ready(self, song: Song):
        self._add_or_update(song); self._apply_filters()

    def _on_total(self, total: int):
        if total > 0:
            if not self.loading_dialog:
                self.loading_dialog = LoadingDialog(self)
            self.loading_dialog.update_progress(0, total, None, "")
            self.loading_dialog.show(); self.loading_dialog.raise_()
            self._set_status(f"Importing 0 / {total}…")
        else:
            if self.loading_dialog:
                self.loading_dialog.close(); self.loading_dialog = None

    def _on_progress(self, done: int, total: int, eta: Optional[float], name: str):
        if self.loading_dialog:
            self.loading_dialog.update_progress(done, total, eta, name)
        pct   = int(done / total * 100) if total else 100
        eta_t = f" – ETA {_fmt_time(int(eta * 1000))}" if eta else ""
        self._set_status(f"Importing {done}/{total} ({pct}%){eta_t}")

    def _on_removed(self, ids: List[str]):
        for sid in ids:
            item = self.row_items.pop(sid, None)
            self.songs_by_id.pop(sid, None)
            if item is not None:
                self.table.removeRow(item.row())
        self._rebuild_cat_filter()

    def _on_scan_error(self, msg: str):
        if self.loading_dialog: self.loading_dialog.close(); self.loading_dialog = None
        self.statusBar().showMessage(f"Scan error: {msg}", 6000)
        self._set_status("Error")

    def _on_finished(self, added: List[str], removed: List[str],
                     moved: int, w: ScanWorker):
        if self.loading_dialog: self.loading_dialog.close(); self.loading_dialog = None
        self.cache = w.new_cache
        if self.root_path: save_cache(self.root_path, self.cache)
        self._rebuild_cat_filter()
        self._set_status(f"{len(self.songs_by_id)} songs \u2022 Up to date")
        if added or removed:
            self.last_added = added; self.last_removed = removed
            self.changes_btn.setEnabled(True)
            self.statusBar().showMessage(
                f"Library changed: +{len(added)} added, -{len(removed)} removed"
                + (f" ({moved} moved)" if moved else ""), 6000)

    def _set_status(self, txt: str):
        self.status_label.setText(txt)

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
            QMessageBox.information(self, "Nothing to play",
                                    "No songs match the current filter selection.")
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
            "Locked: cannot change category. Click to unlock.",
            "Unlocked: changing category will MOVE the file.")
        cat_lock.toggledLock.connect(lambda unlocked, c=combo: c.setEnabled(unlocked))
        self.table.setCellWidget(row, COL_CATEGORY_LOCK, _centered(cat_lock))

        # ── Star rating (always directly clickable – no lock) ──
        # Stars are always editable. Clicking a star immediately writes
        # the rating into the audio file and shows a confirmation toast.
        star_wrapper = QWidget()
        star_lay = QHBoxLayout(star_wrapper)
        star_lay.setContentsMargins(4, 0, 16, 0)   # 16 px right padding before scrollbar
        star_lay.setSpacing(0)
        star = StarRatingWidget(song.rating)
        star.set_editable(True)   # always editable, no lock needed
        star.ratingChanged.connect(lambda nr, s=song, sw=star: self._on_rating(s, sw, nr))
        star_lay.addWidget(star)
        star_lay.addStretch()
        self.table.setCellWidget(row, COL_RATING, star_wrapper)

        combo.setProperty("lock_ref", cat_lock)

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
            QMessageBox.warning(self, "Playback in progress",
                                "Pause the song first before moving it to another category.")
            combo.blockSignals(True); combo.setCurrentText(song.category); combo.blockSignals(False)
            return

        old_cat, old_path_str = song.category, str(song.path)
        try:
            new_path = safe_move_song(song, self.root_path, new_cat)
        except SafeMoveError as e:
            QMessageBox.critical(self, "Move failed",
                                 f"Could not safely move the file:\n{e}\n\nOriginal was NOT modified.")
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
        self.statusBar().showMessage(
            f"'{song.title}' moved: {old_cat} → {new_cat}", 4000)

    # ------------------------------------------------------------------
    # Rating change → write directly into the audio file
    # ------------------------------------------------------------------
    def _on_rating(self, song: Song, star_widget: "StarRatingWidget", new_rating: int):
        if not write_rating(song.path, new_rating):
            QMessageBox.warning(self, "Rating not saved",
                                f"Could not write rating into '{song.filename}'.\n"
                                "The format may not support embedded ratings.")
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

        # Re-evaluate row visibility (rating filter may now hide/show this song)
        item = self.row_items.get(song.id)
        if item:
            self.table.setRowHidden(item.row(), not self._matches(song))

        # Popup confirmation: rating saved directly into the file
        stars = "★" * new_rating + "☆" * (5 - new_rating)
        if new_rating == 0:
            msg = f"Rating removed for:\n{song.title}"
        else:
            msg = f"Rating saved directly into file:\n{song.title}\n\n{stars}  ({new_rating}/5)"
        QMessageBox.information(self, "Rating saved", msg)


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