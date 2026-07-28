#!/usr/bin/env python3
"""
Music Library App
=================
Single-file, modern dark-mode (Apple-style) music library + player.

Run:
    pip install PySide6 mutagen pynput
    python music_app.py

Build .exe:
    pyinstaller --onefile --windowed music_app.py

────────────────────────────────────────────────────────────────────────
KEYBOARD SHORTCUTS  (all work globally — even when app is in background)
────────────────────────────────────────────────────────────────────────
  Ctrl + Space                       Play / Pause
  MediaNext                          Next song
  MediaPrev                          Previous song
  MediaMute                          Mute / Unmute
  Ctrl+E + →                         Seek forward  (5s → 30s accel.)
  Ctrl+E + ←                         Seek backward (5s → 30s accel.)
  Ctrl+E + ↑                         Volume up     (+1 → +5 accel.)
  Ctrl+E + ↓                         Volume down   (−1 → −5 accel.)
  Ctrl+E + 0..5                      Rate current song 0–5 stars
  Ctrl+E + T                         Open "Add To-Do" dialog
────────────────────────────────────────────────────────────────────────

Background hotkeys require `pip install pynput`. Without it, shortcuts
still work but only when the app window has focus.
"""

import hashlib
import json
import os
import shutil
import sys
import time
import threading
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
    title  = path.stem
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
    new_path = path
    try:
        current_stem = path.stem
        if title and title != current_stem:
            new_name = title + path.suffix
            candidate = path.parent / new_name
            if candidate.exists() and candidate.resolve() != path.resolve():
                counter = 1
                while candidate.exists():
                    candidate = path.parent / f"{title} ({counter}){path.suffix}"
                    counter += 1
            os.rename(path, candidate)
            new_path = candidate

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
            popm_frames = id3.getall("POPM")
            if popm_frames:
                return _popm_to_stars(popm_frames[0].rating)
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
    rating = _clamp(rating)
    ext = path.suffix.lower()

    _STAR_TO_POPM = {0: 0, 1: 1, 2: 64, 3: 128, 4: 196, 5: 255}

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

def _bg_write_rating(path: Path, rating: int):
    """Write rating in background thread — GUI never blocks."""
    threading.Thread(target=write_rating, args=(path, rating), daemon=True).start()

def _bg_move_song(song, root, new_cat, callback):
    def _do():
        try:
            new_path = safe_move_song(song, root, new_cat)
            callback(new_path)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


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

        fast_items: List[Tuple[str, bool]] = (
            [(p, False) for p in unchanged] +
            [(new_p, False) for _, new_p in moved_pairs]
        )
        slow_items: List[str] = truly_added + changed

        total = len(fast_items) + len(slow_items)
        self.totalDetermined.emit(total)

        start = time.monotonic()
        done  = 0

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

            cache_fresh = (rec.get("mtime") is not None
                          and abs(rec.get("mtime", 0) - mtime) < 1.0
                          and rec.get("size") == size
                          and "title" in rec)

            if cache_fresh:
                title    = path_obj.stem
                artist   = rec.get("artist", "")
                rating   = rec.get("rating", 0)
                duration = rec.get("duration", 0.0)
            else:
                title, artist = read_display_tags(path_obj)
                rating   = read_rating(path_obj)
                duration = rec.get("duration", 0.0)

            song = Song(
                path=path_obj, category=cat,
                title=title, artist=artist,
                rating=rating, duration=duration,
                id=rec.get("id") or uuid.uuid4().hex,
            )
            new_cache[p] = {**rec, "size": size, "mtime": mtime,
                            "title": title, "artist": artist,
                            "rating": rating, "id": song.id}
            done += 1
            elapsed = time.monotonic() - start
            rate    = done / elapsed if elapsed > 0.001 else 0
            eta     = (total - done) / rate if rate > 0 else None
            self.progressUpdate.emit(done, total, eta, song.title)
            self.songReady.emit(song)

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
# Global hotkey listener  (works even when app is in background)
# ============================================================================
class GlobalHotkeyListener(QObject):
    """Listens for hotkeys globally using pynput. Works when other apps
    (Word, Chrome, etc.) have focus.

    Shortcut scheme:
      - MediaNext / MediaPrev / MediaMute       — no modifier
      - Ctrl + Space                            — play/pause
      - Ctrl + E (held) + action key            — everything else
    """

    # Signal emits qt_key_code
    commandReceived = Signal(int)

    _PYNPUT_TO_QT = {}  # populated at start()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ctrl_held  = False
        self._e_held     = False
        self._listener   = None
        self._available  = False

    def start(self):
        try:
            from pynput import keyboard as _kb
            self._kb = _kb

            self._PYNPUT_TO_QT = {
                _kb.Key.left:  Qt.Key_Left,
                _kb.Key.right: Qt.Key_Right,
                _kb.Key.up:    Qt.Key_Up,
                _kb.Key.down:  Qt.Key_Down,
            }
            # digits / letters handled dynamically in _map_key

            self._listener = _kb.Listener(
                on_press=self._on_press,
                on_release=self._on_release)
            self._listener.daemon = True
            self._listener.start()
            self._available = True
        except Exception as e:
            print(f"WARN: Global hotkeys unavailable: {e}")
            self._available = False

    def stop(self):
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass

    def is_available(self) -> bool:
        return self._available

    def _is_e_key(self, key) -> bool:
        """Detect the letter E as a held modifier — vk 69 or char 'e'/'E'."""
        if hasattr(key, 'vk') and key.vk == 69:  # VK_E
            return True
        ch = getattr(key, 'char', None)
        if ch and ch.lower() == 'e':
            return True
        return False

    def _on_press(self, key):
        try:
            _kb = self._kb

            # ─── Track Ctrl ──────────────────────────────────────
            if key in (_kb.Key.ctrl_l, _kb.Key.ctrl_r, _kb.Key.ctrl):
                self._ctrl_held = True
                return

            # ─── Track E (only while Ctrl is held) ───────────────
            if self._ctrl_held and self._is_e_key(key):
                self._e_held = True
                return

            # ─── Zero-modifier media keys ────────────────────────
            if key == _kb.Key.media_next:
                self.commandReceived.emit(Qt.Key_MediaNext); return
            if key == _kb.Key.media_previous:
                self.commandReceived.emit(Qt.Key_MediaPrevious); return
            if key == _kb.Key.media_volume_mute:
                self.commandReceived.emit(Qt.Key_VolumeMute); return

            # ─── Ctrl + Space → Play/Pause ───────────────────────
            if self._ctrl_held and key == _kb.Key.space:
                self.commandReceived.emit(Qt.Key_Space); return

            # ─── Ctrl + E + <action> ─────────────────────────────
            if self._ctrl_held and self._e_held:
                qt_key = self._map_key(key)
                if qt_key is not None:
                    self.commandReceived.emit(qt_key)

        except Exception as e:
            print(f"Global hotkey error: {e}")

    def _on_release(self, key):
        try:
            _kb = self._kb
            if key in (_kb.Key.ctrl_l, _kb.Key.ctrl_r, _kb.Key.ctrl):
                self._ctrl_held = False
                self._e_held    = False   # releasing Ctrl always drops E
            elif self._is_e_key(key):
                self._e_held = False
        except Exception:
            pass

    def _map_key(self, key) -> Optional[int]:
        """Map a pynput key to a Qt key code.

        CRITICAL: When Ctrl is held, Windows/pynput delivers a control
        character for letters (e.g. Ctrl+O arrives as char '\\x0f', not
        'o'). So we MUST prefer the virtual-key code (vk) over char.
        Falling back to char only when vk is unavailable.
        """
        qt = self._PYNPUT_TO_QT.get(key)
        if qt is not None:
            return qt
        vk = getattr(key, 'vk', None)
        if vk is not None:
            if 48 <= vk <= 57:   # 0-9
                return Qt.Key_0 + (vk - 48)
            if 65 <= vk <= 90:   # A-Z
                return Qt.Key_A + (vk - 65)
        ch = getattr(key, 'char', None)
        if ch and len(ch) == 1 and ' ' <= ch <= '~':
            c = ch.lower()
            if '0' <= c <= '9':
                return Qt.Key_0 + (ord(c) - ord('0'))
            if 'a' <= c <= 'z':
                return Qt.Key_A + (ord(c) - ord('a'))
        return None


# ============================================================================
# Background task worker  (non-blocking file I/O with interrupts)
# ============================================================================
class TaskWorker(QThread):
    taskDone   = Signal(str, object)
    taskFailed = Signal(str, str)
    queueEmpty = Signal()
    progressUpdate = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue: List[dict] = []
        self._lock = threading.Lock()
        self._running = True
        self._wake = threading.Event()
        # Proper pause mechanism: hold the lock, use a Condition to wait
        # for _paused → False. Event.wait() returned immediately when the
        # event was already set, which was the bug in the previous impl.
        self._pause_cond = threading.Condition()
        self._paused = False
        # Task currently being executed (readable from any thread)
        self._current_task_id: Optional[str] = None

    def add_task(self, task: dict, priority: bool = False):
        with self._lock:
            if priority:
                self._queue.insert(0, task)
            else:
                self._queue.append(task)
        self._wake.set()

    def interrupt(self):
        """Pause task processing between tasks. The current task, if any,
        completes atomically first — file operations must never be halted
        mid-write. UI stays responsive because tasks run on this thread."""
        with self._pause_cond:
            self._paused = True

    def resume(self):
        with self._pause_cond:
            self._paused = False
            self._pause_cond.notify_all()
        self._wake.set()

    def is_paused(self) -> bool:
        with self._pause_cond:
            return self._paused

    def current_task_id(self) -> Optional[str]:
        return self._current_task_id

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def pending_tasks(self) -> list:
        with self._lock:
            return list(self._queue)

    def stop(self):
        self._running = False
        # Force-unpause so the thread can exit
        with self._pause_cond:
            self._paused = False
            self._pause_cond.notify_all()
        self._wake.set()
        self.wait(10000)

    def get_serializable_queue(self) -> list:
        with self._lock:
            safe = []
            for t in self._queue:
                st = {k: (str(v) if isinstance(v, Path) else v)
                      for k, v in t.items()
                      if k != '_song_ref'}
                safe.append(st)
            return safe

    def run(self):
        while self._running:
            self._wake.wait(timeout=1.0)
            self._wake.clear()

            while self._running:
                # ─── Wait if paused (UI activity in progress) ───
                with self._pause_cond:
                    while self._paused and self._running:
                        self._pause_cond.wait(timeout=0.5)
                    if not self._running:
                        break

                # ─── Pop next task ──────────────────────────────
                with self._lock:
                    if not self._queue:
                        break
                    task = self._queue.pop(0)

                self._current_task_id = task.get('id', '')

                remaining = self.pending_count()
                total = remaining + 1
                self.progressUpdate.emit(total - remaining, total)

                # ─── Execute task atomically ────────────────────
                # We intentionally do NOT check _paused inside file
                # operations. A single file operation (move/rename) is
                # allowed to complete without interruption — otherwise
                # we'd risk file corruption.
                try:
                    result = self._execute(task)
                    self.taskDone.emit(task.get('id', ''), result)
                except Exception as e:
                    self.taskFailed.emit(
                        task.get('id', ''), str(e))

                self._current_task_id = None

            self.queueEmpty.emit()

    def _execute(self, task: dict) -> dict:
        action = task.get('action', '')

        if action == 'MOVE':
            src = Path(task['src_path'])
            root = Path(task['root'])
            target_cat = task['target_cat']
            if not src.is_file():
                return {'action': 'MOVE', 'error': 'source gone',
                        'song_id': task.get('song_id')}
            new_path = safe_move_song(
                type('S', (), {'path': src, 'id': task.get('song_id')})(),
                root, target_cat)
            return {'action': 'MOVE', 'song_id': task.get('song_id'),
                    'new_path': str(new_path), 'old_path': str(src),
                    'new_cat': target_cat, 'old_cat': task.get('old_cat', ''),
                    'title': task.get('title', '')}

        if action == 'RENAME':
            src = Path(task['src_path'])
            new_title = task['new_title']
            artist = task.get('artist', '')
            ok, new_path = write_display_tags(src, new_title, artist)
            if not ok:
                raise RuntimeError(f"Could not rename {src.name}")
            return {'action': 'RENAME', 'song_id': task.get('song_id'),
                    'new_path': str(new_path), 'old_path': str(src),
                    'new_title': new_title,
                    'old_title': task.get('old_title', '')}

        if action == 'RATING':
            path = Path(task['path'])
            rating = int(task['rating'])
            write_rating(path, rating)
            return {'action': 'RATING', 'song_id': task.get('song_id'),
                    'path': str(path), 'rating': rating}

        if action == 'DELETE':
            path = Path(task['path'])
            if path.is_file():
                os.remove(path)
            return {'action': 'DELETE', 'song_id': task.get('song_id'),
                    'path': str(path), 'title': task.get('title', '')}

        return {'action': action, 'error': 'unknown action'}


# ============================================================================
# Player
# ============================================================================
class RepeatMode(Enum):
    OFF     = 0
    ONE     = 1
    ALL     = 2
    REVERSE = 3


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
        if self._index < 0 and self._queue:
            self._index = 0
            self._load(autoplay=True)
            return
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
            song = self.current_song()
            if song and song.path.suffix.lower() not in TODO_EXTENSIONS:
                self._load(autoplay=True); return
            attempts += 1
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
        if song.path.suffix.lower() in TODO_EXTENSIONS:
            self.songChanged.emit(song)
            return
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
    toggledLock = Signal(bool)

    _ICON = "\u2712"

    _CSS_LOCKED = (
        "QToolButton{"
        "  background: transparent;"
        "  border: none;"
        "  color: #30D158;"
        "  font-size: 13px;"
        "  padding: 0px;"
        "}"
        "QToolButton:hover{ color: #4dde70; }"
    )

    _CSS_UNLOCKED = (
        "QToolButton{"
        "  background: transparent;"
        "  border: none;"
        "  color: #FF453A;"
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
    ratingChanged = Signal(int)
    COLOR_FILLED = "#FFD60A"
    COLOR_EMPTY  = "#8e8e93"

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
            new = 0
        self._rating = new
        self._refresh()
        self.ratingChanged.emit(self._rating)

    def _refresh(self):
        for i, btn in enumerate(self._btns):
            filled = i < self._rating
            color  = self.COLOR_FILLED if filled else self.COLOR_EMPTY
            char   = "\u2605" if filled else "\u2606"
            btn.setText(char)
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
# VU Meter widget
# ============================================================================
class VUMeter(QWidget):
    _DB_MIN = -60.0
    _DB_MAX  =   0.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(54, 60)
        self.setMaximumWidth(70)

        self._level_L: float = self._DB_MIN
        self._level_R: float = self._DB_MIN

        self._peak_L: float = self._DB_MIN
        self._peak_R: float = self._DB_MIN
        self._peak_L_age: int = 0
        self._peak_R_age: int = 0

        self._decay_rate  = 3.5
        self._peak_hold   = 20
        self._peak_decay  = 1.0

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._playing = False
        self._pos_ms  = 0
        self._dur_ms  = 1

        self._smooth_L = 0.0
        self._smooth_R = 0.0

    def set_playing(self, playing: bool):
        self._playing = playing
        if not playing:
            self._smooth_L = 0.0
            self._smooth_R = 0.0

    def set_position(self, pos_ms: int, dur_ms: int):
        self._pos_ms = pos_ms
        self._dur_ms = max(1, dur_ms)

    def _tick(self):
        if self._playing and self._dur_ms > 0:
            t = self._pos_ms / 1000.0
            import random
            rng = random.Random(int(t * 4))
            base = rng.uniform(0.12, 0.55)

            vary_L = 0.5 + 0.5 * math.sin(t * 7.3 + 0.0)
            vary_R = 0.5 + 0.5 * math.sin(t * 6.1 + 1.1)

            target_L = base * (0.7 + 0.3 * vary_L)
            target_R = base * (0.7 + 0.3 * vary_R)

            a_attack  = 0.5
            a_release = 0.15
            aL = a_attack if target_L > self._smooth_L else a_release
            aR = a_attack if target_R > self._smooth_R else a_release
            self._smooth_L += aL * (target_L - self._smooth_L)
            self._smooth_R += aR * (target_R - self._smooth_R)

            self._level_L = self._linear_to_db(self._smooth_L)
            self._level_R = self._linear_to_db(self._smooth_R)
        else:
            self._level_L = max(self._DB_MIN, self._level_L - self._decay_rate)
            self._level_R = max(self._DB_MIN, self._level_R - self._decay_rate)

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

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, QColor("#1c1c1e"))

        bar_w   = (w - 14) // 2
        gap     = 4
        label_h = 14
        bar_h   = h - label_h - 4

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
        p.fillRect(x, y, w, h, QColor("#2c2c2e"))

        frac  = self._db_to_frac(level_db)
        fill_h = max(0, int(frac * h))
        fill_y = y + h - fill_h

        if fill_h > 0:
            grad = QLinearGradient(x, y + h, x, y)
            grad.setColorAt(0.00, QColor("#30D158"))
            grad.setColorAt(0.65, QColor("#FFD60A"))
            grad.setColorAt(0.85, QColor("#FF9F0A"))
            grad.setColorAt(1.00, QColor("#FF453A"))
            from PySide6.QtGui import QBrush
            p.fillRect(x, fill_y, w, fill_h, QBrush(grad))

        pk_frac = self._db_to_frac(peak_db)
        pk_y    = y + h - max(1, int(pk_frac * h))
        pk_color = QColor("#FF453A") if peak_db > -6 else QColor("#FFD60A") if peak_db > -18 else QColor("#30D158")
        p.fillRect(x, pk_y, w, 2, pk_color)

        db_str = f"{level_db:.0f}"
        p.setPen(QColor("#8e8e93"))
        from PySide6.QtCore import QRect
        p.setFont(QFont("SF Mono, Consolas, monospace", 7))
        p.drawText(QRect(x, 0, w, 13), Qt.AlignCenter, db_str)

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

        self.title_lbl = QLabel("Loading music library\u2026")
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
        self.count_lbl.setText(f"{done} of {total} songs  \u2014  {pct}%")


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

QLabel#nowPlayingLabel {
    color: #f2f2f7;
    font-size: 13px;
    font-weight: 500;
}

QLabel#timeLabel {
    color: #8e8e93;
    font-size: 11px;
    font-family: "SF Mono", "Consolas", monospace;
}

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

QHeaderView::section:vertical {
    background: #1c1c1e; color: #48484a; font-size: 10px; font-weight: 400;
    font-family: "SF Mono","Consolas",monospace; border: none;
    border-right: 1px solid #2c2c2e; padding: 0px 2px; }

QTableCornerButton::section {
    background: #1c1c1e; border: none;
    border-bottom: 1px solid #2c2c2e; border-right: 1px solid #2c2c2e; }

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

QLineEdit#cellEditor {
    background: #1c1c1e;
    border: 1px solid #0a84ff;
    border-radius: 4px;
    color: #f2f2f7;
    font-size: 13px;
    padding: 2px 6px;
}

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


class _InvisibleSortItem(QTableWidgetItem):
    def __init__(self, sort_key=0):
        super().__init__("")
        self._sort_key = sort_key
    def __lt__(self, other):
        if isinstance(other, _InvisibleSortItem):
            return self._sort_key < other._sort_key
        return super().__lt__(other)
    def set_sort_key(self, key):
        self._sort_key = key


def _split_title(title: str) -> Tuple[str, str]:
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

ROW_HEIGHT = 38


# ============================================================================
# Shortcut help text (single source of truth — printed at startup and
# shown in the info bar)
# ============================================================================
SHORTCUTS_HELP = """
──────────────────────── KEYBOARD SHORTCUTS ─────────────────────
  Ctrl + Space                   →  Play / Pause
  MediaNext                      →  Next song
  MediaPrev                      →  Previous song
  MediaMute                      →  Mute / Unmute
  Ctrl+E + →                     →  Seek forward  (5s → 30s accel.)
  Ctrl+E + ←                     →  Seek backward (5s → 30s accel.)
  Ctrl+E + ↑                     →  Volume up     (+1 → +5 accel.)
  Ctrl+E + ↓                     →  Volume down   (−1 → −5 accel.)
  Ctrl+E + 0..5                  →  Rate current song 0–5 stars
  Ctrl+E + T                     →  Open "Add To-Do" dialog
─────────────────────────────────────────────────────────────────
All shortcuts work globally (in background / any app focused) when
'pynput' is installed:   pip install pynput
"""


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

        self._filter_cat    = "All"
        self._filter_rating = 0
        self._filter_search = ""
        self._filter_collab = False
        self._filter_cover  = False
        self._filter_accordion = False
        self._filter_christmas = False

        self.scan_worker: Optional[ScanWorker] = None
        self.pending_rescan = False
        self.last_added:   List[str] = []
        self.last_removed: List[str] = []
        self._pending_changes: List[tuple] = []
        self._suppress_rescan = False

        # ── Spinner state: song IDs currently having a queued/running task ──
        # Maps song_id → dict{'task_id', 'action'}. Row displays a spinner
        # icon until this entry is removed.
        self._pending_task_state: Dict[str, dict] = {}
        self._spinner_icon_cache: Optional["QIcon"] = None

        self.player = PlayerController(self)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(self._on_dir_changed)
        self.watcher.fileChanged.connect(self._on_file_changed)
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(1500)
        self._rescan_timer.timeout.connect(self._start_scan)

        self._build_ui()
        self._connect_player()
        self._setup_shortcuts()

        # ── Background task worker (non-blocking file I/O) ────────
        self._task_worker = TaskWorker(self)
        self._task_worker.taskDone.connect(self._on_task_done)
        self._task_worker.taskFailed.connect(self._on_task_failed)
        self._task_worker.queueEmpty.connect(self._on_queue_empty)
        self._task_worker.start()

        # ── UI-priority interrupts ───────────────────────────────
        # Any keystroke / mouse press pauses the worker briefly; after
        # 350 ms of UI idle, work resumes.
        self._ui_idle_timer = QTimer(self)
        self._ui_idle_timer.setSingleShot(True)
        self._ui_idle_timer.setInterval(350)
        self._ui_idle_timer.timeout.connect(self._ui_idle_resume)

        # ── Debounced cache save (never blocks the main thread) ──
        self._cache_save_timer = QTimer(self)
        self._cache_save_timer.setSingleShot(True)
        self._cache_save_timer.setInterval(1500)
        self._cache_save_timer.timeout.connect(self._flush_cache_now)

        # ── Load unfinished tasks from last session ───────────────
        self._load_pending_queue()
        self._restore_all_spinners_from_queue()

        # Restore last opened folder on startup
        last = self.settings.value("root_path", "")
        if last and Path(last).is_dir():
            QTimer.singleShot(0, lambda p=Path(last): self._set_root(p))

    def _setup_shortcuts(self):
        # ── Manual E-key tracking (in-app) ────────────────────────
        # Qt modifiers natively track Ctrl/Shift/Alt/Meta. We track the
        # E key ourselves so Ctrl+E+<action> works as a chorded prefix.
        self._e_held = False

        # Seek acceleration: repeated arrow presses skip further
        self._seek_accel_count = 0
        self._seek_accel_timer = QTimer(self)
        self._seek_accel_timer.setSingleShot(True)
        self._seek_accel_timer.setInterval(900)
        self._seek_accel_timer.timeout.connect(
            lambda: setattr(self, '_seek_accel_count', 0))

        # Volume acceleration: repeated up/down presses jump further
        self._vol_accel_count = 0
        self._vol_accel_timer = QTimer(self)
        self._vol_accel_timer.setSingleShot(True)
        self._vol_accel_timer.setInterval(600)
        self._vol_accel_timer.timeout.connect(
            lambda: setattr(self, '_vol_accel_count', 0))

        # Dedup: track last-fire timestamp per key so the same command
        # arriving twice (Qt eventFilter + pynput) is not executed twice.
        self._last_cmd_times: Dict[int, float] = {}

        # ── App-wide event filter (works when app HAS focus) ─────
        QApplication.instance().installEventFilter(self)

        # ── Global hotkey listener (works when app is in BG) ─────
        self._global_hotkeys = GlobalHotkeyListener(self)
        self._global_hotkeys.commandReceived.connect(
            self._on_global_hotkey)
        self._global_hotkeys.start()

        # Print shortcut help
        print(SHORTCUTS_HELP)

        if not self._global_hotkeys.is_available():
            print("INFO: pynput not installed — global (background) hotkeys disabled.\n"
                  "      Install with: pip install pynput\n"
                  "      (In-app shortcuts still work when window is focused.)")

    # ------------------------------------------------------------------
    # Spinner icon + per-song pending-task tracking
    # ------------------------------------------------------------------
    def _get_spinner_icon(self):
        """Cached QIcon (hourglass) shown on rows whose task is pending."""
        if self._spinner_icon_cache is not None:
            return self._spinner_icon_cache
        from PySide6.QtGui import QIcon, QPixmap
        pm = QPixmap(18, 18)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor("#FF9F0A"))
        p.setFont(QFont("", 12))
        p.drawText(pm.rect(), Qt.AlignCenter, "\u23f3")   # hourglass ⏳
        p.end()
        self._spinner_icon_cache = QIcon(pm)
        return self._spinner_icon_cache

    def _mark_task_pending(self, song_id: str, task_id: str, action: str):
        """Register a queued/running task for a song and show the spinner."""
        self._pending_task_state[song_id] = {
            'task_id': task_id, 'action': action}
        item = self.row_items.get(song_id)
        if item is not None:
            item.setIcon(self._get_spinner_icon())
            act_map = {'MOVE': 'Moving…', 'RENAME': 'Renaming…',
                       'RATING': 'Saving rating…', 'DELETE': 'Deleting…'}
            item.setToolTip(act_map.get(action, f"{action}…"))
        self._update_pending_btn()

    def _mark_task_done(self, song_id: str):
        """Remove pending marker for a song (task finished / failed)."""
        self._pending_task_state.pop(song_id, None)
        item = self.row_items.get(song_id)
        if item is not None:
            from PySide6.QtGui import QIcon
            item.setIcon(QIcon())    # clear icon
            item.setToolTip("")
        self._update_pending_btn()

    def _restore_all_spinners_from_queue(self):
        """After loading pending_queue.json on startup, mark spinners for
        every song whose task is still pending."""
        for task in self._task_worker.pending_tasks():
            sid = task.get('song_id')
            if sid:
                self._mark_task_pending(sid, task.get('id', ''), task.get('action', ''))

    # ------------------------------------------------------------------
    # UI-priority interrupts
    # ------------------------------------------------------------------
    def _bump_ui_activity(self):
        """Call whenever the user does anything (key press, mouse click,
        shortcut). Pauses the task worker so it doesn't compete for CPU
        or disk I/O with UI work. Auto-resumes after a short idle window."""
        if hasattr(self, '_task_worker'):
            if not self._task_worker.is_paused():
                self._task_worker.interrupt()
        # Restart idle timer — resume after 350 ms with no more activity
        if hasattr(self, '_ui_idle_timer'):
            self._ui_idle_timer.start()

    def _ui_idle_resume(self):
        if hasattr(self, '_task_worker'):
            self._task_worker.resume()

    # ------------------------------------------------------------------
    # Debounced cache saving (never blocks the main thread)
    # ------------------------------------------------------------------
    def _schedule_cache_save(self):
        """Batch cache-save requests; a background thread does the write."""
        if hasattr(self, '_cache_save_timer'):
            self._cache_save_timer.start()

    def _flush_cache_now(self, sync: bool = False):
        """Write cache to disk. `sync=True` blocks (used at app close)."""
        if not self.root_path:
            return
        snapshot = dict(self.cache)
        root = self.root_path
        if sync:
            save_cache(root, snapshot)
        else:
            threading.Thread(
                target=save_cache, args=(root, snapshot), daemon=True).start()

    def eventFilter(self, obj, event):
        """App-wide keyboard handler — works when window has focus."""
        from PySide6.QtCore import QEvent

        # ── UI activity → interrupt worker ──────────────────────
        if event.type() in (QEvent.KeyPress, QEvent.MouseButtonPress,
                             QEvent.MouseButtonDblClick, QEvent.Wheel):
            self._bump_ui_activity()

        if event.type() == QEvent.KeyPress:
            key = event.key()
            mods = event.modifiers()

            # ── Zero-modifier media keys ─────────────────────────
            if key in (Qt.Key_MediaNext, Qt.Key_MediaPrevious,
                       Qt.Key_VolumeMute):
                if self._handle_command_key(key):
                    return True

            # ── Ctrl + Space → Play / Pause ──────────────────────
            if key == Qt.Key_Space and (mods & Qt.ControlModifier):
                if self._handle_command_key(Qt.Key_Space):
                    return True

            # ── Track E press (only meaningful with Ctrl held) ───
            if key == Qt.Key_E and not event.isAutoRepeat():
                if mods & Qt.ControlModifier:
                    self._e_held = True
                    return True   # consume Ctrl+E itself

            # ── Ctrl + E + action ────────────────────────────────
            if (mods & Qt.ControlModifier) and self._e_held:
                if key not in (Qt.Key_E, Qt.Key_Control, Qt.Key_Shift):
                    if self._handle_command_key(key):
                        return True

        elif event.type() == QEvent.KeyRelease:
            key = event.key()
            if key == Qt.Key_E and not event.isAutoRepeat():
                self._e_held = False
                self._seek_accel_count = 0
                self._vol_accel_count  = 0
            elif key == Qt.Key_Control:
                # Releasing Ctrl always drops the E-modifier state
                self._e_held = False

        return super().eventFilter(obj, event)

    def changeEvent(self, event):
        """Reset modifier state when window loses focus."""
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.ActivationChange:
            if not self.isActiveWindow():
                self._e_held = False
                self._seek_accel_count = 0
                self._vol_accel_count = 0
        super().changeEvent(event)

    def _get_seek_step_ms(self) -> int:
        """Accelerating seek: 5s → 7s → 9s → 12s → … → 30s max."""
        self._seek_accel_count += 1
        self._seek_accel_timer.start()
        n = self._seek_accel_count
        if n <= 1:
            return 5000
        step = min(5000 + 2000 * (n - 1), 30000)
        return step

    def _handle_command_key(self, key, shift: bool = False) -> bool:
        """Dispatch a hotkey action.
        Returns True if handled."""

        # ── Deduplication ─────────────────────────────────────────
        # When the app window has focus, a key can fire twice — once
        # via Qt's event filter, once via the pynput global listener.
        # Ignore duplicates that arrive within 200 ms of each other.
        now = time.monotonic()
        last = self._last_cmd_times.get(key, 0.0)
        if now - last < 0.20:
            return True   # swallow the duplicate
        self._last_cmd_times[key] = now

        # ── Ctrl+E+T → Add To-Do ──────────────────────────────────
        if key == Qt.Key_T:
            self.raise_()
            self.activateWindow()
            self._add_todo()
            return True

        # ── Rating: 0–5 (Ctrl+E+digit) ────────────────────────────
        if Qt.Key_0 <= key <= Qt.Key_5:
            self._shortcut_rating(key - Qt.Key_0)
            return True

        # ── Seek forward (Ctrl+E+→, accelerating) ─────────────────
        if key == Qt.Key_Right:
            if self.player.current_song():
                step = self._get_seek_step_ms()
                self.player.seek(self.player._player.position() + step)
                self._show_toast(f"\u23e9  +{step // 1000}s", 800, "info")
            else:
                self._show_toast("No song playing.", 1000, "warning")
            return True

        # ── Seek backward (Ctrl+E+←, accelerating) ────────────────
        if key == Qt.Key_Left:
            if self.player.current_song():
                step = self._get_seek_step_ms()
                self.player.seek(max(0, self.player._player.position() - step))
                self._show_toast(f"\u23ea  -{step // 1000}s", 800, "info")
            else:
                self._show_toast("No song playing.", 1000, "warning")
            return True

        # ── Play / Pause (Ctrl+Space) ─────────────────────────────
        if key == Qt.Key_Space:
            self._shortcut_play_pause()
            return True

        # ── Next / Previous (MediaNext / MediaPrev, no modifier) ──
        if key == Qt.Key_MediaNext:
            if self.player.current_song() is None:
                songs = self._visible_songs_in_order()
                if songs:
                    self.player.play_song(songs[0], queue=songs)
            else:
                self.player.next()
            return True

        if key == Qt.Key_MediaPrevious:
            if self.player.current_song() is None:
                songs = self._visible_songs_in_order()
                if songs:
                    self.player.play_song(songs[-1], queue=songs)
            else:
                self.player.previous()
            return True

        # ── Volume Up / Down  (accelerating ±1 → ±5) ──────────────
        if key == Qt.Key_Up:
            step = self._get_volume_step()
            self.vol_slider.setValue(min(100, self.vol_slider.value() + step))
            return True

        if key == Qt.Key_Down:
            step = self._get_volume_step()
            self.vol_slider.setValue(max(0, self.vol_slider.value() - step))
            return True

        # ── Mute (media key only) ─────────────────────────────────
        if key == Qt.Key_VolumeMute:
            self.vol_slider.setValue(
                0 if self.vol_slider.value() > 0 else 80)
            return True

        return False

    def _get_volume_step(self) -> int:
        """Accelerating volume: 1, 1, 2, 3, 5, 5, 5 …"""
        self._vol_accel_count += 1
        self._vol_accel_timer.start()
        n = self._vol_accel_count
        if n <= 2:
            return 1
        if n == 3:
            return 2
        if n == 4:
            return 3
        return 5

    def _on_global_hotkey(self, qt_key: int):
        """Called from pynput background thread via signal — runs on
        main thread. Handles the command even when the app is in
        the background."""
        self._bump_ui_activity()
        self._handle_command_key(qt_key)

    # ------------------------------------------------------------------
    # Background task worker callbacks
    # ------------------------------------------------------------------
    def _on_task_done(self, task_id: str, result: dict):
        action = result.get('action', '')
        song_id = result.get('song_id', '')
        song = self.songs_by_id.get(song_id)

        # Clear the pending spinner regardless of action
        if song_id:
            self._mark_task_done(song_id)

        if action == 'MOVE' and song:
            new_path = Path(result['new_path'])
            old_path_str = result.get('old_path', '')
            new_cat = result.get('new_cat', '')
            song.path = new_path
            song.category = new_cat
            self.cache.pop(old_path_str, None)
            self._sync_cache(song)
            item = self.row_items.get(song.id)
            if item:
                combo = self.table.cellWidget(item.row(), COL_CATEGORY)
                if combo:
                    combo.blockSignals(True)
                    combo.setCurrentText(new_cat)
                    combo.blockSignals(False)
                cat_sort = self.table.item(item.row(), COL_CATEGORY)
                if cat_sort:
                    cat_sort.setText(new_cat)
                lock = combo.property("lock_ref") if combo else None
                if lock:
                    lock.setEnabled(True)
                    lock.set_locked(True)
                self.table.setRowHidden(item.row(), not self._matches(song))
            self._log_change("MOVE", result.get('title', song.title),
                             f"{result.get('old_cat','')} \u2192 {new_cat}")
            self._show_toast(
                f"\u2713  '{song.title}'  moved \u2192 {new_cat}", 3000, "success")

        elif action == 'RENAME' and song:
            new_path = Path(result['new_path'])
            old_path_str = result.get('old_path', '')
            new_title = result.get('new_title', '')
            self.cache.pop(old_path_str, None)
            song.path = new_path
            song.title = new_title
            self.player.notify_song_path_changed(song, new_path)
            self._sync_cache(song)
            item = self.row_items.get(song.id)
            if item:
                a, s = _split_title(new_title)
                item.setText(a)
                sn = self.table.item(item.row(), COL_SONGNAME)
                if sn:
                    sn.setText(s)
                lock_w = self.table.cellWidget(item.row(), COL_NAME_EDIT)
                if lock_w:
                    lk = lock_w.findChild(LockButton)
                    if lk:
                        lk.setEnabled(True)
                        lk.set_locked(True)
            self._log_change("RENAME", new_title,
                             f"was: {result.get('old_title','')}")
            self._show_toast(f'\u2713  Renamed: "{new_title}"', 3000, "success")

        elif action == 'RATING' and song:
            # Update cache with the final rating
            self._sync_cache(song)
            # Small toast to confirm persistence (short, not intrusive)
            # (Rating UI was already updated when user pressed the shortcut.)

        elif action == 'DELETE' and song:
            self.cache.pop(result.get('path', ''), None)
            item = self.row_items.pop(song.id, None)
            self.songs_by_id.pop(song.id, None)
            if item:
                self.table.removeRow(item.row())
            self._update_queue_numbers()
            self._rebuild_play_queue()
            self._log_change("DEL", result.get('title', ''), result.get('path', ''))
            self._show_toast(
                f"\u2713  Deleted: {result.get('title','')}", 3000, "success")

        self._suppress_rescan = True
        QTimer.singleShot(2500, lambda: setattr(self, '_suppress_rescan', False))
        self._rescan_timer.stop()
        self._update_pending_btn()
        self._save_pending_queue()

    def _on_task_failed(self, task_id: str, error: str):
        # Best-effort spinner cleanup: find song_id whose task matches
        for sid, state in list(self._pending_task_state.items()):
            if state.get('task_id') == task_id:
                self._mark_task_done(sid)
                break
        self._show_toast(f"Task failed: {error}", 5000, "error")
        self._update_pending_btn()
        self._save_pending_queue()

    def _on_queue_empty(self):
        self._update_pending_btn()
        self._save_pending_queue()
        if self.root_path:
            self._schedule_cache_save()

    # ------------------------------------------------------------------
    # Queue persistence
    # ------------------------------------------------------------------
    def _queue_file_path(self) -> Path:
        base = QStandardPaths.writableLocation(
            QStandardPaths.AppDataLocation)
        if not base:
            base = str(Path.home() / ".music_library_app")
        bp = Path(base)
        bp.mkdir(parents=True, exist_ok=True)
        return bp / "pending_queue.json"

    def _save_pending_queue(self):
        try:
            data = self._task_worker.get_serializable_queue()
            with open(self._queue_file_path(), 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load_pending_queue(self):
        qf = self._queue_file_path()
        if not qf.is_file():
            return
        try:
            with open(qf, 'r', encoding='utf-8') as f:
                tasks = json.load(f)
            if tasks:
                for t in tasks:
                    self._task_worker.add_task(t)
                self._show_toast(
                    f"\u23f3  Resuming {len(tasks)} pending task(s) from last session",
                    4000, "warning")
            qf.unlink(missing_ok=True)
        except Exception:
            pass

    def closeEvent(self, event):
        # Save the queue BEFORE stopping the worker so any in-progress
        # task is atomically persisted for next launch.
        self._save_pending_queue()
        self._save_session_state()
        # Allow the CURRENT running task to complete before we exit.
        # TaskWorker.stop() calls wait() which blocks until run() returns.
        # We deliberately don't interrupt mid-write to protect files.
        if hasattr(self, '_task_worker'):
            self._task_worker.stop()
        if hasattr(self, '_global_hotkeys'):
            self._global_hotkeys.stop()
        # Final SYNCHRONOUS cache flush so we exit clean
        self._flush_cache_now(sync=True)
        # Re-save queue in case tasks completed during stop()
        self._save_pending_queue()
        event.accept()

    # ------------------------------------------------------------------
    # Session state persistence (filters + last-played song)
    # ------------------------------------------------------------------
    def _save_session_state(self):
        """Save current filter state and last-played song path via QSettings."""
        try:
            self.settings.setValue("filter/search",     self._filter_search)
            self.settings.setValue("filter/rating",     self._filter_rating)
            self.settings.setValue("filter/collab",     self._filter_collab)
            self.settings.setValue("filter/cover",      self._filter_cover)
            self.settings.setValue("filter/accordion",  self._filter_accordion)
            self.settings.setValue("filter/christmas",  self._filter_christmas)
            self.settings.setValue("filter/cat_checks", json.dumps(self._cat_checks))
            # Sort column & order
            self.settings.setValue("sort/col",   self._last_sort_col)
            self.settings.setValue("sort/order", int(self._last_sort_order))
            # Last played song — prefer highlighted (last selection), else player
            last = self.player.current_song()
            if last is None and self._highlighted_id:
                last = self.songs_by_id.get(self._highlighted_id)
            if last:
                self.settings.setValue("session/last_song_path", str(last.path))
            else:
                self.settings.remove("session/last_song_path")
            # Volume
            self.settings.setValue("session/volume", self.vol_slider.value())
        except Exception as e:
            print(f"WARN: could not save session state: {e}")

    def _restore_session_state(self):
        """Restore filters + last-played song. Called ONCE after first scan
        completes, so that self.songs_by_id is populated."""
        if getattr(self, '_session_restored', False):
            return
        self._session_restored = True

        try:
            # ── Filters ─────────────────────────────────────────
            search = self.settings.value("filter/search", "", type=str)
            if search:
                self.search_box.blockSignals(True)
                self.search_box.setText(search)
                self.search_box.blockSignals(False)
                self._filter_search = search.lower()

            rating = self.settings.value("filter/rating", 0, type=int)
            if rating != 0:
                self._filter_rating = rating
                for i in range(self.rat_filter.count()):
                    if self.rat_filter.itemData(i) == rating:
                        self.rat_filter.blockSignals(True)
                        self.rat_filter.setCurrentIndex(i)
                        self.rat_filter.blockSignals(False)
                        break

            for attr, btn in [
                ("collab",    self.collab_btn),
                ("cover",     self.cover_btn),
                ("accordion", self.accordion_btn),
                ("christmas", self.christmas_btn),
            ]:
                val = self.settings.value(f"filter/{attr}", False, type=bool)
                if val:
                    setattr(self, f"_filter_{attr}", True)
                    btn.blockSignals(True)
                    btn.setChecked(True)
                    btn.blockSignals(False)

            cat_json = self.settings.value("filter/cat_checks", "", type=str)
            if cat_json:
                try:
                    saved_checks = json.loads(cat_json)
                    for cat in self._cat_checks:
                        if cat in saved_checks:
                            self._cat_checks[cat] = bool(saved_checks[cat])
                    self._update_cat_btn_label()
                except Exception:
                    pass

            # ── Sort ────────────────────────────────────────────
            sort_col   = self.settings.value("sort/col",   COL_ARTIST, type=int)
            sort_order = self.settings.value("sort/order", int(Qt.AscendingOrder), type=int)
            if sort_col in (COL_ARTIST, COL_SONGNAME, COL_CATEGORY, COL_RATING):
                self.table.sortItems(sort_col, Qt.SortOrder(sort_order))

            # ── Volume ──────────────────────────────────────────
            vol = self.settings.value("session/volume", -1, type=int)
            if 0 <= vol <= 100:
                self.vol_slider.setValue(vol)

            # ── Apply filters, then locate last song ────────────
            self._apply_filters()

            last_path = self.settings.value("session/last_song_path", "", type=str)
            if last_path:
                target = None
                for sid, s in self.songs_by_id.items():
                    if str(s.path) == last_path:
                        target = s
                        break
                if target:
                    self._restore_last_song(target)
                else:
                    self._show_toast(
                        "Last-played song was not found (moved or deleted).",
                        3500, "warning")
        except Exception as e:
            print(f"WARN: could not restore session state: {e}")

    def _restore_last_song(self, song: Song):
        """Load the last-played song into the player without autoplay,
        highlight it in blue, and scroll to it."""
        # Load into the player queue so Ctrl+Space starts it
        visible = self._visible_songs_in_order()
        if song in visible:
            queue = visible
        else:
            # Song may be filtered out — put it as a single-item queue
            queue = [song]
        self.player._queue = list(queue)
        self.player._index = queue.index(song) if song in queue else 0
        # Set the media source but DO NOT play
        try:
            self.player._player.setSource(QUrl.fromLocalFile(str(song.path)))
        except Exception:
            pass
        # Update the "Now Playing" label
        self.player.songChanged.emit(song)
        # Ensure the row is highlighted in blue
        self._highlight(song)
        # Scroll into view
        item = self.row_items.get(song.id)
        if item is not None:
            row = item.row()
            if not self.table.isRowHidden(row):
                idx = self.table.model().index(row, COL_ARTIST)
                # Delay scroll one tick so the table has laid out
                QTimer.singleShot(50, lambda: self.table.scrollTo(
                    idx, QAbstractItemView.PositionAtCenter))
        self._show_toast(
            f"\u23ee  Resumed at: {song.title}   (Ctrl+Space to play)",
            4500, "info")

    def _shortcut_play_pause(self):
        if self.player.current_song() is None:
            songs = self._visible_songs_in_order()
            if songs:
                self.player.play_song(songs[0], queue=songs)
        else:
            self.player.toggle_play_pause()

    def _shortcut_rating(self, rating: int):
        song = self.player.current_song()
        if song is None:
            self._show_toast("No song playing.", 1500, "warning")
            return
        song.rating = rating
        # Update UI immediately (visual feedback is instant)
        item = self.row_items.get(song.id)
        if item:
            rat_sort = self.table.item(item.row(), COL_RATING)
            if isinstance(rat_sort, _InvisibleSortItem):
                rat_sort.set_sort_key(rating)
            star_wrap = self.table.cellWidget(item.row(), COL_RATING)
            if star_wrap:
                star = star_wrap.findChild(StarRatingWidget)
                if star: star.set_rating(rating)
        # Queue the disk write via TaskWorker (survives app close)
        self._queue_rating_task(song, rating)
        stars = "\u2605" * rating + "\u2606" * (5 - rating)
        self._log_change("RATING", song.title, f"{rating}/5")
        self._show_toast(f"\u2713  {stars} ({rating}/5) \u2014 {song.title}", 1500, "success")

    def _queue_rating_task(self, song: "Song", rating: int):
        """Central place to submit a rating-write task."""
        self._suppress_rescan = True
        QTimer.singleShot(2500, lambda: setattr(self, '_suppress_rescan', False))
        task_id = f"rating_{song.id}_{uuid.uuid4().hex[:8]}"
        self._task_worker.add_task({
            'id': task_id, 'action': 'RATING',
            'song_id': song.id,
            'path': str(song.path),
            'rating': int(rating),
            'title': song.title,
        })
        self._mark_task_pending(song.id, task_id, 'RATING')
        self._save_pending_queue()

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

        add_todo_btn = QToolButton()
        add_todo_btn.setText("+ To-Do")
        add_todo_btn.setToolTip(
            "Create a .txt placeholder for a song to download later\n"
            "Shortcut: Ctrl+E + T  (works globally when pynput is installed)")
        add_todo_btn.setFixedHeight(24)
        add_todo_btn.setCursor(Qt.PointingHandCursor)
        add_todo_btn.setStyleSheet(
            "QToolButton{background:rgba(10,132,255,0.12);border:1px solid rgba(10,132,255,0.3);"
            "border-radius:6px;color:#0a84ff;font-size:11px;font-weight:600;padding:0 10px;}"
            "QToolButton:hover{background:rgba(10,132,255,0.22);border-color:#0a84ff;}"
        )
        add_todo_btn.clicked.connect(self._add_todo)
        lay.addWidget(add_todo_btn)

        # Shortcut help button
        help_btn = QToolButton()
        help_btn.setText("? Shortcuts")
        help_btn.setToolTip("Show all keyboard shortcuts")
        help_btn.setFixedHeight(24)
        help_btn.setCursor(Qt.PointingHandCursor)
        help_btn.setStyleSheet(
            "QToolButton{background:#2c2c2e;border:1px solid #3a3a3c;"
            "border-radius:6px;color:#8e8e93;font-size:11px;font-weight:600;padding:0 10px;}"
            "QToolButton:hover{background:#3a3a3c;color:#f2f2f7;}"
        )
        help_btn.clicked.connect(self._show_shortcuts_dialog)
        lay.addWidget(help_btn)

        lay.addStretch()

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

    def _show_shortcuts_dialog(self):
        from PySide6.QtWidgets import QDialog, QTextEdit
        dlg = QDialog(self)
        dlg.setWindowTitle("Keyboard Shortcuts")
        dlg.setFixedSize(620, 500)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(16, 16, 16, 16)

        html = """
        <div style='font-family:SF Mono,Consolas,monospace;font-size:13px;color:#f2f2f7;'>
        <h2 style='color:#0a84ff;margin-top:0;'>Keyboard Shortcuts</h2>
        <p style='color:#8e8e93;font-size:12px;'>All shortcuts work globally
        (in background — any app can be focused) when <code>pynput</code> is
        installed.</p>
        <hr style='border-color:#3a3a3c;'>
        <table style='border-collapse:collapse;width:100%;'>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>Ctrl + Space</td>
            <td style='padding:6px 12px;'>Play / Pause</td></tr>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>MediaNext</td>
            <td style='padding:6px 12px;'>Next song</td></tr>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>MediaPrev</td>
            <td style='padding:6px 12px;'>Previous song</td></tr>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>MediaMute</td>
            <td style='padding:6px 12px;'>Mute / Unmute</td></tr>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>Ctrl+E + →</td>
            <td style='padding:6px 12px;'>Seek forward (5s → 30s accelerating)</td></tr>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>Ctrl+E + ←</td>
            <td style='padding:6px 12px;'>Seek backward (5s → 30s accelerating)</td></tr>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>Ctrl+E + ↑</td>
            <td style='padding:6px 12px;'>Volume up  (+1 → +5 accelerating)</td></tr>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>Ctrl+E + ↓</td>
            <td style='padding:6px 12px;'>Volume down (−1 → −5 accelerating)</td></tr>
        <tr><td style='padding:6px 12px;color:#0a84ff;font-weight:600;'>Ctrl+E + 0..5</td>
            <td style='padding:6px 12px;'>Rate current song 0–5 stars ★</td></tr>
        <tr><td style='padding:6px 12px;color:#FFD60A;font-weight:600;'>Ctrl+E + T</td>
            <td style='padding:6px 12px;'><b>Open "Add To-Do" dialog</b></td></tr>
        </table>
        <hr style='border-color:#3a3a3c;'>
        <p style='color:#8e8e93;font-size:11px;'>
        <b>Background support:</b> requires <code>pip install pynput</code>.<br>
        Without pynput, shortcuts still work when the app window has focus.
        </p>
        </div>
        """
        tv = QTextEdit()
        tv.setReadOnly(True)
        tv.setHtml(html)
        tv.setStyleSheet(
            "QTextEdit{background:#1c1c1e;border:1px solid #2c2c2e;"
            "border-radius:6px;padding:8px;}")
        lay.addWidget(tv)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        lay.addWidget(close_btn, alignment=Qt.AlignRight)
        dlg.exec()

    def _build_top_bar(self) -> QWidget:
        wrapper = QWidget(); wrapper.setObjectName("topBar")
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

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

        refresh_btn = QPushButton("\u21ba  Refresh")
        refresh_btn.clicked.connect(self._start_scan)
        lay.addWidget(refresh_btn)

        outer.addWidget(bar)

        self.info_bar = QFrame()
        self.info_bar.setObjectName("infoBar")
        self.info_bar.setFixedHeight(32)
        info_lay = QHBoxLayout(self.info_bar)
        info_lay.setContentsMargins(16, 0, 16, 0)
        info_lay.setSpacing(10)

        self.status_dot = QLabel("\u25cf")
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

        self.pending_btn = QPushButton("Queue (0)")
        self.pending_btn.setToolTip("Show pending changes waiting to be applied")
        self.pending_btn.clicked.connect(self._show_pending)
        info_lay.addWidget(self.pending_btn)

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
        if "Loading" in txt or "Importing" in txt or "Checking" in txt:
            self._set_info(txt, "loading", auto_reset=False)
        else:
            self._set_info(txt, "idle", auto_reset=False)

    def _build_filter_bar(self) -> QWidget:
        wrapper = QWidget()
        outer = QVBoxLayout(wrapper)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bar = QFrame(); bar.setObjectName("filterBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 8, 12, 8); lay.setSpacing(8)

        search_icon = QLabel("\U0001f50d"); search_icon.setObjectName("subtleLabel")
        lay.addWidget(search_icon)
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search by title\u2026")
        self.search_box.setFixedWidth(230)
        self.search_box.textChanged.connect(self._on_search_changed)
        lay.addWidget(self.search_box)

        lay.addSpacing(16)

        cat_lbl = QLabel("Category:"); cat_lbl.setObjectName("subtleLabel")
        lay.addWidget(cat_lbl)
        self.cat_filter_btn = QPushButton("All")
        self.cat_filter_btn.setFixedWidth(155)
        self.cat_filter_btn.setToolTip("Click to select/deselect categories")
        self.cat_filter_btn.clicked.connect(self._show_cat_checkboxes)
        lay.addWidget(self.cat_filter_btn)
        self._cat_checks: Dict[str, bool] = {}

        lay.addSpacing(16)

        rat_lbl = QLabel("Min. Rating:"); rat_lbl.setObjectName("subtleLabel")
        lay.addWidget(rat_lbl)
        self.rat_filter = QComboBox()
        self.rat_filter.setFixedWidth(168)
        self.rat_filter.addItem("All ratings", 0)
        self.rat_filter.addItem("\u2606\u2606\u2606\u2606\u2606  No rating", -1)
        star_labels = {
            1: "\u2605\u2606\u2606\u2606\u2606  (1 star)",
            2: "\u2605\u2605\u2606\u2606\u2606  (2 stars)",
            3: "\u2605\u2605\u2605\u2606\u2606  (3 stars)",
            4: "\u2605\u2605\u2605\u2605\u2606  (4 stars)",
            5: "\u2605\u2605\u2605\u2605\u2605  (5 stars)",
        }
        for n, lbl in star_labels.items():
            self.rat_filter.addItem(lbl, n)
            self.rat_filter.setItemData(
                self.rat_filter.count() - 1, QColor("#FFD60A"), Qt.ForegroundRole)
        self.rat_filter.currentIndexChanged.connect(self._on_rat_filter_changed)
        lay.addWidget(self.rat_filter)

        lay.addSpacing(16)

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

        self.accordion_btn = _toggle_btn(
            "[A]", "Show only Accordion songs (title contains '[A]')")
        self.accordion_btn.toggled.connect(self._on_accordion_toggled)
        lay.addWidget(self.accordion_btn)

        self.christmas_btn = _toggle_btn(
            "[C]", "Show only Christmas songs (title contains '[C]')")
        self.christmas_btn.toggled.connect(self._on_christmas_toggled)
        lay.addWidget(self.christmas_btn)

        lay.addSpacing(16)

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

        self.bulk_del_btn = QPushButton("\U0001f5d1  Delete")
        self.bulk_del_btn.setToolTip("Delete ALL selected songs permanently")
        self.bulk_del_btn.setEnabled(False)
        self.bulk_del_btn.setStyleSheet(
            "QPushButton:enabled{color:#FF453A;font-weight:600;}")
        self.bulk_del_btn.clicked.connect(self._on_bulk_delete)
        bulk_lay.addWidget(self.bulk_del_btn)

        self.bulk_status = QLabel("Select 2+ songs to enable")
        self.bulk_status.setObjectName("subtleLabel")
        bulk_lay.addWidget(self.bulk_status)

        bulk_lay.addStretch()

        outer.addWidget(bulk_bar)
        outer.addWidget(bar)
        return wrapper

    def _build_table(self) -> QWidget:
        self.table = QTableWidget(0, COL_COUNT)
        self.table.setHorizontalHeaderLabels(
            ["Artist", "Song Name", "Edit", "Category", "Edit", "Rating", "Edit", ""])
        self.table.verticalHeader().setVisible(True)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.verticalHeader().setFixedWidth(42)
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.table.verticalHeader().setSectionsClickable(False)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.sortItems(COL_ARTIST, Qt.AscendingOrder)
        h = self.table.horizontalHeader()
        h.setSortIndicatorShown(False)
        h.sortIndicatorChanged.connect(self._on_sort_changed)
        self._base_headers = ["Artist", "Song Name", "Edit", "Category", "Edit", "Rating", "Edit", ""]
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

    def _build_player_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("playerBar")
        bar.setFixedHeight(110)
        outer = QHBoxLayout(bar)
        outer.setContentsMargins(16, 6, 16, 8)
        outer.setSpacing(12)

        self.vu_meter = VUMeter()
        outer.addWidget(self.vu_meter)

        main_col = QVBoxLayout()
        main_col.setSpacing(4)

        seek_row = QHBoxLayout()
        seek_row.setSpacing(8)

        self.lbl_pos = QLabel("0:00")
        self.lbl_pos.setObjectName("timeLabel")
        self.lbl_pos.setFixedWidth(36)
        self.lbl_pos.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setObjectName("seekSlider")
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

        ctrl = QHBoxLayout()
        ctrl.setSpacing(0)

        self.lbl_now = QLabel("Nothing is playing")
        self.lbl_now.setObjectName("nowPlayingLabel")
        self.lbl_now.setMinimumWidth(180)
        self.lbl_now.setMaximumWidth(300)
        ctrl.addWidget(self.lbl_now, stretch=1)

        ctrl.addStretch(1)

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

        self.rep_btn = _circle_btn("\u21bb", 36, "Repeat: Off",
                                   bg="#2c2c2e", fg="#8e8e93", font_size=16)
        self.rep_btn.clicked.connect(self._cycle_repeat)
        ctrl.addWidget(self.rep_btn)

        ctrl.addSpacing(8)

        prev_btn = _circle_btn("\u23ee", 40, "Previous  (MediaPrev)", font_size=17)
        prev_btn.clicked.connect(self.player.previous)
        ctrl.addWidget(prev_btn)

        ctrl.addSpacing(6)

        self.pp_btn = QToolButton()
        self.pp_btn.setText("\u25b6")
        self.pp_btn.setFixedSize(50, 50)
        self.pp_btn.setToolTip("Play / Pause  (Ctrl + Space)")
        self.pp_btn.setCursor(Qt.PointingHandCursor)
        self.pp_btn.setStyleSheet(
            "QToolButton{background:#ffffff;border:none;border-radius:25px;"
            "color:#1c1c1e;font-size:19px;font-weight:800;}"
            "QToolButton:hover{background:#e5e5ea;}"
            "QToolButton:pressed{background:#c7c7cc;}"
        )
        self.pp_btn.clicked.connect(self._shortcut_play_pause)
        ctrl.addWidget(self.pp_btn)

        ctrl.addSpacing(6)

        next_btn = _circle_btn("\u23ed", 40, "Next  (MediaNext)", font_size=17)
        next_btn.clicked.connect(self.player.next)
        ctrl.addWidget(next_btn)

        ctrl.addSpacing(8)

        self.shuf_btn = _circle_btn("\u21c4", 36, "Shuffle: Off",
                                    bg="#2c2c2e", fg="#8e8e93", font_size=16)
        self.shuf_btn.clicked.connect(self._toggle_shuffle)
        ctrl.addWidget(self.shuf_btn)

        ctrl.addSpacing(12)

        ctrl.addStretch(1)

        vol_container = QFrame()
        vol_container.setFixedWidth(130)
        vol_layout = QHBoxLayout(vol_container)
        vol_layout.setContentsMargins(0, 0, 0, 0)
        vol_layout.setSpacing(4)

        self.vol_icon = QLabel("\U0001f50a")
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

    _seek_dragging: bool = False

    def _seek_mouse_press(self, event):
        if event.button() == Qt.LeftButton and self.seek_slider.maximum() > 0:
            slider = self.seek_slider
            ratio  = event.position().x() / slider.width()
            value  = int(ratio * slider.maximum())
            value  = max(slider.minimum(), min(slider.maximum(), value))
            slider.setValue(value)
            self.player.seek(value)
        QSlider.mousePressEvent(self.seek_slider, event)

    def _on_seek_pressed(self):
        self._seek_dragging = True

    def _on_seek_released(self):
        self._seek_dragging = False
        self.player.seek(self.seek_slider.value())

    def _on_volume_changed(self, value: int):
        self.player.set_volume(value)
        if value == 0:
            self.vol_icon.setText("\U0001f507")
        elif value < 40:
            self.vol_icon.setText("\U0001f509")
        else:
            self.vol_icon.setText("\U0001f50a")

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
        self.pp_btn.setText("\u23f8" if playing else "\u25b6")

    def _on_song_changed(self, song: Optional[Song]):
        if song is None:
            self.lbl_now.setText("Nothing is playing")
        else:
            artist_part, song_part = _split_title(song.title)
            ap = f"{artist_part}  \u00b7  " if artist_part else ""
            self.lbl_now.setText(f"{ap}{song_part}  \u00b7  {song.category}")
        self._highlight(song)
        self._process_pending_changes()

    def _on_pos(self, ms: int):
        if not getattr(self, '_seek_dragging', False):
            self.seek_slider.setValue(ms)
        self.lbl_pos.setText(_fmt_time(ms))
        dur = self.seek_slider.maximum()
        self.vu_meter.set_position(ms, max(1, dur))

    def _process_pending_changes(self):
        if not self._pending_changes:
            return
        current = self.player.current_song()
        current_id = current.id if current else None
        remaining = []
        for action, song, detail in self._pending_changes:
            if current_id == song.id and self.player.is_playing():
                remaining.append((action, song, detail))
                continue
            if action == "MOVE":
                self._suppress_rescan = True; self.watcher.blockSignals(True)
                old_cat, old_path_str = song.category, str(song.path)
                try:
                    new_path = safe_move_song(song, self.root_path, detail)
                    song.path = new_path; song.category = detail
                    self.cache.pop(old_path_str, None)
                    self._sync_cache(song)
                    item = self.row_items.get(song.id)
                    if item:
                        combo = self.table.cellWidget(item.row(), COL_CATEGORY)
                        if combo:
                            combo.blockSignals(True)
                            combo.setCurrentText(detail)
                            combo.blockSignals(False)
                        lock = combo.property("lock_ref") if combo else None
                        if lock:
                            lock.setEnabled(True)
                            lock.set_locked(True)
                        cat_sort = self.table.item(item.row(), COL_CATEGORY)
                        if cat_sort: cat_sort.setText(detail)
                        self.table.setRowHidden(item.row(), not self._matches(song))
                    self._show_toast(
                        f"\u2713  Queued move completed: '{song.title}' \u2192 {detail}",
                        3000, "success")
                    self._log_change("MOVE", song.title, f"{old_cat} -> {detail}")
                except SafeMoveError as e:
                    self._show_toast(f"Queued move failed: {e}", 5000, "error")
                self._suppress_rescan = False; self.watcher.blockSignals(False)
                self._rescan_timer.stop()

            elif action == "RENAME":
                self._suppress_rescan = True; self.watcher.blockSignals(True)
                old_title = song.title
                old_path_str = str(song.path)
                ok, new_path = write_display_tags(song.path, detail, song.artist)
                if ok:
                    self.cache.pop(old_path_str, None)
                    song.path = new_path; song.title = detail
                    self._sync_cache(song)
                    item = self.row_items.get(song.id)
                    if item:
                        a, s = _split_title(detail)
                        item.setText(a)
                        sn = self.table.item(item.row(), COL_SONGNAME)
                        if sn: sn.setText(s)
                        lock_w = self.table.cellWidget(item.row(), COL_NAME_EDIT)
                        if lock_w:
                            lk = lock_w.findChild(LockButton)
                            if lk:
                                lk.setEnabled(True)
                                lk.set_locked(True)
                    self._show_toast(f"\u2713  Queued rename: \"{detail}\"", 3000, "success")
                    self._log_change("RENAME", detail, f"was: {old_title}")
                self._suppress_rescan = False; self.watcher.blockSignals(False)
                self._rescan_timer.stop()

        self._pending_changes = remaining
        self._update_pending_btn()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    _saved_scroll_pos: int = -1

    def _scroll_to_playing(self):
        current = self.player.current_song()
        if current is None:
            self._show_toast("No song is currently playing.", 2000, "warning")
            return
        item = self.row_items.get(current.id)
        if item is None:
            return

        if self._saved_scroll_pos >= 0:
            self.table.verticalScrollBar().setValue(self._saved_scroll_pos)
            self._saved_scroll_pos = -1
            self.focus_btn.setStyleSheet(
                "QToolButton{background:#2c2c2e;border:none;border-radius:6px;"
                "color:#8e8e93;font-size:12px;font-weight:600;}"
                "QToolButton:hover{background:#3a3a3c;color:#f2f2f7;}"
            )
        else:
            self._saved_scroll_pos = self.table.verticalScrollBar().value()
            row = item.row()
            idx = self.table.model().index(row, COL_ARTIST)
            self.table.scrollTo(idx, QAbstractItemView.PositionAtCenter)
            self.focus_btn.setStyleSheet(
                "QToolButton{background:#1a3a5c;border:none;border-radius:6px;"
                "color:#0a84ff;font-size:12px;font-weight:700;}"
                "QToolButton:hover{background:#234a70;}"
            )

    def _scroll_to_top(self):
        self.table.scrollToTop()

    def _scroll_to_bottom(self):
        self.table.scrollToBottom()

    # ------------------------------------------------------------------
    # To-Do & Delete
    # ------------------------------------------------------------------
    def _add_todo(self):
        if not self.root_path:
            self._show_toast("Open a music folder first.", 3000, "warning")
            return

        self._suppress_rescan = True; self.watcher.blockSignals(True)

        todo_dir = self.root_path / "To-Do"
        if not todo_dir.exists():
            try:
                todo_dir.mkdir(parents=True, exist_ok=True)
                self.watcher.addPath(str(todo_dir))
                self._rebuild_cat_filter()
            except OSError as e:
                self._suppress_rescan = False; self.watcher.blockSignals(False)
                self._show_toast(f"Could not create To-Do folder: {e}", 5000, "error")
                return

        existing_titles = [s.title for s in self.songs_by_id.values()]

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
        completer = QCompleter(existing_titles, dlg)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setMaxVisibleItems(5)
        name_input.setCompleter(completer)
        dlg_lay.addWidget(name_input)

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
            self._suppress_rescan = False; self.watcher.blockSignals(False)
            return

        name = name_input.text().strip()
        if not name:
            self._suppress_rescan = False; self.watcher.blockSignals(False)
            return
        rat_val = 0

        txt_path = todo_dir / f"{name}.txt"
        counter = 1
        while txt_path.exists():
            txt_path = todo_dir / f"{name} ({counter}).txt"
            counter += 1
        txt_path.write_text(f"To-Do: {name}\n", encoding="utf-8")

        song = Song(
            path=txt_path, category="To-Do",
            title=txt_path.stem, artist="",
            rating=rat_val, duration=0.0)
        self._add_or_update(song)

        st = txt_path.stat()
        self.cache[str(txt_path)] = {
            "size": st.st_size, "mtime": st.st_mtime,
            "title": song.title, "artist": "", "rating": rat_val,
            "duration": 0.0, "id": song.id}
        if self.root_path:
            self._schedule_cache_save()

        self._rebuild_cat_filter()
        self._apply_filters()

        self._suppress_rescan = False; self.watcher.blockSignals(False)
        self._rescan_timer.stop()
        self._show_toast(f"\u2713  To-Do added: \"{name}\"", 3000, "success")
        self._log_change("TODO", name, "To-Do placeholder created")

    def _delete_song(self, song: Song):
        reply = QMessageBox.question(
            self, "Delete file",
            f"Permanently delete this file?\n\n{song.path.name}\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return

        if self.player.current_song() is song:
            self.player._player.stop()

        # Queue delete — TaskWorker performs os.remove and removes row on done
        task_id = f"delete_{song.id}_{uuid.uuid4().hex[:8]}"
        self._task_worker.add_task({
            'id': task_id, 'action': 'DELETE',
            'song_id': song.id,
            'path': str(song.path),
            'title': song.title,
        })
        self._mark_task_pending(song.id, task_id, 'DELETE')
        self._save_pending_queue()
        self._show_toast(f"\u23f3  Queued delete: {song.title}", 2000, "info")

    def _cycle_repeat(self):
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.REVERSE, RepeatMode.ONE]
        new   = order[(order.index(self.player.repeat_mode()) + 1) % 4]
        self.player.set_repeat_mode(new)

        cfg = {
            RepeatMode.OFF:     ("\u21bb", "#8e8e93", "#2c2c2e", "Repeat: Off"),
            RepeatMode.ALL:     ("\u21bb", "#0a84ff", "#1a3a5c", "Repeat: All  \u2014  restarts from beginning"),
            RepeatMode.REVERSE: ("\u21ba", "#0a84ff", "#1a3a5c", "Repeat: Reverse  \u2014  plays backwards through list"),
            RepeatMode.ONE:     ("\u2460", "#30D158", "#1a3a2c", "Repeat: One  \u2014  repeats this song only"),
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
        self.path_btn.setText(f"\U0001f4c1  {path.name}")
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
        if not self._suppress_rescan:
            self._rescan_timer.start()

    def _on_file_changed(self, changed_path: str):
        if Path(changed_path).exists():
            self.watcher.addPath(changed_path)
        if not self._suppress_rescan:
            self._rescan_timer.start()

    # ------------------------------------------------------------------
    # Background scanning
    # ------------------------------------------------------------------
    def _start_scan(self):
        if self.root_path is None: return
        if self.scan_worker and self.scan_worker.isRunning():
            self.pending_rescan = True; return
        self._set_status("Checking library\u2026")
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

    def _on_song_ready(self, song: Song):
        self._add_or_update(song)
        item = self.row_items.get(song.id)
        if item is not None:
            self.table.setRowHidden(item.row(), not self._matches(song))
        self.watcher.addPath(str(song.path))
        self._song_ready_count = getattr(self, '_song_ready_count', 0) + 1
        if self._song_ready_count % 50 == 0:
            QApplication.processEvents()

    def _on_total(self, total: int):
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        verb = "Loading" if not self.songs_by_id else "Updating"
        self._set_info(f"{verb}  0 / {total} songs\u2026", "loading", auto_reset=False)

    def _on_progress(self, done: int, total: int, eta: Optional[float], name: str):
        self.progress_bar.setValue(done)
        pct  = int(done / total * 100) if total else 100
        verb = "Loading" if len(self.songs_by_id) < 2 else "Updating"
        self._set_info(
            f"{verb}  {done} / {total} songs  ({pct}%)",
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
        if self.root_path: self._schedule_cache_save()
        self._rebuild_cat_filter()
        n = len(self.songs_by_id)
        self.song_count_label.setText(f"{n} songs")
        self._set_info(f"{n} songs  \u2022  Up to date", "success",
                       auto_reset=True, duration_ms=3000)

        self._apply_filters()
        self._update_queue_numbers()
        self._rebuild_play_queue()

        # ── Restore filters + last-played song (first scan only) ────
        self._restore_session_state()

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
        from PySide6.QtWidgets import QDialog, QTextEdit

        log_path = self._changelog_path()
        lines = []
        if log_path.is_file():
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    lines = [r.strip() for r in f.readlines() if r.strip()]
            except Exception:
                pass

        icons = {
            "ADD":    ("\u2795", "#30D158"),
            "DEL":    ("\u274c", "#FF453A"),
            "MOVE":   ("\u27a1", "#0a84ff"),
            "RATING": ("\u2b50", "#FFD60A"),
            "RENAME": ("\u270f", "#FF9F0A"),
            "TODO":   ("\u2610", "#8e8e93"),
        }

        html = "<table style='font-family:SF Mono,Consolas,monospace;font-size:11px;border-collapse:collapse;white-space:nowrap;'>"

        if self._pending_changes:
            html += ("<tr><td colspan='4' style='color:#FF9F0A;font-weight:700;"
                     "font-size:12px;padding:4px 0;'>\u23f3 PENDING QUEUE</td></tr>")
            for action, song, detail in self._pending_changes:
                icon, color = icons.get(action, ("\u2022", "#8e8e93"))
                html += (f"<tr style='background:#2a1e0a;'>"
                         f"<td style='color:{color};font-size:14px;padding:3px 8px 3px 4px;width:20px;'>{icon}</td>"
                         f"<td style='color:#FF9F0A;padding:3px 8px;'>\u23f3 Waiting</td>"
                         f"<td style='color:#f2f2f7;font-weight:600;padding:3px 4px;'>{song.title}</td>"
                         f"<td style='color:#8e8e93;padding:3px 4px;'>{detail}</td></tr>")
            html += "<tr><td colspan='4' style='border-bottom:1px solid #3a3a3c;'></td></tr>"

        if not lines:
            html += "<tr><td colspan='4' style='color:#8e8e93;padding:8px;'>No changes recorded yet.</td></tr>"
        else:
            for line in reversed(lines):
                parts = line.split(",", 3)
                if len(parts) >= 3:
                    ts, action, title = parts[0], parts[1], parts[2]
                    detail = parts[3] if len(parts) > 3 else ""
                    icon, color = icons.get(action, ("\u2022", "#8e8e93"))
                    html += (f"<tr>"
                             f"<td style='color:{color};font-size:14px;padding:2px 8px 2px 4px;width:20px;'>{icon}</td>"
                             f"<td style='color:#6e6e73;padding:2px 6px;'>{ts}</td>"
                             f"<td style='color:#f2f2f7;font-weight:600;padding:2px 4px;'>{title}</td>"
                             f"<td style='color:#8e8e93;padding:2px 4px;'>{detail}</td></tr>")
        html += "</table>"

        dlg = QDialog(self)
        dlg.setWindowTitle("Change History")
        dlg.setFixedSize(700, 400)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(12, 12, 12, 12)

        text_view = QTextEdit()
        text_view.setReadOnly(True)
        text_view.setHtml(html)
        text_view.setLineWrapMode(QTextEdit.NoWrap)
        text_view.setStyleSheet(
            "QTextEdit{background:#1c1c1e;border:1px solid #2c2c2e;"
            "border-radius:6px;color:#f2f2f7;}")
        lay.addWidget(text_view)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        lay.addWidget(close_btn, alignment=Qt.AlignRight)

        dlg.exec()

    def _update_pending_btn(self):
        n_pending = len(self._pending_changes)
        n_bg = self._task_worker.pending_count() if hasattr(self, '_task_worker') else 0
        n = n_pending + n_bg
        self.pending_btn.setText(f"Queue ({n})")
        if n > 0:
            self.pending_btn.setStyleSheet(
                "QPushButton{color:#FF9F0A;font-weight:600;}")
        else:
            self.pending_btn.setStyleSheet("")

    def _show_pending(self):
        from PySide6.QtWidgets import QDialog, QTextEdit

        icons = {
            "MOVE":   ("\u27a1", "#0a84ff"),
            "RENAME": ("\u270f", "#FF9F0A"),
            "RATING": ("\u2b50", "#FFD60A"),
            "DELETE": ("\u274c", "#FF453A"),
        }

        bg_tasks = self._task_worker.pending_tasks() if hasattr(self, '_task_worker') else []
        total = len(self._pending_changes) + len(bg_tasks)

        html = "<div style='font-family:SF Mono,Consolas,monospace;font-size:12px;'>"

        if bg_tasks:
            html += (f"<p style='color:#0a84ff;font-weight:700;'>"
                     f"\u2699 {len(bg_tasks)} background task(s) running</p>")
            for t in bg_tasks:
                action = t.get('action', '?')
                icon, color = icons.get(action, ("\u2022", "#8e8e93"))
                title = t.get('title', t.get('new_title', '?'))
                detail = t.get('target_cat', t.get('new_title', ''))
                html += (f"<p style='margin:4px 0;'>"
                         f"<span style='color:{color};font-size:14px;'>{icon}</span>"
                         f"&nbsp;&nbsp;&nbsp;"
                         f"<span style='color:#f2f2f7;font-weight:600;'>{title}</span> "
                         f"<span style='color:#8e8e93;'>\u2014 {action}: {detail}</span></p>")
            html += "<hr style='border-color:#3a3a3c;'>"

        if self._pending_changes:
            html += (f"<p style='color:#FF9F0A;font-weight:700;'>"
                     f"\u23f3 {len(self._pending_changes)} waiting for playback</p>")
            for action, song, detail in self._pending_changes:
                icon, color = icons.get(action, ("\u2022", "#8e8e93"))
                html += (f"<p style='margin:4px 0;'>"
                         f"<span style='color:{color};font-size:14px;'>{icon}</span>"
                         f"&nbsp;&nbsp;&nbsp;"
                         f"<span style='color:#f2f2f7;font-weight:600;'>{song.title}</span> "
                         f"<span style='color:#8e8e93;'>\u2014 {action}: {detail}</span></p>")

        if not bg_tasks and not self._pending_changes:
            html += "<p style='color:#30D158;'>\u2713  All tasks complete — queue is empty.</p>"

        html += "</div>"

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Pending Queue ({total})")
        dlg.setFixedSize(650, 350)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(12, 12, 12, 12)

        tv = QTextEdit()
        tv.setReadOnly(True)
        tv.setHtml(html)
        tv.setLineWrapMode(QTextEdit.NoWrap)
        tv.setStyleSheet("QTextEdit{background:#1c1c1e;border:1px solid #2c2c2e;border-radius:6px;}")
        lay.addWidget(tv)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.close)
        lay.addWidget(close_btn, alignment=Qt.AlignRight)
        dlg.exec()

    def _changelog_path(self) -> Path:
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        if not base: base = str(Path.home() / ".music_library_app")
        bp = Path(base); bp.mkdir(parents=True, exist_ok=True)
        return bp / "changelog.csv"

    def _log_change(self, action: str, title: str, detail: str = ""):
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self._changelog_path(), "a", encoding="utf-8") as f:
                f.write(f"{ts},{action},{title},{detail}\n")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Category filter
    # ------------------------------------------------------------------
    def _rebuild_cat_filter(self):
        cats = list_categories(self.root_path) if self.root_path else []
        if cats == self._last_cats: return
        self._last_cats = cats

        for c in cats:
            if c not in self._cat_checks:
                self._cat_checks[c] = True
        for c in list(self._cat_checks):
            if c not in cats:
                del self._cat_checks[c]

        self._update_cat_btn_label()

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

    def _on_accordion_toggled(self, checked: bool):
        self._filter_accordion = checked
        self._apply_filters()

    def _on_christmas_toggled(self, checked: bool):
        self._filter_christmas = checked
        self._apply_filters()

    def _show_cat_checkboxes(self):
        from PySide6.QtWidgets import QMenu, QWidgetAction, QCheckBox

        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#2c2c2e;border:1px solid #3a3a3c;"
            "border-radius:8px;padding:6px 0;}")

        cat_cbs: list = []
        self._pending_cat_change = False

        def _on_checkbox_toggled():
            self._pending_cat_change = True
            self.cat_filter_btn.setText("Apply Selection")
            self.cat_filter_btn.setStyleSheet(
                "QPushButton{background:rgba(10,132,255,0.25);"
                "border:1.5px solid #0a84ff;border-radius:8px;"
                "color:#0a84ff;font-size:12px;font-weight:700;"
                "padding:4px 10px;}")

        def _select_only(cat_name: str):
            for c in self._cat_checks:
                self._cat_checks[c] = (c == cat_name)
            self._pending_cat_change = False
            menu.close()

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
        def _select_all_and_close():
            for c in self._cat_checks:
                self._cat_checks[c] = True
            menu.close()
        all_lbl.clicked.connect(_select_all_and_close)
        all_lay.addWidget(all_lbl, stretch=1)

        wa_all = QWidgetAction(menu)
        wa_all.setDefaultWidget(all_w)
        menu.addAction(wa_all)
        menu.addSeparator()

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

        if self._pending_cat_change:
            for cat, cb in cat_cbs:
                self._cat_checks[cat] = cb.isChecked()

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
        self.accordion_btn.blockSignals(True); self.accordion_btn.setChecked(False); self.accordion_btn.blockSignals(False)
        self.christmas_btn.blockSignals(True); self.christmas_btn.setChecked(False); self.christmas_btn.blockSignals(False)
        self._filter_search = ""; self._filter_rating = 0
        self._filter_collab = False; self._filter_cover = False
        self._filter_accordion = False; self._filter_christmas = False
        for c in self._cat_checks:
            self._cat_checks[c] = True
        self._update_cat_btn_label()
        self._apply_filters()

    def _matches(self, song: Song) -> bool:
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
            return False
        if self._filter_rating > 0 and song.rating < self._filter_rating:
            return False
        if self._filter_collab and "collab" not in song.title.lower():
            return False
        if self._filter_cover and "cover" not in song.title.lower():
            return False
        if self._filter_accordion and "[a]" not in song.title.lower():
            return False
        if self._filter_christmas and "[c]" not in song.title.lower():
            return False
        return True

    _last_sort_col = 0
    _last_sort_order = Qt.AscendingOrder

    def _on_sort_changed(self, logical_index: int, order):
        sortable = {COL_ARTIST, COL_SONGNAME, COL_CATEGORY, COL_RATING}
        if logical_index not in sortable:
            self.table.horizontalHeader().blockSignals(True)
            self.table.sortItems(self._last_sort_col, self._last_sort_order)
            self.table.horizontalHeader().blockSignals(False)
            return

        self._last_sort_col = logical_index
        self._last_sort_order = order

        for i, base_name in enumerate(self._base_headers):
            if i == logical_index:
                arrow = " \u25bc" if order == Qt.AscendingOrder else " \u25b2"
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
        queue_num = 0
        for visual in range(self.table.rowCount()):
            if not self.table.isRowHidden(visual):
                queue_num += 1
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
        existing_id = None
        for sid, s in self.songs_by_id.items():
            if s.path == song.path and sid != song.id:
                existing_id = sid
                break
        if existing_id:
            old_song = self.songs_by_id[existing_id]
            old_song.title = song.title
            old_song.artist = song.artist
            old_song.rating = song.rating
            old_song.category = song.category
            old_song.duration = song.duration
            song = old_song

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
            cat_sort = self.table.item(row, COL_CATEGORY)
            if cat_sort: cat_sort.setText(song.category)
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

        # If a task for this song is already pending (e.g. loaded from
        # the persisted queue on startup), show the spinner now.
        if song.id in self._pending_task_state:
            ti.setIcon(self._get_spinner_icon())

        sn = QTableWidgetItem(song_part)
        self.table.setItem(row, COL_SONGNAME, sn)

        name_lock = LockButton(
            "Green pen: name is protected. Click to edit.",
            "Red pen: name editing active.")
        name_lock.toggledLock.connect(
            lambda unlocked, s=song, lk=name_lock: self._on_title_edit_unlock(s, lk, unlocked))
        self.table.setCellWidget(row, COL_NAME_EDIT, _centered(name_lock))

        cats = list_categories(self.root_path) if self.root_path else []
        combo = QComboBox(); combo.addItems(cats)
        if song.category in cats: combo.setCurrentText(song.category)
        combo.setEnabled(False)
        combo.currentTextChanged.connect(
            lambda nc, s=song, c=combo: self._on_cat_combo(s, c, nc))
        cat_sort_item = QTableWidgetItem(song.category)
        self.table.setItem(row, COL_CATEGORY, cat_sort_item)
        self.table.setCellWidget(row, COL_CATEGORY, combo)

        cat_lock = LockButton(
            "Green pen: category is protected. Click to allow editing.",
            "Red pen: editing active. Select a new category to move the file.")
        cat_lock.toggledLock.connect(lambda unlocked, c=combo: c.setEnabled(unlocked))
        self.table.setCellWidget(row, COL_CATEGORY_LOCK, _centered(cat_lock))

        star_wrapper = QWidget()
        star_lay = QHBoxLayout(star_wrapper)
        star_lay.setContentsMargins(4, 0, 4, 0)
        star_lay.setSpacing(0)
        star = StarRatingWidget(song.rating)
        star.set_editable(False)
        star.ratingChanged.connect(lambda nr, s=song, sw=star: self._on_rating(s, sw, nr))
        star_lay.addWidget(star)
        star_lay.addStretch()
        rat_sort_item = _InvisibleSortItem(song.rating)
        self.table.setItem(row, COL_RATING, rat_sort_item)
        self.table.setCellWidget(row, COL_RATING, star_wrapper)

        rat_lock = LockButton(
            "Green pen: rating is protected. Click to allow editing.",
            "Red pen: editing active. Click a star to set the rating."
        )
        rat_lock.toggledLock.connect(lambda unlocked, sw=star: sw.set_editable(unlocked))
        self.table.setCellWidget(row, COL_RATING_LOCK, _centered(rat_lock, right_pad=14))

        combo.setProperty("lock_ref", cat_lock)
        star.setProperty("lock_ref", rat_lock)

        is_todo = song.path.suffix.lower() in TODO_EXTENSIONS
        if is_todo:
            cat_lock.setVisible(False)
            rat_lock.setVisible(False)
            combo.setEnabled(False)

        del_btn = QToolButton()
        del_btn.setText("\U0001f5d1")
        del_btn.setToolTip("Delete this file permanently")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setFixedSize(22, 22)
        del_btn.setStyleSheet(
            "QToolButton{background:transparent;border:none;color:#8e8e93;font-size:13px;}"
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
        sel_rows = set(idx.row() for idx in self.table.selectedIndexes())
        multi = len(sel_rows) >= 2
        self.bulk_cat_combo.setEnabled(multi)
        self.bulk_rat_combo.setEnabled(multi)
        self.bulk_del_btn.setEnabled(multi)
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

        self._suppress_rescan = True
        queued = 0
        for song in songs:
            if song.category == new_cat:
                continue
            if self.player.current_song() is song and self.player.is_playing():
                continue
            task_id = f"bulkmove_{song.id}_{uuid.uuid4().hex[:8]}"
            self._task_worker.add_task({
                'id': task_id,
                'action': 'MOVE',
                'song_id': song.id,
                'src_path': str(song.path),
                'root': str(self.root_path),
                'target_cat': new_cat,
                'old_cat': song.category,
                'title': song.title,
            })
            self._mark_task_pending(song.id, task_id, 'MOVE')
            queued += 1

        self.bulk_cat_combo.setCurrentIndex(0)
        self._save_pending_queue()
        self._show_toast(
            f"\u23f3  {queued} songs queued for move \u2192 '{new_cat}'",
            3000, "info")

    def _on_bulk_rating(self, idx: int):
        if idx <= 0:
            return
        new_rating = self.bulk_rat_combo.itemData(idx)
        if new_rating is None or new_rating < 0:
            return
        songs = self._selected_songs()
        if not songs:
            return

        self._suppress_rescan = True
        self._rescan_timer.stop()
        self._set_info(f"Queuing rating for {len(songs)} files\u2026",
                       "loading", auto_reset=False)

        count = 0
        for song in songs:
            song.rating = new_rating
            item = self.row_items.get(song.id)
            if item:
                rat_sort = self.table.item(item.row(), COL_RATING)
                if isinstance(rat_sort, _InvisibleSortItem):
                    rat_sort.set_sort_key(new_rating)
                star_wrap = self.table.cellWidget(item.row(), COL_RATING)
                if star_wrap:
                    star = star_wrap.findChild(StarRatingWidget)
                    if star: star.set_rating(new_rating)
            self._queue_rating_task(song, new_rating)
            count += 1

        self.bulk_rat_combo.setCurrentIndex(0)
        self._update_queue_numbers()

        stars = "\u2605" * new_rating + "\u2606" * (5 - new_rating)
        self._set_info(
            f"\u23f3  Queued rating {stars} ({new_rating}/5) for {count} songs",
            "success", duration_ms=3500)

    def _on_bulk_delete(self):
        songs = self._selected_songs()
        if not songs:
            return
        reply = QMessageBox.question(
            self, "Delete files",
            f"Permanently delete {len(songs)} selected file(s)?\n\nThis cannot be undone.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._suppress_rescan = True
        queued = 0
        for song in songs:
            if self.player.current_song() is song:
                self.player._player.stop()
            task_id = f"bulkdel_{song.id}_{uuid.uuid4().hex[:8]}"
            self._task_worker.add_task({
                'id': task_id, 'action': 'DELETE',
                'song_id': song.id,
                'path': str(song.path),
                'title': song.title,
            })
            self._mark_task_pending(song.id, task_id, 'DELETE')
            queued += 1
        self._save_pending_queue()
        self._show_toast(f"\u23f3  Queued delete: {queued} file(s)", 3000, "info")

    def _on_double_click(self, index):
        item = self.table.item(index.row(), COL_ARTIST)
        if item is None: return
        song = self.songs_by_id.get(item.data(Qt.UserRole))
        if song: self.player.play_song(song, queue=self._visible_songs_in_order())

    _CSS_LOCK_DISABLED = (
        "QToolButton{background:transparent;border:none;"
        "color:#48484a;font-size:13px;}"
    )

    def _highlight(self, playing: Optional[Song]):
        if self._highlighted_id:
            prev = self.row_items.get(self._highlighted_id)
            if prev:
                row = prev.row()
                f = prev.font(); f.setBold(False); prev.setFont(f)
                prev.setForeground(Qt.white)
                sn = self.table.item(row, COL_SONGNAME)
                if sn:
                    sf = sn.font(); sf.setBold(False); sn.setFont(sf)
                    sn.setForeground(Qt.white)
                for col in (COL_NAME_EDIT, COL_CATEGORY_LOCK):
                    w = self.table.cellWidget(row, col)
                    if w:
                        lk = w.findChild(LockButton)
                        if lk:
                            lk.setEnabled(True)
                            lk.set_locked(True)
                            lk.setToolTip(lk._tip_locked)

        self._highlighted_id = playing.id if playing else None

        if playing:
            item = self.row_items.get(playing.id)
            if item:
                row = item.row()
                f = item.font(); f.setBold(True); item.setFont(f)
                item.setForeground(Qt.cyan)
                sn = self.table.item(row, COL_SONGNAME)
                if sn:
                    sf = sn.font(); sf.setBold(True); sn.setFont(sf)
                    sn.setForeground(Qt.cyan)
                for col in (COL_NAME_EDIT, COL_CATEGORY_LOCK):
                    w = self.table.cellWidget(row, col)
                    if w:
                        lk = w.findChild(LockButton)
                        if lk:
                            lk.set_locked(True)
                            lk.setEnabled(False)
                            lk.setStyleSheet(self._CSS_LOCK_DISABLED)
                            lk.setToolTip(
                                "Cannot edit while this song is playing.\n"
                                "Play a different song first.")

    # ------------------------------------------------------------------
    # Category change
    # ------------------------------------------------------------------
    def _on_title_edit_unlock(self, song: Song, lock: "LockButton", unlocked: bool):
        item = self.row_items.get(song.id)
        if item is None:
            lock.set_locked(True); return
        row = item.row()
        old_artist, old_songname = _split_title(song.title)

        def _commit_name(new_artist: str, new_songname: str):
            if new_artist and new_songname:
                new_title = f"{new_artist} - {new_songname}"
            elif new_songname:
                new_title = new_songname
            else:
                new_title = song.title

            a_part, s_part = _split_title(new_title)
            item.setText(a_part)
            sn_item = self.table.item(row, COL_SONGNAME)
            if sn_item: sn_item.setText(s_part)

            if new_title != song.title:
                current = self.player.current_song()
                is_active = (current is not None and current.id == song.id and
                            (self.player.is_playing() or
                             self.player._player.playbackState() != QMediaPlayer.StoppedState))

                if is_active:
                    self._pending_changes.append(("RENAME", song, new_title))
                    self._update_pending_btn()
                    lock.setText("\u23f3")
                    lock.setStyleSheet(
                        "QToolButton{background:transparent;border:none;"
                        "color:#FF9F0A;font-size:13px;}")
                    lock.setEnabled(False)
                    self._show_toast(
                        f"\u23f3  Rename queued: \"{new_title}\"  (will apply when song finishes)",
                        4000, "warning")
                    return

                old_title = song.title
                # Queue the disk write — UI already shows the new title
                task_id = f"rename_{song.id}_{uuid.uuid4().hex[:8]}"
                self._task_worker.add_task({
                    'id': task_id,
                    'action': 'RENAME',
                    'song_id': song.id,
                    'src_path': str(song.path),
                    'new_title': new_title,
                    'old_title': old_title,
                    'artist': song.artist,
                })
                # Optimistically update in-memory title so UI is consistent
                song.title = new_title
                self._mark_task_pending(song.id, task_id, 'RENAME')
                self._save_pending_queue()
                self._set_info(f'\u23f3  Renaming: "{new_title}"\u2026',
                               "loading", auto_reset=False)
                self._log_change("RENAME", new_title, f"was: {old_title}")

        if not unlocked:
            ed_artist = self.table.cellWidget(row, COL_ARTIST)
            ed_song   = self.table.cellWidget(row, COL_SONGNAME)
            new_a = ed_artist.text().strip() if isinstance(ed_artist, QLineEdit) else old_artist
            new_s = ed_song.text().strip() if isinstance(ed_song, QLineEdit) else old_songname
            self.table.removeCellWidget(row, COL_ARTIST)
            self.table.removeCellWidget(row, COL_SONGNAME)
            _commit_name(new_a, new_s or old_songname)
            return

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

        ed_a.returnPressed.connect(lambda: ed_s.setFocus())
        ed_s.returnPressed.connect(commit_enter)

    def _sync_cache(self, song: Song):
        try:
            st  = song.path.stat()
            rec = self.cache.get(str(song.path), {})
            rec.update({"size": st.st_size, "mtime": st.st_mtime,
                        "title": song.title, "artist": song.artist,
                        "rating": song.rating, "duration": song.duration,
                        "id": song.id})
            self.cache[str(song.path)] = rec
            if self.root_path: self._schedule_cache_save()
        except OSError:
            pass

    def _on_cat_combo(self, song: Song, combo: QComboBox, new_cat: str):
        if new_cat == song.category or not combo.isEnabled(): return

        current = self.player.current_song()
        if current and current.id == song.id and self.player.is_playing():
            self._show_toast(
                "Stop playing this song first before changing its category.",
                3500, "warning")
            combo.blockSignals(True)
            combo.setCurrentText(song.category)
            combo.blockSignals(False)
            return

        old_cat = song.category
        self._suppress_rescan = True
        lock: LockButton = combo.property("lock_ref")
        if lock: lock.set_locked(True)
        combo.setEnabled(False)

        task_id = f"move_{song.id}_{uuid.uuid4().hex[:8]}"
        self._task_worker.add_task({
            'id': task_id,
            'action': 'MOVE',
            'song_id': song.id,
            'src_path': str(song.path),
            'root': str(self.root_path),
            'target_cat': new_cat,
            'old_cat': old_cat,
            'title': song.title,
        })
        self._mark_task_pending(song.id, task_id, 'MOVE')
        self._save_pending_queue()
        self._show_toast(
            f"\u23f3  Moving '{song.title}' \u2192 {new_cat}\u2026", 2000, "info")

    # ------------------------------------------------------------------
    # Rating change
    # ------------------------------------------------------------------
    def _on_rating(self, song: Song, star_widget: "StarRatingWidget", new_rating: int):
        item = self.row_items.get(song.id)
        song.rating = new_rating
        if item:
            rat_sort = self.table.item(item.row(), COL_RATING)
            if isinstance(rat_sort, _InvisibleSortItem):
                rat_sort.set_sort_key(new_rating)
            self.table.setRowHidden(item.row(), not self._matches(song))
            self.table.selectRow(item.row())
        lock: LockButton = star_widget.property("lock_ref")
        if lock: lock.set_locked(True)
        star_widget.set_editable(False)
        self._queue_rating_task(song, new_rating)
        stars = "\u2605" * new_rating + "\u2606" * (5 - new_rating)
        self._log_change("RATING", song.title, f"{new_rating}/5")
        self._show_toast(f"\u2713  {stars} ({new_rating}/5) \u2014 {song.title}", 2000, "success")


# ============================================================================
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_QSS)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)

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