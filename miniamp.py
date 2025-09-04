import sys
import os
import random
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Slot, QTimer
from PySide6.QtGui import QAction, QKeySequence, QPainter, QColor, QPixmap, QIcon
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QFileDialog, QSlider, QLabel, QStyle, QMenuBar, QListWidget,
    QListWidgetItem, QMessageBox, QAbstractItemView, QCheckBox, QFrame, QSizePolicy
)
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

# (valfritt) mutagen för längd/metadata
try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None

# ---- Spektrumanalys (kräver ffmpeg installerat) ----
import numpy as np
try:
    from pydub import AudioSegment
    PydubAvailable = True
except Exception:
    PydubAvailable = False


# ---------------- Equalizer UI ----------------
class EqualizerWidget(QWidget):
    def __init__(self, bands=20, parent=None):
        super().__init__(parent)
        self.bands = bands
        self.levels = [10 for _ in range(bands)]
        self.setMinimumHeight(100)
        self._fallback_timer = QTimer(self)
        self._fallback_timer.timeout.connect(self._animate_fallback)
        self._fallback_timer.start(120)  # används bara om inga set_levels()-anrop kommer

    def set_levels(self, levels):
        # levels: iterable [0..1] per band
        if not levels:
            return
        # mjuk smoothing
        if len(self.levels) != len(levels):
            self.levels = [0]*len(levels)
        alpha = 0.4
        for i, v in enumerate(levels):
            v = float(max(0.0, min(1.0, v)))
            self.levels[i] = (1 - alpha) * self.levels[i] + alpha * (v * 100.0)
        self.update()

    def _animate_fallback(self):
        # om ingen ljuddata, gör en diskret animation så det inte är helt stilla
        self.levels = [max(0, min(100, l + np.random.randint(-8, 8))) for l in self.levels]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        w = self.width() / max(1, len(self.levels))
        h = self.height()
        margin = max(2, int(w * 0.15))
        bar_w = max(2, int(w - margin))
        for i, lvl in enumerate(self.levels):
            bar_h = int((lvl / 100.0) * h)
            x = int(i * w + margin / 2)
            y = h - bar_h
            color = QColor(124, 92, 255)      # lila – matchar ditt tema
            painter.fillRect(x, y, bar_w, bar_h, color)


# ---------------- Audio Analyzer ----------------
class AudioAnalyzer:
    """
    Dekodar ljudfilen via pydub/ffmpeg till PCM (mono), och exponerar band-nivåer för given tid (ms).
    """
    def __init__(self, bands=20):
        self.bands = bands
        self.rate = None
        self.samples = None  # numpy float32 mono [-1, 1]
        self.window_ms = 100  # FFT-fönster (ms)
        self._hann_cache = {}

    def set_file(self, path: str):
        self.samples = None
        self.rate = None
        if not PydubAvailable or not os.path.exists(path):
            return
        try:
            seg = AudioSegment.from_file(path)  # kräver ffmpeg i systemet
            # konvertera till mono för analys
            if seg.channels > 1:
                seg = seg.set_channels(1)
            # håll vettig samplerate
            if seg.frame_rate > 44100:
                seg = seg.set_frame_rate(44100)
            self.rate = seg.frame_rate
            # till numpy float32
            raw = np.array(seg.get_array_of_samples()).astype(np.float32)
            # normera baserat på samplebred
            peak = float(1 << (8*seg.sample_width - 1))
            raw = raw / peak
            self.samples = raw
        except Exception:
            self.samples = None
            self.rate = None

    def _hann(self, n):
        key = int(n)
        if key not in self._hann_cache:
            self._hann_cache[key] = np.hanning(key).astype(np.float32)
        return self._hann_cache[key]

    def levels_at_ms(self, ms: int):
        """
        Returnerar en lista [0..1] per band för aktuell position.
        """
        if self.samples is None or self.rate is None:
            return None
        # hämta ett fönster runt tiden
        half = int(self.window_ms / 2)
        start_ms = max(0, ms - half)
        end_ms = start_ms + self.window_ms
        start_idx = int(start_ms * self.rate / 1000)
        end_idx = int(end_ms * self.rate / 1000)
        if start_idx >= len(self.samples):
            return None
        chunk = self.samples[start_idx: end_idx]
        if len(chunk) < 16:
            return None

        # FFT
        n = int(2 ** np.ceil(np.log2(len(chunk))))  # nästa tvåpotens
        window = self._hann(len(chunk))
        chunk_w = chunk[:len(window)] * window
        spec = np.fft.rfft(chunk_w, n=n)
        mag = np.abs(spec)  # magnitud
        # frekvensaxel
        freqs = np.fft.rfftfreq(n, d=1.0/self.rate)

        # Log-indelning av band (20 Hz – 20 kHz)
        fmin, fmax = 20.0, min(20000.0, self.rate/2)
        edges = np.geomspace(fmin, fmax, num=self.bands+1)
        levels = []
        eps = 1e-9
        for i in range(self.bands):
            lo, hi = edges[i], edges[i+1]
            idx = np.where((freqs >= lo) & (freqs < hi))[0]
            if idx.size == 0:
                levels.append(0.0)
            else:
                val = float(np.sqrt(np.mean(mag[idx]**2)))  # RMS i bandet
                levels.append(val)

        # normalisera logg-ish
        arr = np.array(levels, dtype=np.float32) + eps
        arr = np.log10(arr)
        # skala in i [0..1]
        arr = (arr - arr.min()) / max(1e-6, (arr.max() - arr.min()))
        # lite tonvikt på bas: multiplicera med fallande kurva
        weight = np.linspace(1.2, 0.9, num=self.bands)
        arr = np.clip(arr * weight, 0.0, 1.0)
        return arr.tolist()


# ---------------- Huvud-appen ----------------
class MiniAmp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MiniAmp – Playlists")
        self.resize(900, 600)

        # --- Player ---
        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.7)

        # runtime state
        self.current_index = -1
        self.repeat = False
        self.shuffle = False

        # --- Analyzer / EQ ---
        self.analyzer = AudioAnalyzer(bands=20)

        # --- THEME ---
        self.apply_theme()

        # --- Meny ---
        menubar = QMenuBar()
        menubar.setObjectName("MenuBar")
        file_menu = menubar.addMenu("Arkiv")
        open_action = QAction("Öppna fil…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.open_files)
        file_menu.addAction(open_action)

        open_folder_action = QAction("Lägg till mapp…", self)
        open_folder_action.triggered.connect(self.add_folder)
        file_menu.addAction(open_folder_action)

        file_menu.addSeparator()

        load_m3u_action = QAction("Öppna spellista (.m3u)…", self)
        load_m3u_action.triggered.connect(self.load_playlist_m3u)
        file_menu.addAction(load_m3u_action)

        save_m3u_action = QAction("Spara spellista som .m3u…", self)
        save_m3u_action.triggered.connect(self.save_playlist_m3u)
        file_menu.addAction(save_m3u_action)

        file_menu.addSeparator()

        quit_action = QAction("Avsluta", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # --- Header med logga ---
        header = QHBoxLayout()
        logo_path_candidates = [
            Path(__file__).with_name("ccmamp_logo.png"),
            Path(__file__).parent / "assets" / "ccmamp_logo.png",
        ]
        logo_file = next((p for p in logo_path_candidates if p.exists()), None)
        if logo_file:
            logo_lbl = QLabel()
            pm = QPixmap(str(logo_file))
            pm_scaled = pm.scaledToHeight(56, Qt.SmoothTransformation)
            logo_lbl.setPixmap(pm_scaled)
            logo_lbl.setContentsMargins(0, 0, 10, 0)
            header.addWidget(logo_lbl)
            self.setWindowIcon(QIcon(pm))
            self.setWindowTitle("CCMAMP – CodeCraftsMan Amp")
        else:
            title = QLabel("CCMAMP")
            title.setObjectName("Title")
            header.addWidget(title)
        header.addStretch()

        # --- Kontroller ---
        btn_prev = QPushButton(self.style().standardIcon(QStyle.SP_MediaSkipBackward), "")
        btn_prev.setObjectName("IconButton")
        btn_play = QPushButton(self.style().standardIcon(QStyle.SP_MediaPlay), "")
        btn_play.setObjectName("IconButtonPrimary")
        btn_pause = QPushButton(self.style().standardIcon(QStyle.SP_MediaPause), "")
        btn_pause.setObjectName("IconButton")
        btn_stop = QPushButton(self.style().standardIcon(QStyle.SP_MediaStop), "")
        btn_stop.setObjectName("IconButton")
        btn_next = QPushButton(self.style().standardIcon(QStyle.SP_MediaSkipForward), "")
        btn_next.setObjectName("IconButton")

        btn_prev.clicked.connect(self.prev_track)
        btn_play.clicked.connect(self.player.play)
        btn_pause.clicked.connect(self.player.pause)
        btn_stop.clicked.connect(self.player.stop)
        btn_next.clicked.connect(self.next_track)

        # tidsreglage
        self.pos_slider = QSlider(Qt.Horizontal); self.pos_slider.setRange(0, 0)
        self.pos_slider.setObjectName("Progress")
        self.lbl_time = QLabel("00:00 / 00:00")
        self.lbl_time.setObjectName("Caption")
        self.lbl_file = QLabel("Ingen fil vald")
        self.lbl_file.setObjectName("NowPlaying")

        # volym
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100); self.vol_slider.setValue(70)
        self.vol_slider.setObjectName("Volume")
        self.vol_slider.valueChanged.connect(lambda v: self.audio.setVolume(v/100))

        # shuffle/repeat
        self.chk_shuffle = QCheckBox("Shuffle")
        self.chk_repeat = QCheckBox("Repeat")
        self.chk_shuffle.setObjectName("Toggle")
        self.chk_repeat.setObjectName("Toggle")
        self.chk_shuffle.stateChanged.connect(lambda s: setattr(self, 'shuffle', bool(s)))
        self.chk_repeat.stateChanged.connect(lambda s: setattr(self, 'repeat', bool(s)))

        # --- Playlist view ---
        self.playlist = QListWidget()
        self.playlist.setObjectName("Playlist")
        self.playlist.setSelectionMode(QAbstractItemView.SingleSelection)
        self.playlist.setDragEnabled(True)
        self.playlist.setAcceptDrops(True)
        self.playlist.viewport().setAcceptDrops(True)
        self.playlist.setDragDropMode(QAbstractItemView.InternalMove)
        self.playlist.doubleClicked.connect(self.on_item_double_clicked)
        self.setAcceptDrops(True)

        # knappar för listan
        btn_add = QPushButton("+ Lägg till filer")
        btn_add.setObjectName("Btn")
        btn_add.clicked.connect(self.open_files)
        btn_remove = QPushButton("− Ta bort")
        btn_remove.setObjectName("Btn")
        btn_remove.clicked.connect(self.remove_selected)
        btn_clear = QPushButton("Rensa")
        btn_clear.setObjectName("BtnGhost")
        btn_clear.clicked.connect(self.clear_playlist)

        # --- Layout ---
        v = QVBoxLayout(self)
        v.setMenuBar(menubar)

        v.addLayout(header)

        # Now playing
        v.addWidget(self.lbl_file)

        # kontroller rad 1
        row1 = QHBoxLayout()
        row1.addWidget(btn_prev)
        row1.addWidget(btn_play)
        row1.addWidget(btn_pause)
        row1.addWidget(btn_stop)
        row1.addWidget(btn_next)
        row1.addStretch()
        lab_vol = QLabel("Volym")
        lab_vol.setObjectName("Caption")
        row1.addWidget(lab_vol)
        row1.addWidget(self.vol_slider)
        v.addLayout(row1)

        v.addWidget(self.pos_slider)

        row2 = QHBoxLayout()
        row2.addWidget(self.lbl_time)
        row2.addStretch()
        row2.addWidget(self.chk_shuffle)
        row2.addWidget(self.chk_repeat)
        v.addLayout(row2)

        # --- Equalizer (följ musiken) ---
        self.equalizer = EqualizerWidget(bands=self.analyzer.bands)
        v.addWidget(self.equalizer)

        # uppdaterings-timer för EQ (synkad mot player.position)
        self.eq_timer = QTimer(self)
        self.eq_timer.setInterval(50)
        self.eq_timer.timeout.connect(self._update_equalizer)
        self.eq_timer.start()

        # Separator
        sep = QFrame(); sep.setFrameShape(QFrame.HLine); sep.setObjectName("Separator")
        v.addWidget(sep)

        # Playlist + knappar
        row3 = QHBoxLayout()
        row3.addWidget(self.playlist, 3)
        col_buttons = QVBoxLayout()
        col_buttons.addWidget(btn_add)
        col_buttons.addWidget(btn_remove)
        col_buttons.addWidget(btn_clear)
        col_buttons.addStretch()
        row3.addLayout(col_buttons, 1)
        v.addLayout(row3)

        # Footer / credit
        footer = QLabel("Made by CodeCraftsMan")
        footer.setAlignment(Qt.AlignCenter)
        footer.setObjectName("Footer")
        footer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        v.addWidget(footer)

        # --- Signals ---
        self.player.positionChanged.connect(self.on_position_changed)
        self.player.durationChanged.connect(self.on_duration_changed)
        self.player.mediaStatusChanged.connect(self.on_media_status_changed)
        self.player.errorOccurred.connect(self.on_error)
        self.pos_slider.sliderMoved.connect(self.on_seek)

        # kortkommandon
        QAction("Play/Pause", self, shortcut=QKeySequence("Space"), triggered=self.toggle_play_pause)
        QAction("Next", self, shortcut=QKeySequence("Ctrl+Right"), triggered=self.next_track)
        QAction("Prev", self, shortcut=QKeySequence("Ctrl+Left"), triggered=self.prev_track)

    # --- Theme / Stylesheet ---
    def apply_theme(self):
        self.setStyleSheet(
            """
            QWidget { background: #0f1115; color: #e6e6e6; font-size: 14px; }
            #MenuBar { background: #12151c; border: 1px solid #202433; }

            #Title { font-size: 40px; font-weight: 900; letter-spacing: 1px; color: #e6e6e6; }
            #NowPlaying { font-weight: 600; padding: 6px 10px; border-radius: 10px;
                          background: #151925; border: 1px solid #1f2433; }

            #Caption { color: #98a1b3; }

            #Progress::groove:horizontal { height: 8px; background: #1a2030; border-radius: 6px; }
            #Progress::handle:horizontal { width: 18px; height: 18px; margin: -6px 0; border-radius: 9px;
                                           background: #7c5cff; border: 2px solid #2b2f40; }

            #Volume::groove:horizontal { height: 6px; background: #1a2030; border-radius: 6px; }
            #Volume::handle:horizontal { width: 16px; height: 16px; margin: -5px 0; border-radius: 8px;
                                         background: #2dd4bf; border: 2px solid #2b2f40; }

            #Toggle { color: #cbd5e1; }
            QCheckBox::indicator { width: 18px; height: 18px; }
            QCheckBox::indicator:unchecked { border: 2px solid #343a4f; background: #141827; border-radius: 5px; }
            QCheckBox::indicator:checked { border: 2px solid #4f46e5; background: #4f46e5; border-radius: 5px; }

            #Playlist { background: #0f1320; border: 1px solid #1e2436; border-radius: 12px; padding: 6px; }
            #Playlist::item { padding: 8px 10px; border-radius: 8px; }
            #Playlist::item:selected { background: #1f2a44; color: #e6e6e6; }
            #Playlist::item:hover { background: #182136; }

            #Separator { color: #202636; background: #202636; max-height: 1px; }

            QPushButton#IconButton, QPushButton#IconButtonPrimary {
                width: 42px; height: 42px; border-radius: 12px; border: 1px solid #23283a; 
                background: #151a28; padding: 6px; }
            QPushButton#IconButton:hover { background: #1a2133; }
            QPushButton#IconButton:pressed { background: #0f1422; }

            QPushButton#IconButtonPrimary { border: 1px solid #3b2dd6; background: #4f46e5; }
            QPushButton#IconButtonPrimary:hover { background: #5b54ea; }
            QPushButton#IconButtonPrimary:pressed { background: #413ad2; }

            QPushButton#Btn { border-radius: 10px; padding: 8px 14px; border: 1px solid #243049; background: #131a2a; }
            QPushButton#Btn:hover { background: #182137; }
            QPushButton#Btn:pressed { background: #0f1626; }

            QPushButton#BtnGhost { border-radius: 10px; padding: 8px 14px; border: 1px dashed #2a3551; background: transparent; color: #9aa3b2; }
            QPushButton#BtnGhost:hover { background: rgba(255,255,255,0.04); }

            #Footer { color: #76819a; margin-top: 10px; padding: 10px; border-top: 1px solid #1a2130; }
            """
        )

    # --- EQ uppdatering ---
    def _update_equalizer(self):
        if self.player.playbackState() != QMediaPlayer.PlayingState:
            return
        pos_ms = self.player.position()
        levels = self.analyzer.levels_at_ms(pos_ms)
        if levels is not None:
            self.equalizer.set_levels(levels)

    # --- Drag & drop från OS till listan/fönstret ---
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        urls = [u.toLocalFile() for u in event.mimeData().urls()]
        self.add_files(urls)

    # --- Playlist-hjälpare ---
    def add_files(self, paths):
        supported = {'.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac'}
        new_items = []
        for p in paths:
            if not p:
                continue
            ext = Path(p).suffix.lower()
            if ext in supported and Path(p).exists():
                dur_ms = self.probe_duration_ms(p)
                name = Path(p).stem
                text = f"{name} — {self.fmt_duration(dur_ms)}" if dur_ms else name
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, str(Path(p).resolve()))
                if dur_ms is not None:
                    item.setData(Qt.UserRole + 1, dur_ms)
                self.playlist.addItem(item)
                new_items.append(item)
        if new_items and self.current_index == -1:
            self.current_index = self.playlist.row(new_items[0])
            self.play_item(self.current_index)

    @Slot()
    def open_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Välj ljudfiler",
            filter="Ljudfiler (*.mp3 *.wav *.m4a *.aac *.ogg *.flac)"
        )
        if files:
            self.add_files(files)

    @Slot()
    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Välj mapp med ljudfiler")
        if folder:
            all_files = []
            for root, _, files in os.walk(folder):
                for f in files:
                    all_files.append(os.path.join(root, f))
            self.add_files(all_files)

    def remove_selected(self):
        row = self.playlist.currentRow()
        if row < 0:
            return
        item_current_playing = (row == self.current_index)
        self.playlist.takeItem(row)
        if row < self.current_index:
            self.current_index -= 1
        elif item_current_playing:
            self.current_index = -1
            if self.playlist.count():
                self.current_index = min(row, self.playlist.count() - 1)
                self.play_item(self.current_index)
            else:
                self.player.stop()
                self.lbl_file.setText("Ingen fil vald")

    def clear_playlist(self):
        self.playlist.clear()
        self.current_index = -1
        self.player.stop()
        self.lbl_file.setText("Ingen fil vald")

    def on_item_double_clicked(self):
        row = self.playlist.currentRow()
        if row >= 0:
            self.current_index = row
            self.play_item(row)

    # --- Spelning ---
    def play_item(self, row):
        item = self.playlist.item(row)
        if not item:
            return
        path = item.data(Qt.UserRole)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.lbl_file.setText(Path(path).stem)
        self.player.play()
        self.playlist.setCurrentRow(row)
        # ladda analysdata för equalizern
        self.analyzer.set_file(path)

    def next_track(self):
        n = self.playlist.count()
        if n == 0:
            return
        if self.shuffle:
            next_row = random.randrange(n)
        else:
            next_row = 0 if self.current_index < 0 else (self.current_index + 1) % n
        self.current_index = next_row
        self.play_item(next_row)

    def prev_track(self):
        n = self.playlist.count()
        if n == 0:
            return
        if self.shuffle:
            prev_row = random.randrange(n)
        else:
            prev_row = 0 if self.current_index < 0 else (self.current_index - 1 + n) % n
        self.current_index = prev_row
        self.play_item(prev_row)

    def toggle_play_pause(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    # --- Tids/position ---
    @Slot(int)
    def on_seek(self, pos):
        self.player.setPosition(pos)

    @Slot(int)
    def on_position_changed(self, pos):
        self.pos_slider.setValue(pos)
        self.update_time_label()

    @Slot(int)
    def on_duration_changed(self, dur):
        self.pos_slider.setRange(0, dur)
        self.update_time_label()

    def update_time_label(self):
        dur = self.player.duration()
        pos = self.player.position()
        self.lbl_time.setText(f"{self.fmt_duration(pos)} / {self.fmt_duration(dur)}")

    # --- Autonästa ---
    def on_media_status_changed(self, status):
        from PySide6.QtMultimedia import QMediaPlayer as _P
        if status == _P.EndOfMedia:
            if self.repeat and not self.shuffle:
                self.play_item(self.current_index if self.current_index >= 0 else 0)
            else:
                self.next_track()

    def on_error(self, error, *args):
        if error:
            QMessageBox.warning(self, "Fel vid uppspelning", self.player.errorString())

    # --- Hjälpmetoder för tid / metadata ---
    def fmt_duration(self, ms):
        if ms is None or ms <= 0:
            return "--:--"
        s = int(round(ms/1000))
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"

    def probe_duration_ms(self, path):
        if MutagenFile is None:
            return None
        try:
            audio = MutagenFile(path)
            if audio and audio.info and getattr(audio.info, 'length', None):
                return int(audio.info.length * 1000)
        except Exception:
            return None
        return None

    # --- M3U import/export ---
    def save_playlist_m3u(self):
        if self.playlist.count() == 0:
            QMessageBox.information(self, "Spara spellista", "Spellistan är tom.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Spara spellista", filter="M3U files (*.m3u *.m3u8)")
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write("#EXTM3U\n")
                for i in range(self.playlist.count()):
                    item = self.playlist.item(i)
                    file_path = item.data(Qt.UserRole)
                    dur_ms = item.data(Qt.UserRole + 1)
                    if dur_ms is None:
                        dur_ms = self.probe_duration_ms(file_path)
                    dur_s = int(round(dur_ms/1000)) if dur_ms else -1
                    title = Path(file_path).stem
                    f.write(f"#EXTINF:{dur_s},{title}\n")
                    f.write(file_path + "\n")
        except Exception as e:
            QMessageBox.critical(self, "Spara misslyckades", str(e))

    def load_playlist_m3u(self):
        path, _ = QFileDialog.getOpenFileName(self, "Öppna spellista", filter="M3U files (*.m3u *.m3u8)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                pending_duration = None
                for raw in f:
                    line = raw.strip()
                    if not line:
                        continue
                    if line.startswith('#EXTINF:'):
                        try:
                            rest = line[8:]  # "<seconds>,<title>"
                            dur_part, _sep, _title = rest.partition(',')
                            pending_duration = int(float(dur_part)) * 1000 if dur_part else None
                        except Exception:
                            pending_duration = None
                        continue
                    if line.startswith('#'):
                        continue
                    p = line
                    if not os.path.isabs(p):
                        p = os.path.abspath(os.path.join(os.path.dirname(path), p))
                    if os.path.exists(p):
                        dur_ms = pending_duration if pending_duration else self.probe_duration_ms(p)
                        name = Path(p).stem
                        text = f"{name} — {self.fmt_duration(dur_ms)}" if dur_ms else name
                        item = QListWidgetItem(text)
                        item.setData(Qt.UserRole, str(Path(p).resolve()))
                        if dur_ms is not None:
                            item.setData(Qt.UserRole + 1, dur_ms)
                        self.playlist.addItem(item)
                    pending_duration = None
        except Exception as e:
            QMessageBox.critical(self, "Öppna misslyckades", str(e))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MiniAmp()
    w.show()
    sys.exit(app.exec())







