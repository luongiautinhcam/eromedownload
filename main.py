#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Erome Album Downloader v3.0 (UI rewrite)
- PySide6 (Qt) UI with table-based tracking
- Active Downloads (with per-file progress)
- History table for completed/failed items
- QThreadPool + QRunnable workers, configurable concurrency
- Placeholder get_album_items() to plug real parser later

Requirements:
  pip install -r requirements.txt

Run:
  python main.py
"""

from __future__ import annotations

import os
import sys
import time
import math
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
from urllib.parse import urlparse
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PySide6.QtCore import (
    Qt, QAbstractTableModel, QModelIndex, QRect, QSize, QTimer,
    Signal, QObject, QRunnable, QThreadPool, QDateTime
)
from PySide6.QtGui import QColor, QBrush, QPainter
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGroupBox, QLabel, QLineEdit, QPushButton, QFileDialog, QSpinBox,
    QCheckBox, QSplitter, QTableView, QStyledItemDelegate, QStyleOptionProgressBar,
    QStyle, QProgressBar
)


# ---------------------------
# Helpers / Formatting
# ---------------------------

def human_bytes(n: Optional[int]) -> str:
    if n is None:
        return "Unknown"
    if n < 0:
        return "Unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    if i == 0:
        return f"{int(f)} {units[i]}"
    return f"{f:.2f} {units[i]}"


def human_speed(bps: float) -> str:
    if bps <= 0:
        return "0 B/s"
    return f"{human_bytes(int(bps))}/s"


def human_eta(seconds: Optional[int]) -> str:
    if seconds is None or seconds < 0:
        return ""
    m, s = divmod(int(seconds), 60)
    if m == 0:
        return f"{s}s"
    h, m = divmod(m, 60)
    if h == 0:
        return f"{m}m{s}s"
    return f"{h}h{m}m"


def guess_type_from_filename(name: str) -> str:
    ext = os.path.splitext(name.lower())[1]
    if ext in {".mp4", ".webm", ".mkv", ".mov", ".m4v"}:
        return "video"
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return "image"
    return "other"


# ---------------------------
# Data Model
# ---------------------------

STATUS_QUEUED = "QUEUED"
STATUS_DOWNLOADING = "DOWNLOADING"
STATUS_PAUSED = "PAUSED"
STATUS_DONE = "DONE"
STATUS_ERROR = "ERROR"


@dataclass
class DownloadItem:
    id: int
    url: str
    filename: str
    filetype: str
    total_bytes: Optional[int] = None
    downloaded_bytes: int = 0
    status: str = STATUS_QUEUED
    speed_bps: float = 0.0
    eta_seconds: Optional[int] = None
    worker_id: Optional[int] = None
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    error_message: Optional[str] = None
    save_path: Optional[str] = None


# ---------------------------
# Qt Table Models
# ---------------------------

class ActiveDownloadsModel(QAbstractTableModel):
    COLS = [
        "#", "Status", "File name", "Type", "Total size",
        "Downloaded", "Progress", "Speed", "ETA", "Thread"
    ]

    def __init__(self, items: List[DownloadItem] | None = None):
        super().__init__()
        self.items: List[DownloadItem] = items or []

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self.items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLS[section]
        return super().headerData(section, orientation, role)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemIsEnabled
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        col = index.column()

        if role == Qt.DisplayRole:
            if col == 0:
                return item.id
            elif col == 1:
                return item.status
            elif col == 2:
                return item.filename
            elif col == 3:
                return item.filetype
            elif col == 4:
                return human_bytes(item.total_bytes)
            elif col == 5:
                return human_bytes(item.downloaded_bytes)
            elif col == 6:
                if item.total_bytes and item.total_bytes > 0:
                    pct = int((item.downloaded_bytes / item.total_bytes) * 100)
                else:
                    pct = 0
                return pct
            elif col == 7:
                return human_speed(item.speed_bps)
            elif col == 8:
                return human_eta(item.eta_seconds)
            elif col == 9:
                return item.worker_id if item.worker_id is not None else "-"

        if role == Qt.TextAlignmentRole:
            if col in (0, 4, 5, 6, 7, 8, 9):
                return Qt.AlignCenter

        if role == Qt.ForegroundRole and item.status == STATUS_ERROR:
            return QBrush(QColor("red"))

        return None

    # Utilities to update rows efficiently
    def append_items(self, items: List[DownloadItem]):
        start = len(self.items)
        end = start + len(items) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        self.items.extend(items)
        self.endInsertRows()

    def update_item(self, row: int):
        top_left = self.index(row, 0)
        bottom_right = self.index(row, self.columnCount() - 1)
        self.dataChanged.emit(top_left, bottom_right, [Qt.DisplayRole])

    def remove_row(self, row: int):
        self.beginRemoveRows(QModelIndex(), row, row)
        self.items.pop(row)
        self.endRemoveRows()


class HistoryModel(QAbstractTableModel):
    COLS = ["Time", "Result", "File name", "Type", "Size", "Duration", "Message"]

    def __init__(self):
        super().__init__()
        self.rows: List[Tuple[str, str, str, str, str, str, str]] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLS[section]
        return super().headerData(section, orientation, role)

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemIsEnabled
        return Qt.ItemIsSelectable | Qt.ItemIsEnabled

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.DisplayRole:
            return row[index.column()]
        if role == Qt.TextAlignmentRole and index.column() in (0, 1, 4, 5):
            return Qt.AlignCenter
        if role == Qt.ForegroundRole and row[1] == STATUS_ERROR:
            return QBrush(QColor("red"))
        return None

    def add_row(self, when: float, result: str, filename: str, ftype: str,
                size_bytes: Optional[int], duration: float, message: str):
        dt = QDateTime.fromSecsSinceEpoch(int(when)).toString("yyyy-MM-dd HH:mm:ss")
        size_str = human_bytes(size_bytes)
        dur_str = f"{duration:.2f}s"
        new_row = (dt, result, filename, ftype, size_str, dur_str, message or "")
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
        self.rows.append(new_row)
        self.endInsertRows()

    def clear(self):
        if not self.rows:
            return
        self.beginRemoveRows(QModelIndex(), 0, len(self.rows) - 1)
        self.rows = []
        self.endRemoveRows()


# ---------------------------
# Progress Delegate
# ---------------------------

class ProgressBarDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option, index):
        if index.column() != 6:
            super().paint(painter, option, index)
            return
        value = index.data(Qt.DisplayRole)
        if value is None:
            value = 0
        progress_opt = QStyleOptionProgressBar()
        progress_opt.rect = option.rect
        progress_opt.minimum = 0
        progress_opt.maximum = 100
        progress_opt.progress = int(value)
        progress_opt.text = f"{int(value)}%" if value > 0 else ""
        progress_opt.textVisible = True
        QApplication.style().drawControl(QStyle.CE_ProgressBar, progress_opt, painter)

    def sizeHint(self, option, index):
        if index.column() == 6:
            return QSize(100, 18)
        return super().sizeHint(option, index)


# ---------------------------
# Worker Signals and Runnable
# ---------------------------

class DownloadSignals(QObject):
    started = Signal(int, int)  # item_id, worker_id
    progress = Signal(int, int, object, float, object)  # item_id, downloaded, total, speed, eta
    status = Signal(int, str)  # item_id, status
    finished = Signal(int, bool, str, object)  # item_id, success, err, total


class DownloadWorker(QRunnable):
    def __init__(self, item: DownloadItem, save_dir: str, worker_id: int,
                 session: Optional[requests.Session], pause_flag: threading.Event,
                 stop_flag: threading.Event, chunk_size: int = 64 * 1024):
        super().__init__()
        self.item = item
        self.save_dir = save_dir
        self.worker_id = worker_id
        self.session = session or requests.Session()
        self.pause_flag = pause_flag
        self.stop_flag = stop_flag
        self.chunk_size = chunk_size
        self.signals = DownloadSignals()

    def run(self):
        item = self.item
        url = item.url
        filename = item.filename
        path = os.path.join(self.save_dir, filename)
        item.save_path = path

        # Use session-level default headers (set by core)
        headers = None

        self.signals.started.emit(item.id, self.worker_id)
        self.signals.status.emit(item.id, STATUS_DOWNLOADING)

        start = time.time()
        last_emit = start
        bytes_last = 0
        item.start_time = start

        try:
            # Skip if file already exists (treat as done)
            if os.path.exists(path):
                # probe head for size if available (optional)
                try:
                    hr = self.session.head(url, timeout=15)
                    total_hdr = hr.headers.get("Content-Length")
                    total_bytes = int(total_hdr) if total_hdr and total_hdr.isdigit() else None
                except Exception:
                    total_bytes = None
                item.end_time = time.time()
                self.signals.finished.emit(item.id, True, "", total_bytes)
                return

            with self.session.get(url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = r.headers.get("Content-Length")
                total_bytes = int(total) if total and total.isdigit() else None

                # If overwriting existing, start fresh
                if os.path.exists(path):
                    os.remove(path)

                with open(path, "wb") as f:
                    downloaded = 0
                    speed_bps = 0.0
                    eta = None
                    ma_window: List[Tuple[float, int]] = []  # (time, bytes)

                    for chunk in r.iter_content(chunk_size=self.chunk_size):
                        if self.stop_flag.is_set():
                            raise RuntimeError("Aborted")

                        # Global pause handling
                        while self.pause_flag.is_set():
                            time.sleep(0.1)

                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)

                        # Moving average speed over last ~3 seconds
                        now = time.time()
                        ma_window.append((now, downloaded))
                        # drop points older than 3s
                        while ma_window and (now - ma_window[0][0] > 3.0):
                            ma_window.pop(0)
                        if len(ma_window) >= 2:
                            dt = ma_window[-1][0] - ma_window[0][0]
                            db = ma_window[-1][1] - ma_window[0][1]
                            speed_bps = (db / dt) if dt > 0 else 0.0
                        else:
                            # first second, rough estimate
                            dt = now - start
                            speed_bps = (downloaded / dt) if dt > 0 else 0.0

                        eta = None
                        if total_bytes and speed_bps > 0:
                            remain = max(total_bytes - downloaded, 0)
                            eta = int(remain / speed_bps)

                        # Emit every ~200ms
                        if now - last_emit >= 0.2:
                            self.signals.progress.emit(item.id, downloaded, total_bytes, speed_bps, eta)
                            last_emit = now

                # Final emit
                self.signals.progress.emit(item.id, downloaded, total_bytes, speed_bps, eta)

            # Success
            item.end_time = time.time()
            self.signals.finished.emit(item.id, True, "", total_bytes)
        except Exception as e:
            item.end_time = time.time()
            err_msg = str(e)
            self.signals.finished.emit(item.id, False, err_msg, None)


# ---------------------------
# Main Window / Controller
# ---------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Erome Album Downloader v3.0")
        self.resize(1000, 700)

        # State
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(3)
        self.pause_flag = threading.Event()
        self.stop_flag = threading.Event()
        self.session: Optional[requests.Session] = None
        self.core: Optional[EromeDownloaderCore] = None

        self.total_count = 0
        self.completed_count = 0
        self.total_videos = 0
        self.total_images = 0

        self.next_worker_id = 1
        self.active_row_by_id: Dict[int, int] = {}

        # Models
        self.active_model = ActiveDownloadsModel()
        self.history_model = HistoryModel()

        # UI
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Source
        grp_source = QGroupBox("Source")
        l_source = QHBoxLayout(grp_source)
        l_source.addWidget(QLabel("Erome Album URL"))
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://www.erome.com/a/...")
        l_source.addWidget(self.url_input, 1)
        root.addWidget(grp_source)

        # Save
        grp_save = QGroupBox("Save")
        l_save = QHBoxLayout(grp_save)
        l_save.addWidget(QLabel("Save folder"))
        self.path_input = QLineEdit(os.path.join(os.getcwd(), "downloads"))
        self.path_input.setReadOnly(True)
        l_save.addWidget(self.path_input, 1)
        self.choose_btn = QPushButton("Choose…")
        self.choose_btn.clicked.connect(self.on_choose_folder)
        l_save.addWidget(self.choose_btn)
        root.addWidget(grp_save)

        # Options
        grp_opts = QGroupBox("Options")
        l_opts = QHBoxLayout(grp_opts)
        l_opts.addWidget(QLabel("Threads"))
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 16)
        self.threads_spin.setValue(3)
        self.threads_spin.valueChanged.connect(self.on_threads_changed)
        l_opts.addWidget(self.threads_spin)
        self.metadata_checkbox = QCheckBox("Download album metadata (JSON)")
        self.metadata_checkbox.setChecked(True)
        l_opts.addWidget(self.metadata_checkbox)
        l_opts.addStretch(1)
        root.addWidget(grp_opts)

        # Overall Progress
        grp_overall = QGroupBox("Overall Progress")
        v_overall = QVBoxLayout(grp_overall)
        self.overall_status_label = QLabel("Downloading: 0 / 0 (0%) | Video: 0 | Images: 0")
        v_overall.addWidget(self.overall_status_label)
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 100)
        v_overall.addWidget(self.overall_progress)
        root.addWidget(grp_overall)

        # Splitter with Active + History
        splitter = QSplitter()
        splitter.setOrientation(Qt.Vertical)

        grp_active = QGroupBox("Active Downloads")
        v_active = QVBoxLayout(grp_active)
        self.active_table = QTableView()
        self.active_table.setModel(self.active_model)
        self.active_table.setItemDelegateForColumn(6, ProgressBarDelegate())
        self.active_table.horizontalHeader().setStretchLastSection(True)
        self.active_table.setAlternatingRowColors(True)
        v_active.addWidget(self.active_table)

        grp_history = QGroupBox("History")
        v_hist = QVBoxLayout(grp_history)
        self.history_table = QTableView()
        self.history_table.setModel(self.history_model)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        self.history_table.setAlternatingRowColors(True)
        v_hist.addWidget(self.history_table)

        splitter.addWidget(grp_active)
        splitter.addWidget(grp_history)
        splitter.setSizes([450, 250])
        root.addWidget(splitter, 1)

        # Bottom buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.on_start)
        btn_row.addWidget(self.start_btn)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.on_pause)
        btn_row.addWidget(self.pause_btn)
        self.clear_hist_btn = QPushButton("Clear history")
        self.clear_hist_btn.clicked.connect(self.on_clear_history)
        btn_row.addWidget(self.clear_hist_btn)
        self.exit_btn = QPushButton("Exit")
        self.exit_btn.clicked.connect(self.close)
        btn_row.addWidget(self.exit_btn)
        root.addLayout(btn_row)

        # Timer to refresh overall status periodically (in case of missed signals)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(500)
        self.refresh_timer.timeout.connect(self.update_overall_status)
        self.refresh_timer.start()

    # -------- Buttons ---------
    def on_choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose folder", self.path_input.text())
        if folder:
            self.path_input.setText(folder)

    def on_threads_changed(self, val: int):
        self.thread_pool.setMaxThreadCount(val)

    def on_start(self):
        # Resume if paused
        if self.pause_flag.is_set():
            self.pause_flag.clear()
            self.pause_btn.setText("Pause")
            self.start_btn.setEnabled(False)
            return

        # Fresh start
        url = self.url_input.text().strip()
        if not url:
            url = "https://www.erome.com/a/"
        base_dir = self.path_input.text().strip()
        # Derive album_id folder from URL
        album_id_match = re.search(r'erome\.com/a/(\w+)', url)
        album_id = album_id_match.group(1) if album_id_match else "album"
        save_dir = os.path.join(base_dir, album_id)
        os.makedirs(save_dir, exist_ok=True)
        self.current_save_dir = save_dir

        # Init core (session with retries + headers)
        self.core = EromeDownloaderCore(url)
        self.session = self.core.session

        # Optional metadata JSON
        if self.metadata_checkbox.isChecked():
            meta_path = os.path.join(save_dir, "album_info.json")
            try:
                metadata = self.core.get_metadata()
            except Exception:
                metadata = None
            if metadata is None:
                metadata = {
                    "url": url,
                    "download_date": QDateTime.currentDateTime().toString(Qt.ISODate)
                }
            import json
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

        # Build queue from placeholder
        # Build queue from real Erome page
        try:
            videos, images = self.core.get_file_list()
        except Exception as e:
            videos, images = [], []
        items_info: List[Tuple[str, str, str]] = []
        for v in videos:
            fname = os.path.basename(urlparse(v).path)
            items_info.append((v, fname, "video"))
        for img in images:
            fname = os.path.basename(urlparse(img).path)
            items_info.append((img, fname, "image"))
        items: List[DownloadItem] = []
        self.total_videos = 0
        self.total_images = 0
        for i, (file_url, fname, ftype) in enumerate(items_info, start=1):
            if not fname:
                fname = os.path.basename(urlparse(file_url).path) or f"file_{i}"
            if not ftype:
                ftype = guess_type_from_filename(fname)
            if ftype == "video":
                self.total_videos += 1
            elif ftype == "image":
                self.total_images += 1
            items.append(DownloadItem(
                id=i, url=file_url, filename=fname, filetype=ftype
            ))

        # Reset state
        self.active_model = ActiveDownloadsModel(items)
        self.active_table.setModel(self.active_model)
        self.active_table.setItemDelegateForColumn(6, ProgressBarDelegate())
        self.history_model = HistoryModel()
        self.history_table.setModel(self.history_model)
        self.active_row_by_id = {item.id: idx for idx, item in enumerate(self.active_model.items)}

        self.total_count = len(items)
        self.completed_count = 0
        self.update_overall_status()

        # Launch first batch according to thread limit
        self.stop_flag.clear()
        self.pause_flag.clear()
        self.start_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)

        self._schedule_work()

    def on_pause(self):
        if not self.pause_flag.is_set():
            self.pause_flag.set()
            # Mark active as PAUSED
            for row, it in enumerate(self.active_model.items):
                if it.status == STATUS_DOWNLOADING:
                    it.status = STATUS_PAUSED
                    self.active_model.update_item(row)
            self.pause_btn.setText("Resume")
            self.start_btn.setEnabled(True)
        else:
            self.pause_flag.clear()
            # Resume active items
            for row, it in enumerate(self.active_model.items):
                if it.status == STATUS_PAUSED:
                    it.status = STATUS_DOWNLOADING
                    self.active_model.update_item(row)
            self.pause_btn.setText("Pause")
            self.start_btn.setEnabled(False)

    def on_clear_history(self):
        self.history_model.clear()

    # -------- Scheduling ---------
    def _schedule_work(self):
        # Start up to maxThreadCount items in QUEUED state
        running = sum(1 for it in self.active_model.items if it.status == STATUS_DOWNLOADING)
        capacity = max(0, self.thread_pool.maxThreadCount() - running)
        if capacity <= 0:
            return

        for row, it in enumerate(self.active_model.items):
            if capacity <= 0:
                break
            if it.status == STATUS_QUEUED:
                worker_id = self.next_worker_id
                self.next_worker_id += 1

                w = DownloadWorker(
                    item=it,
                    save_dir=getattr(self, 'current_save_dir', self.path_input.text().strip()),
                    worker_id=worker_id,
                    session=self.session,
                    pause_flag=self.pause_flag,
                    stop_flag=self.stop_flag,
                )
                w.signals.started.connect(self.on_worker_started)
                w.signals.progress.connect(self.on_worker_progress)
                w.signals.status.connect(self.on_worker_status)
                w.signals.finished.connect(self.on_worker_finished)

                it.worker_id = worker_id
                it.status = STATUS_DOWNLOADING
                it.start_time = time.time()
                self.active_model.update_item(row)

                self.thread_pool.start(w)
                capacity -= 1

        # If still have queued items, set a timer to try again shortly
        if any(it.status == STATUS_QUEUED for it in self.active_model.items):
            QTimer.singleShot(300, self._schedule_work)

    # -------- Signals from workers ---------
    def on_worker_started(self, item_id: int, worker_id: int):
        # Already set in scheduling, keep for completeness
        pass

    def on_worker_status(self, item_id: int, status: str):
        row = self.active_row_by_id.get(item_id)
        if row is None:
            return
        it = self.active_model.items[row]
        it.status = status
        self.active_model.update_item(row)

    def on_worker_progress(self, item_id: int, downloaded: int, total: Optional[int], speed: float, eta: Optional[int]):
        row = self.active_row_by_id.get(item_id)
        if row is None:
            return
        it = self.active_model.items[row]
        it.downloaded_bytes = downloaded
        it.total_bytes = total
        it.speed_bps = speed
        it.eta_seconds = eta
        # status remains DOWNLOADING unless paused externally
        self.active_model.update_item(row)
        self.update_overall_status()

    def on_worker_finished(self, item_id: int, success: bool, err: str, total: Optional[int]):
        row = self.active_row_by_id.get(item_id)
        if row is None:
            return
        it = self.active_model.items[row]
        it.end_time = time.time()
        it.total_bytes = total if total is not None else it.total_bytes

        if success:
            it.status = STATUS_DONE
            result = STATUS_DONE
            message = ""
        else:
            it.status = STATUS_ERROR
            it.error_message = err
            result = STATUS_ERROR
            message = err

        # Move to history
        duration = (it.end_time - (it.start_time or it.end_time))
        self.history_model.add_row(when=it.end_time, result=result, filename=it.filename,
                                   ftype=it.filetype, size_bytes=it.total_bytes, duration=duration,
                                   message=message)

        # Remove from active (keep order stable)
        self.active_model.remove_row(row)
        # Rebuild id->row map
        self.active_row_by_id = {itm.id: idx for idx, itm in enumerate(self.active_model.items)}

        self.completed_count += 1
        self.update_overall_status()

        # Schedule next queued if any
        self._schedule_work()

        # When all done, toggle buttons
        if self.completed_count >= self.total_count:
            self.start_btn.setEnabled(True)
            self.pause_btn.setEnabled(False)
            self.pause_btn.setText("Pause")

    # -------- Overall status ---------
    def update_overall_status(self):
        total = self.total_count
        comp = self.completed_count
        pct = int((comp / total) * 100) if total > 0 else 0
        self.overall_progress.setValue(pct)
        self.overall_status_label.setText(
            f"Downloading: {comp} / {total} ({pct}%) | Video: {self.total_videos} | Images: {self.total_images}"
        )

    # -------- Graceful close ---------
    def closeEvent(self, event):
        # Stop new tasks and request workers to abort
        self.stop_flag.set()
        # Give a moment for threads to exit
        t0 = time.time()
        while self.thread_pool.activeThreadCount() > 0 and time.time() - t0 < 3:
            QApplication.processEvents()
            time.sleep(0.05)
        super().closeEvent(event)


# ---------------------------
# Placeholder: album items
# ---------------------------

def get_album_items(album_url: str) -> List[Tuple[str, str, str]]:
    """
    Placeholder implementation returning a small set of public, safe test files.
    Replace this with your Erome parser to return: (file_url, filename, filetype)
    filetype should be one of: "video", "image", or "other".
    """
    # Safe test endpoints (no auth, public):
    # Deprecated in v3. Use EromeDownloaderCore.get_file_list() instead.
    return []


# ---------------------------
# Core downloader (ported from Tkinter version)
# ---------------------------

class EromeDownloaderCore:
    def __init__(self, album_url: str):
        self.album_url = album_url
        self.session = self._create_session()
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0 Safari/537.36',
            'Referer': 'https://www.erome.com/',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive'
        }
        # set as default headers on the session
        self.session.headers.update(self.headers)

    def _create_session(self) -> requests.Session:
        s = requests.Session()
        retry = Retry(total=5, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        s.mount('http://', adapter)
        s.mount('https://', adapter)
        return s

    def get_metadata(self) -> Optional[dict]:
        try:
            resp = self.session.get(self.album_url, timeout=15)
            resp.raise_for_status()
            html = resp.text
            from datetime import datetime as _dt
            metadata = {
                'url': self.album_url,
                'download_date': _dt.now().isoformat(),
                'title': '',
                'author': '',
                'views': 0,
                'likes': 0,
                'description': ''
            }
            m = re.search(r'<title>([^<]+)</title>', html)
            if m:
                metadata['title'] = m.group(1).strip()
            a = re.search(r'class="username"[^>]*>([^<]+)</a>', html)
            if a:
                metadata['author'] = a.group(1).strip()
            v = re.search(r'(\d+(?:,\d+)*)\s*(?:views|vues)', html, re.IGNORECASE)
            if v:
                metadata['views'] = int(v.group(1).replace(',', ''))
            l = re.search(r'(\d+)\s*(?:likes|j\'aime)', html, re.IGNORECASE)
            if l:
                metadata['likes'] = int(l.group(1))
            return metadata
        except Exception:
            return None

    def get_file_list(self) -> Tuple[List[str], List[str]]:
        resp = self.session.get(self.album_url, timeout=15)
        resp.raise_for_status()
        html = resp.text
        videos: List[str] = []
        images: List[str] = []

        # Videos from any vXX subdomain
        video_pattern = r'(https://v\d+\.erome\.com/[^\s"\'<>]+\.(?:mp4|webm|mkv))'
        for m in re.finditer(video_pattern, html, re.IGNORECASE):
            url = m.group(1)
            if url not in videos:
                videos.append(url)

        # Images from any sXX subdomain, skip thumbs
        img_pattern = r'(https://s\d+\.erome\.com/[^\s"\'<>]+\.(?:jpg|jpeg|png|gif|webp))'
        for m in re.finditer(img_pattern, html, re.IGNORECASE):
            url = m.group(1)
            if 'thumbs' in url:
                continue
            if url not in images:
                images.append(url)

        return videos, images

# ---------------------------
# Entrypoint
# ---------------------------

def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
