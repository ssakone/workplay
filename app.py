#!/usr/bin/env python3
"""
WorkPlay — widget frameless toujours au premier plan + tray icon.

Lecteur audio minimaliste conçu pour tourner en arrière-plan sur macOS :
- fenêtre sans cadre, translucide, coins arrondis
- déplaçable à la souris, épinglée au-dessus des autres fenêtres
- icône dans la barre de menus : afficher/masquer, ajouter des URLs YouTube,
  choisir le dossier musique, régler le premier plan, quitter
- les URLs ajoutées sont téléchargées (yt-dlp -> mp3) et rejoignent la playlist
  automatiquement, sans redémarrer le lecteur

Raccourcis : Espace = lecture/pause · ← → = piste précédente/suivante
             ↑ ↓ = volume · L = playlist · ⌘⇧T = premier plan · Échap = réduire
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections import deque
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, QUrl, QSettings, QPoint, QProcess, QProcessEnvironment, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QIcon, QPainter, QPixmap, QTextCursor,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (
    QApplication, QWidget, QFrame, QLabel, QPushButton, QSlider,
    QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QDialog,
    QPlainTextEdit, QProgressBar, QMenu, QSystemTrayIcon,
    QGraphicsDropShadowEffect, QFileDialog, QCheckBox,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Dossier musical par défaut. Modifiable depuis la barre de menus ;
# l'application le choisit elle-même, aucun chemin n'est codé en dur.
MUSIC_DIR_DEFAULT = Path.home() / "Music" / "WorkPlay"

# Ordre d'affichage souhaité. Les fichiers listés ici passent en tête, dans cet
# ordre ; tout le reste suit par ordre alphabétique. Vide par défaut :
# l'application est générique.
PREFERRED_ORDER: list[str] = []

# Exemple — décommente et adapte pour épingler tes morceaux en tête :
# PREFERRED_ORDER = ["Mon morceau préféré", "Un autre titre"]

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".webm"}

# Emplacements habituels des binaires Homebrew, ajoutés au PATH du sous-processus
# (le PATH hérité d'une app lancée depuis le Finder est minimal).
EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]

YTDLP_ARGS = [
    "-x",
    "--audio-format", "mp3",
    "--audio-quality", "0",
    "--embed-thumbnail",
    "--embed-metadata",
    "--add-metadata",
    "--newline",              # une ligne par événement -> progression analysable
    "--ignore-errors",        # une URL invalide ne doit pas tuer le lot
    "--no-overwrites",
    "-o", "%(title)s.%(ext)s",
]

STYLE = """
#Card {
    background-color: rgba(14, 14, 18, 150);
    border: 1px solid rgba(255, 255, 255, 58);
    border-radius: 16px;
}
#Title {
    color: #f2f2f7;
    font-size: 13px;
    font-weight: 600;
}
#Time, #Artist {
    color: rgba(235, 235, 245, 140);
    font-size: 10px;
}
QPushButton#Ctrl {
    background-color: transparent;
    border: none;
    color: #f2f2f7;
    font-size: 15px;
    padding: 0px;
}
QPushButton#Ctrl:hover {
    background-color: rgba(255, 255, 255, 26);
    border-radius: 14px;
}
QPushButton#Play {
    background-color: rgba(255, 255, 255, 235);
    border: none;
    border-radius: 18px;
    color: #111114;
    font-size: 15px;
    font-weight: 700;
}
QPushButton#Play:hover {
    background-color: #ffffff;
}
QPushButton#Mini {
    background-color: transparent;
    border: none;
    color: rgba(235, 235, 245, 150);
    font-size: 12px;
}
QPushButton#Mini:hover {
    color: #f2f2f7;
}
QPushButton#Mini:checked {
    color: #ff8a4c;
}
QSlider::groove:horizontal {
    height: 4px;
    background: rgba(255, 255, 255, 34);
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: #ff8a4c;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #ffffff;
    width: 10px;
    height: 10px;
    margin: -4px 0;
    border-radius: 5px;
}
QSlider#Vol::sub-page:horizontal {
    background: rgba(255, 255, 255, 150);
}
QListWidget {
    background: transparent;
    border: none;
    color: rgba(235, 235, 245, 190);
    font-size: 11px;
    outline: none;
}
QListWidget::item {
    padding: 5px 8px;
    border-radius: 7px;
}
QListWidget::item:hover {
    background: rgba(255, 255, 255, 20);
}
QListWidget::item:selected {
    background: rgba(255, 138, 76, 55);
    color: #ffffff;
}
QMenu {
    background-color: #1c1c22;
    color: #f2f2f7;
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 10px;
    padding: 5px;
    font-size: 12px;
}
QMenu::item {
    padding: 6px 24px 6px 22px;
    border-radius: 6px;
}
QMenu::item:selected {
    background-color: rgba(255, 138, 76, 160);
}
QMenu::separator {
    height: 1px;
    background: rgba(255, 255, 255, 34);
    margin: 4px 8px;
}
QMenu::indicator:checked {
    color: #ff8a4c;
}
QDialog {
    background-color: #16161b;
}
#DlgTitle {
    color: #f2f2f7;
    font-size: 14px;
    font-weight: 600;
}
#DlgHint {
    color: rgba(235, 235, 245, 130);
    font-size: 11px;
}
QPlainTextEdit {
    background-color: rgba(255, 255, 255, 16);
    border: 1px solid rgba(255, 255, 255, 40);
    border-radius: 9px;
    color: #f2f2f7;
    font-size: 12px;
    padding: 6px;
}
#Log {
    background-color: rgba(0, 0, 0, 90);
    color: rgba(235, 235, 245, 175);
    font-family: Menlo, monospace;
    font-size: 10px;
}
QProgressBar {
    background-color: rgba(255, 255, 255, 26);
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background-color: #ff8a4c;
    border-radius: 4px;
}
QPushButton#Primary {
    background-color: #ff8a4c;
    border: none;
    border-radius: 9px;
    color: #16161b;
    font-size: 12px;
    font-weight: 600;
    padding: 8px 18px;
}
QPushButton#Primary:hover {
    background-color: #ffa06b;
}
QPushButton#Primary:disabled {
    background-color: rgba(255, 255, 255, 40);
    color: rgba(0, 0, 0, 110);
}
QPushButton#Ghost {
    background-color: transparent;
    border: 1px solid rgba(255, 255, 255, 50);
    border-radius: 9px;
    color: rgba(235, 235, 245, 190);
    font-size: 12px;
    padding: 8px 16px;
}
QPushButton#Ghost:hover {
    border-color: rgba(255, 255, 255, 110);
    color: #f2f2f7;
}
"""

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def format_time(ms: int) -> str:
    """Millisecondes -> m:ss."""
    if ms is None or ms < 0:
        ms = 0
    total = ms // 1000
    return f"{total // 60}:{total % 60:02d}"


def natural_key(name: str):
    """Clé de tri : titres épinglés en tête, puis ordre alphabétique."""
    stem = Path(name).stem
    try:
        return (0, PREFERRED_ORDER.index(stem), stem.lower())
    except ValueError:
        return (1, 999, stem.lower())


def scan_tracks(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir()
             if p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")]
    return sorted(files, key=lambda p: natural_key(p.name))


def find_ytdlp() -> str | None:
    """Localise yt-dlp, en tenant compte des PATH minimaux des apps GUI."""
    for base in EXTRA_PATHS:
        cand = Path(base) / "yt-dlp"
        if cand.exists():
            return str(cand)
    return shutil.which("yt-dlp")


def process_env() -> QProcessEnvironment:
    """Environnement du sous-processus avec un PATH élargi (ffmpeg inclus)."""
    env = QProcessEnvironment.systemEnvironment()
    current = env.value("PATH", "")
    parts = [p for p in EXTRA_PATHS if p not in current.split(os.pathsep)]
    env.insert("PATH", os.pathsep.join(parts + [current]).strip(os.pathsep))
    return env


def make_icon() -> QIcon:
    """Icône du tray : pastille orange avec une note."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#ff8a4c"))
    p.drawEllipse(3, 3, 58, 58)
    p.setPen(QColor("#16161b"))
    f = QFont()
    f.setPointSize(30)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "♪")
    p.end()
    return QIcon(pm)


# --------------------------------------------------------------------------- #
# Dialogue « Ajouter des URLs »
# --------------------------------------------------------------------------- #

class DownloadDialog(QDialog):
    """Colle une ou plusieurs URLs YouTube ; elles rejoignent la playlist.

    Les téléchargements s'enchaînent en file : on peut en ajouter d'autres
    pendant qu'un lot tourne.
    """

    finished_all = Signal()

    def __init__(self, music_dir: Path, download_dir: Path | None = None,
                 parent=None):
        super().__init__(parent)
        self.music_dir = music_dir
        # Dossier d'atterrissage des téléchargements. S'il diffère de la
        # bibliothèque, le morceau est déplacé après coup (voir Player).
        self.download_dir = download_dir or music_dir
        self.queue: deque[str] = deque()
        self.done = 0
        self.failed = 0
        self.total = 0
        self.current: str | None = None

        self.setWindowTitle("Ajouter des morceaux")
        self.setMinimumWidth(520)
        self.setStyleSheet(STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("Ajouter des morceaux")
        title.setObjectName("DlgTitle")
        root.addWidget(title)

        hint = QLabel(
            "Colle une ou plusieurs URLs YouTube — une par ligne.\n"
            "Chaque morceau est converti en MP3 et ajouté à la playlist."
        )
        hint.setObjectName("DlgHint")
        root.addWidget(hint)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(
            "https://www.youtube.com/watch?v=...\n"
            "https://www.youtube.com/watch?v=..."
        )
        self.input.setFixedHeight(96)
        root.addWidget(self.input)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        root.addWidget(self.bar)

        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(112)
        root.addWidget(self.log)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_folder = QPushButton("Ouvrir le dossier")
        self.btn_folder.setObjectName("Ghost")
        self.btn_folder.clicked.connect(self._open_folder)
        buttons.addWidget(self.btn_folder)

        self.btn_add = QPushButton("Ajouter à la playlist")
        self.btn_add.setObjectName("Primary")
        self.btn_add.clicked.connect(self._start_batch)
        buttons.addWidget(self.btn_add, 0, Qt.AlignRight)

        buttons.addStretch(1)
        self.btn_close = QPushButton("Fermer")
        self.btn_close.setObjectName("Ghost")
        self.btn_close.clicked.connect(self.hide)
        buttons.addWidget(self.btn_close)
        root.addLayout(buttons)

        # --- Sous-processus yt-dlp ------------------------------------------
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setProcessEnvironment(process_env())
        self.proc.setWorkingDirectory(str(self.music_dir))
        self.proc.readyReadStandardOutput.connect(self._on_output)
        self.proc.finished.connect(self._on_proc_finished)

        if find_ytdlp() is None:
            self._log("yt-dlp introuvable — installe-le : brew install yt-dlp ffmpeg")

    # ------------------------------------------------------------ journal --
    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)
        self.log.moveCursor(QTextCursor.End)

    # ---------------------------------------------------------- lancement --
    def _start_batch(self) -> None:
        raw = self.input.toPlainText()
        urls = [u.strip() for u in raw.splitlines() if URL_RE.match(u.strip())]
        if not urls:
            self._log("Aucune URL valide (elles doivent commencer par http).")
            return

        ytdlp = find_ytdlp()
        if ytdlp is None:
            self._log("Impossible de télécharger : yt-dlp absent.")
            return

        self.queue.extend(urls)
        self.total += len(urls)
        self.input.clear()
        self.btn_add.setEnabled(False)
        self._log(f"— {len(urls)} URL(s) mise(s) en file —")
        self._next()

    def _next(self) -> None:
        if self.proc.state() != QProcess.NotRunning:
            return  # un lot tourne déjà, _on_proc_finished enchaînera
        if not self.queue:
            self.btn_add.setEnabled(True)
            self.bar.setValue(100 if self.total else 0)
            self._log(
                f"Terminé : {self.done} morceau(x) ajouté(s)"
                + (f", {self.failed} échec(s)." if self.failed else ".")
            )
            self.finished_all.emit()
            return

        self.current = self.queue.popleft()
        self.bar.setValue(0)
        self._log(f"↓ {self.current}")
        ytdlp = find_ytdlp()
        assert ytdlp is not None
        self.proc.start(ytdlp, YTDLP_ARGS + [self.current])

    def _on_output(self) -> None:
        chunk = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            m = PROGRESS_RE.search(line)
            if m:
                self.bar.setValue(int(float(m.group(1))))
            if line.startswith(("[download]", "[ExtractAudio]", "ERROR", "WARNING")):
                self._log(line[:150])

    def _on_proc_finished(self, code: int, _status) -> None:
        if code == 0:
            self.done += 1
        else:
            self.failed += 1
            self._log(f"Échec sur cette URL (code {code}).")
        # Laisse la file repartir, et signale les nouveaux morceaux.
        QTimer.singleShot(120, self._next)
        self.finished_all.emit()

    def _open_folder(self) -> None:
        os.system(f'open "{self.download_dir}"')

    def add_url(self, url: str) -> None:
        """Pré-remplit le champ avec une URL (appelé depuis le menu tray)."""
        current = self.input.toPlainText().strip()
        self.input.setPlainText(f"{current}\n{url}".strip() if current else url)
        self.input.moveCursor(QTextCursor.End)

    def closeEvent(self, e) -> None:
        """Fermer la fenêtre masque le dialogue sans interrompre un téléchargement."""
        if self.proc.state() != QProcess.NotRunning:
            e.ignore()
            self.hide()
            return
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
# Dialogue « Réglages »
# --------------------------------------------------------------------------- #

class SettingsDialog(QDialog):
    """Réglages : dossier de la bibliothèque et dossier de téléchargement.

    Les chemins se choisissent par le sélecteur macOS plutôt qu'en les tapant :
    c'est plus sûr et cela évite les chemins invalides.
    """

    applied = Signal()

    def __init__(self, music_dir: Path, download_dir: Path, parent=None):
        super().__init__(parent)
        self.music_dir = Path(music_dir)
        self.download_dir = Path(download_dir)
        self.same_as_library = self.download_dir == self.music_dir

        self.setWindowTitle("Réglages WorkPlay")
        self.setMinimumWidth(580)
        self.setStyleSheet(STYLE)
        # Le dialogue reste au-dessus : c'est une action volontaire de
        # l'utilisateur, contrairement au widget qui ne s'impose jamais.
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(14)

        title = QLabel("Réglages")
        title.setObjectName("DlgTitle")
        root.addWidget(title)

        # --- Dossier de la bibliothèque -------------------------------------
        root.addWidget(self._section("Bibliothèque",
                                     "Où WorkPlay cherche vos fichiers audio."))
        self.lbl_lib = self._path_label(self.music_dir)
        root.addWidget(self.lbl_lib)
        root.addLayout(self._buttons(
            ("Choisir un dossier…", self._choose_library),
            ("Ouvrir", lambda: os.system(f'open "{self.music_dir}"')),
        ))

        # --- Dossier de téléchargement --------------------------------------
        root.addWidget(self._section(
            "Téléchargement",
            "Où arrivent les morceaux récupérés depuis une URL.",
        ))
        self.chk_same = QCheckBox(
            "Télécharger dans le dossier de la bibliothèque"
        )
        self.chk_same.setChecked(self.same_as_library)
        self.chk_same.stateChanged.connect(self._on_same_toggled)
        root.addWidget(self.chk_same)

        self.lbl_dl = self._path_label(self.download_dir)
        root.addWidget(self.lbl_dl)
        self.btn_dl_row = self._buttons(
            ("Choisir un dossier…", self._choose_download),
            ("Ouvrir", lambda: os.system(f'open "{self.download_dir}"')),
        )
        root.addLayout(self.btn_dl_row)

        self._sync_enabled()

        # --- Validation ------------------------------------------------------
        footer = QHBoxLayout()
        footer.addStretch(1)
        btn_cancel = QPushButton("Annuler")
        btn_cancel.setObjectName("Ghost")
        btn_cancel.clicked.connect(self.reject)
        footer.addWidget(btn_cancel)

        btn_ok = QPushButton("Enregistrer")
        btn_ok.setObjectName("Primary")
        btn_ok.clicked.connect(self._accept)
        footer.addWidget(btn_ok)
        root.addLayout(footer)

    # ------------------------------------------------------------- helpers --
    @staticmethod
    def _section(text: str, hint: str) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        t = QLabel(text)
        t.setObjectName("Title")
        h = QLabel(hint)
        h.setObjectName("DlgHint")
        h.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(h)
        return box

    @staticmethod
    def _path_label(path: Path) -> QLabel:
        lbl = QLabel(str(path))
        lbl.setObjectName("DlgHint")
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setWordWrap(True)
        return lbl

    @staticmethod
    def _buttons(*specs) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        for text, slot in specs:
            b = QPushButton(text)
            b.setObjectName("Ghost")
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        return row

    # --------------------------------------------------------------- choix --
    def _choose_library(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier de la bibliothèque", str(self.music_dir)
        )
        if chosen:
            self.music_dir = Path(chosen)
            self.lbl_lib.setText(str(self.music_dir))
            if self.chk_same.isChecked():
                self.download_dir = self.music_dir
                self.lbl_dl.setText(str(self.download_dir))

    def _choose_download(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier de téléchargement", str(self.download_dir)
        )
        if chosen:
            self.download_dir = Path(chosen)
            self.lbl_dl.setText(str(self.download_dir))

    def _on_same_toggled(self) -> None:
        self.same_as_library = self.chk_same.isChecked()
        if self.same_as_library:
            self.download_dir = self.music_dir
            self.lbl_dl.setText(str(self.download_dir))
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        # Le second dossier n'a de sens que s'il diffère du premier.
        for i in range(self.btn_dl_row.count()):
            w = self.btn_dl_row.itemAt(i).widget()
            if w is not None:
                w.setEnabled(not self.same_as_library)
        self.lbl_dl.setEnabled(not self.same_as_library)

    def _accept(self) -> None:
        self.applied.emit()
        self.accept()


# --------------------------------------------------------------------------- #
# Widget principal
# --------------------------------------------------------------------------- #

class Player(QWidget):
    # Marge réservée à l'ombre portée (le halo doit pouvoir se dessiner
    # hors du cadre de la carte).
    SHADOW_PAD = 10
    CONTENT_H = 132
    COMPACT_H = CONTENT_H + 2 * SHADOW_PAD
    ITEM_H = 27

    def __init__(self, music_dir: Path, download_dir: Path | None = None):
        super().__init__()
        self.music_dir = music_dir
        # Dossier d'atterrissage des téléchargements : par défaut le même que
        # la bibliothèque, pour que le morceau apparaisse aussitôt dans la liste.
        self.download_dir = download_dir or music_dir
        self.tracks: list[Path] = scan_tracks(music_dir)
        self.index = -1
        self._drag_offset: QPoint | None = None
        self._seeking = False
        self.expanded = False

        self.settings = QSettings("com.m5max", "WorkPlay")
        self.always_on_top = self.settings.value("always_on_top", True, type=bool)

        # ---- Fenêtre : sans cadre, translucide, au premier plan -------------
        self.setWindowFlags(self._flags(self.always_on_top))
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # Sécurité anti-vol-de-focus : afficher cette fenêtre ne l'active
        # jamais, et ne retire donc pas le clavier de l'application au premier
        # plan. C'est une garantie, pas un confort : une fenêtre qui vole le
        # focus pendant une frappe peut faire des dégâts.
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWindowTitle("WorkPlay")
        self.setMinimumWidth(340 + 2 * self.SHADOW_PAD)

        # ---- Moteur audio ---------------------------------------------------
        self.audio = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        vol = int(self.settings.value("volume", 70))
        self.audio.setVolume(vol / 100.0)

        self._build_ui(vol)
        self._connect()
        self._build_tray()
        self._build_download_dialog()

        pos = self.settings.value("pos")
        if pos is not None:
            self.move(pos)

        if self.tracks:
            QTimer.singleShot(150, lambda: self.play_index(0))

    # ------------------------------------------------------------- flags ---
    @staticmethod
    def _flags(on_top: bool):
        f = Qt.FramelessWindowHint | Qt.Tool
        if on_top:
            f |= Qt.WindowStaysOnTopHint
        return f

    def bring_to_front(self) -> None:
        """Replace le widget au-dessus des autres fenêtres.

        Appelée uniquement sur action explicite de l'utilisateur (menu de la
        barre de menus) : WorkPlay ne se remet jamais devant tout seul, et
        cette méthode n'appelle volontairement pas activateWindow().
        """
        if not self.isVisible():
            self.show()
        self.raise_()

    def set_always_on_top(self, on: bool) -> None:
        self.always_on_top = bool(on)
        self.settings.setValue("always_on_top", self.always_on_top)
        was_visible = self.isVisible()
        self.setWindowFlags(self._flags(self.always_on_top))
        if was_visible:
            self.show()
            self.raise_()
        # Synchronise les deux surfaces de contrôle (bouton et menu tray).
        for w in (getattr(self, "btn_pin", None), getattr(self, "act_top", None)):
            if w is not None and w.isChecked() != self.always_on_top:
                w.blockSignals(True)
                w.setChecked(self.always_on_top)
                w.blockSignals(False)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self, vol: int) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            self.SHADOW_PAD, self.SHADOW_PAD, self.SHADOW_PAD, self.SHADOW_PAD
        )

        self.card = QFrame()
        self.card.setObjectName("Card")

        # Ombre portée : garde le texte lisible même quand le fond
        # translucide laisse passer une fenêtre claire derrière.
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 200))
        self.card.setGraphicsEffect(shadow)

        outer.addWidget(self.card)

        root = QVBoxLayout(self.card)
        root.setContentsMargins(14, 11, 14, 12)
        root.setSpacing(7)

        # --- Ligne 1 : titre + boutons fenêtre ------------------------------
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        titles = QVBoxLayout()
        titles.setSpacing(1)
        self.lbl_title = QLabel("—")
        self.lbl_title.setObjectName("Title")
        self.lbl_artist = QLabel(self._count_label())
        self.lbl_artist.setObjectName("Artist")
        titles.addWidget(self.lbl_title)
        titles.addWidget(self.lbl_artist)
        row1.addLayout(titles, 1)

        self.btn_pin = QPushButton("📌")
        self.btn_pin.setObjectName("Mini")
        self.btn_pin.setCheckable(True)
        self.btn_pin.setChecked(self.always_on_top)
        self.btn_pin.setFixedSize(20, 20)
        self.btn_pin.setToolTip("Toujours au premier plan (⌘⇧T)")
        self.btn_pin.toggled.connect(self.set_always_on_top)
        row1.addWidget(self.btn_pin, 0, Qt.AlignTop)

        self.btn_dl = QPushButton("＋")
        self.btn_dl.setObjectName("Mini")
        self.btn_dl.setFixedSize(20, 20)
        self.btn_dl.setToolTip("Ajouter des morceaux par URL")
        self.btn_dl.clicked.connect(self.open_download_dialog)
        row1.addWidget(self.btn_dl, 0, Qt.AlignTop)

        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("Mini")
        self.btn_close.setFixedSize(20, 20)
        self.btn_close.setToolTip("Quitter")
        self.btn_close.clicked.connect(self.quit_app)
        row1.addWidget(self.btn_close, 0, Qt.AlignTop)

        root.addLayout(row1)

        # --- Ligne 2 : progression ------------------------------------------
        row2 = QHBoxLayout()
        row2.setSpacing(7)
        self.lbl_pos = QLabel("0:00")
        self.lbl_pos.setObjectName("Time")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setFixedHeight(14)
        self.lbl_dur = QLabel("0:00")
        self.lbl_dur.setObjectName("Time")
        row2.addWidget(self.lbl_pos)
        row2.addWidget(self.slider, 1)
        row2.addWidget(self.lbl_dur)
        root.addLayout(row2)

        # --- Ligne 3 : contrôles --------------------------------------------
        row3 = QHBoxLayout()
        row3.setSpacing(4)

        self.btn_prev = QPushButton("⏮")
        self.btn_prev.setObjectName("Ctrl")
        self.btn_prev.setFixedSize(28, 28)
        self.btn_prev.setToolTip("Piste précédente  (←)")

        self.btn_play = QPushButton("▶")
        self.btn_play.setObjectName("Play")
        self.btn_play.setFixedSize(36, 36)
        self.btn_play.setToolTip("Lecture / Pause  (Espace)")

        self.btn_next = QPushButton("⏭")
        self.btn_next.setObjectName("Ctrl")
        self.btn_next.setFixedSize(28, 28)
        self.btn_next.setToolTip("Piste suivante  (→)")

        row3.addStretch(1)
        row3.addWidget(self.btn_prev)
        row3.addWidget(self.btn_play)
        row3.addWidget(self.btn_next)
        row3.addStretch(1)

        self.btn_vol = QPushButton("🔊")
        self.btn_vol.setObjectName("Mini")
        self.btn_vol.setFixedSize(22, 22)
        self.btn_vol.clicked.connect(self._toggle_mute)

        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setObjectName("Vol")
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(vol)
        self.slider_vol.setFixedSize(58, 14)
        self.slider_vol.setToolTip("Volume  (↑ ↓)")

        self.btn_list = QPushButton("☰")
        self.btn_list.setObjectName("Mini")
        self.btn_list.setCheckable(True)
        self.btn_list.setFixedSize(22, 22)
        self.btn_list.setToolTip("Playlist  (L)")

        row3.addWidget(self.btn_vol)
        row3.addWidget(self.slider_vol)
        row3.addWidget(self.btn_list)
        root.addLayout(row3)

        # --- Playlist (repliée par défaut) ----------------------------------
        self.list = QListWidget()
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._fill_list()
        self.list.hide()
        root.addWidget(self.list)

        self.setFixedHeight(self.COMPACT_H)
        self.setStyleSheet(STYLE)

    def _count_label(self) -> str:
        n = len(self.tracks)
        return f"{n} piste{'s' if n > 1 else ''}"

    def _fill_list(self) -> None:
        self.list.clear()
        for i, t in enumerate(self.tracks):
            item = QListWidgetItem(f"{i + 1:>2}.  {t.stem}")
            item.setToolTip(str(t))
            self.list.addItem(item)
        self.list.setFixedHeight(min(max(len(self.tracks), 1), 10) * self.ITEM_H)

    # --------------------------------------------------------------- tray --
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip("WorkPlay")

        menu = QMenu()

        self.act_play = menu.addAction("Lecture / Pause")
        self.act_play.triggered.connect(self.toggle_play)

        self.act_show = menu.addAction("Afficher / Masquer le widget")
        self.act_show.triggered.connect(self.toggle_visible)

        self.act_front = menu.addAction("Ramener au premier plan")
        self.act_front.triggered.connect(self.bring_to_front)

        menu.addSeparator()

        self.act_add = menu.addAction("Ajouter des morceaux par URL…")
        self.act_add.triggered.connect(self.open_download_dialog)

        self.act_paste = menu.addAction("Ajouter le lien du presse-papiers")
        self.act_paste.triggered.connect(self.add_clipboard_url)
        self.act_folder = menu.addAction("Ouvrir le dossier musique")
        self.act_folder.triggered.connect(
            lambda: os.system(f'open "{self.music_dir}"')
        )

        self.act_choose = menu.addAction("Choisir le dossier musique…")
        self.act_choose.triggered.connect(self.choose_music_dir)

        self.act_settings = menu.addAction("Réglages…")
        self.act_settings.triggered.connect(self.open_settings_dialog)

        self.act_rescan = menu.addAction("Rescanner la playlist")
        self.act_rescan.triggered.connect(self.refresh_tracks)

        menu.addSeparator()

        self.act_top = menu.addAction("Toujours au premier plan")
        self.act_top.setCheckable(True)
        self.act_top.setChecked(self.always_on_top)
        self.act_top.triggered.connect(self.set_always_on_top)

        menu.addSeparator()

        act_quit = menu.addAction("Quitter")
        act_quit.triggered.connect(self.quit_app)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:  # clic simple
            self.toggle_visible()

    def toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    # ---------------------------------------------------------- downloads --
    def _build_download_dialog(self) -> None:
        self.dlg = DownloadDialog(self.music_dir, self.download_dir, parent=None)
        self.dlg.finished_all.connect(self._after_downloads)

    def open_download_dialog(self) -> None:
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
        self.dlg.input.setFocus()

    def add_clipboard_url(self) -> None:
        """Récupère l'URL du presse-papiers et la prépare dans le dialogue.

        Ferme la boucle du geste courant : copier un lien, un clic dans la
        barre de menus, et il ne reste plus qu'à valider.
        """
        text = QApplication.clipboard().text().strip()
        if not text or not URL_RE.match(text):
            self.open_download_dialog()
            self.dlg._log("Aucune URL dans le presse-papiers.")
            return
        self.open_download_dialog()
        self.dlg.add_url(text)

    # --------------------------------------------------------- dossier -----
    def choose_music_dir(self) -> None:
        """Laisse choisir un autre dossier musical et l'adopte immédiatement."""
        chosen = QFileDialog.getExistingDirectory(
            None, "Choisir le dossier musique", str(self.music_dir)
        )
        if chosen:
            self.change_music_dir(Path(chosen))

    def change_music_dir(self, new_dir: Path) -> None:
        """Bascule vers un autre dossier, mémorise le choix, relance la lecture."""
        if new_dir == self.music_dir:
            return
        self.music_dir = new_dir
        self.settings.setValue("music_dir", str(new_dir))
        # Le dialogue de téléchargement doit écrire dans le nouveau dossier.
        self.dlg.music_dir = new_dir
        self.dlg.proc.setWorkingDirectory(str(new_dir))
        self.index = -1
        self.refresh_tracks(force=True)
        if self.tracks:
            self.play_index(0)
        else:
            self.player.stop()
            self.lbl_title.setText("Aucun fichier audio")
        self.lbl_artist.setText(self._count_label())

    def _after_downloads(self) -> None:
        """Après un lot : rapatrie les fichiers si les dossiers diffèrent,
        puis met la playlist à jour."""
        if self.download_dir != self.music_dir:
            moved = self._collect_downloads()
            if moved:
                self.dlg._log(f"{moved} fichier(s) déplacé(s) vers la bibliothèque.")
        self.refresh_tracks()

    def _collect_downloads(self) -> int:
        """Déplace les fichiers téléchargés vers la bibliothèque.

        Un fichier de même nom déjà présent n'est pas écrasé : le doublon est
        ignoré et signalé.
        """
        if not self.download_dir.is_dir():
            return 0
        moved = 0
        for src in list(self.download_dir.iterdir()):
            if src.is_dir() or src.name.startswith("."):
                continue
            if src.suffix.lower() not in AUDIO_EXTS:
                continue
            dest = self.music_dir / src.name
            try:
                if dest.exists():
                    self.dlg._log(f"déjà présent, ignoré : {src.name}")
                    continue
                shutil.move(str(src), str(dest))
                moved += 1
            except OSError as exc:
                self.dlg._log(f"déplacement impossible ({src.name}) : {exc}")
        return moved

    def set_download_dir(self, path: Path) -> None:
        self.download_dir = path
        self.settings.setValue("download_dir", str(path))
        self.dlg.download_dir = path
        self.dlg.proc.setWorkingDirectory(str(path))

    def open_settings_dialog(self) -> None:
        dlg = SettingsDialog(self.music_dir, self.download_dir, parent=None)
        dlg.applied.connect(lambda: self._apply_settings(dlg))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()   # action volontaire : l'utilisateur veut ce dialogue

    def _apply_settings(self, dlg: "SettingsDialog") -> None:
        self.set_download_dir(Path(dlg.download_dir))
        self.change_music_dir(Path(dlg.music_dir))

    # ------------------------------------------------------------ playlist --
    def refresh_tracks(self, force: bool = False) -> None:
        """Relit le dossier et met la playlist à jour sans couper la lecture."""
        playing = self.tracks[self.index].stem if 0 <= self.index < len(self.tracks) else None
        new = scan_tracks(self.music_dir)
        if new == self.tracks and not force:
            return

        self.tracks = new
        self._fill_list()
        self.lbl_artist.setText(self._count_label())

        if playing is not None:
            for i, t in enumerate(self.tracks):
                if t.stem == playing:
                    self.index = i
                    self.list.setCurrentRow(i)
                    break
        else:
            self.index = -1

    # ------------------------------------------------------------- signaux --
    def _connect(self) -> None:
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_next.clicked.connect(self.next_track)
        self.btn_prev.clicked.connect(self.prev_track)
        self.btn_list.toggled.connect(self._toggle_list)
        self.slider.sliderPressed.connect(self._seek_start)
        self.slider.sliderReleased.connect(self._seek_end)
        self.slider.sliderMoved.connect(self._seek_move)
        self.slider_vol.valueChanged.connect(self._set_volume)
        self.list.itemDoubleClicked.connect(
            lambda item: self.play_index(self.list.row(item))
        )

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_status)

    # ------------------------------------------------------------ lecture --
    def play_index(self, i: int) -> None:
        if not self.tracks:
            self.lbl_title.setText("Aucun fichier audio")
            return
        self.index = i % len(self.tracks)
        track = self.tracks[self.index]
        self.player.setSource(QUrl.fromLocalFile(str(track)))
        self.player.play()
        self.lbl_title.setText(track.stem)
        self.lbl_artist.setText(f"{self.index + 1}/{len(self.tracks)}")
        self.list.setCurrentRow(self.index)

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self.index < 0 and self.tracks:
                self.play_index(0)
            else:
                self.player.play()

    def next_track(self) -> None:
        self.play_index(self.index + 1 if self.index >= 0 else 0)

    def prev_track(self) -> None:
        # Comportement habituel : revient au début si on est déjà avancé.
        if self.player.position() > 3000:
            self.player.setPosition(0)
        else:
            self.play_index(self.index - 1 if self.index >= 0 else 0)

    # ------------------------------------------------------------ slots ----
    def _on_position(self, ms: int) -> None:
        if not self._seeking:
            self.slider.setValue(ms)
        self.lbl_pos.setText(format_time(ms))

    def _on_duration(self, ms: int) -> None:
        self.slider.setRange(0, ms)
        self.lbl_dur.setText(format_time(ms))

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self.btn_play.setText("❚❚" if playing else "▶")

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.EndOfMedia:
            self.next_track()

    def _seek_start(self) -> None:
        self._seeking = True

    def _seek_move(self, v: int) -> None:
        self.lbl_pos.setText(format_time(v))

    def _seek_end(self) -> None:
        self.player.setPosition(self.slider.value())
        self._seeking = False

    def _set_volume(self, v: int) -> None:
        self.audio.setVolume(v / 100.0)
        self.settings.setValue("volume", v)
        self.btn_vol.setText("🔇" if v == 0 else ("🔉" if v < 50 else "🔊"))

    def _toggle_mute(self) -> None:
        if self.slider_vol.value() > 0:
            self._prev_vol = self.slider_vol.value()
            self.slider_vol.setValue(0)
        else:
            self.slider_vol.setValue(getattr(self, "_prev_vol", 70))

    def _toggle_list(self, on: bool) -> None:
        self.list.setVisible(on)
        self.expanded = on
        h = self.COMPACT_H + (self.list.height() + 8 if on else 0)
        self.setFixedHeight(h)
        if on:
            self.list.setCurrentRow(self.index)

    # -------------------------------------------------------- interaction --
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._drag_offset = (
                e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, e) -> None:
        if self._drag_offset is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, e) -> None:
        self._drag_offset = None
        self.settings.setValue("pos", self.pos())

    def mouseDoubleClickEvent(self, e) -> None:
        self.toggle_play()

    def keyPressEvent(self, e) -> None:
        key = e.key()
        mods = e.modifiers()
        if key == Qt.Key_Space:
            self.toggle_play()
        elif key == Qt.Key_Right:
            self.next_track()
        elif key == Qt.Key_Left:
            self.prev_track()
        elif key == Qt.Key_Up:
            self.slider_vol.setValue(min(100, self.slider_vol.value() + 5))
        elif key == Qt.Key_Down:
            self.slider_vol.setValue(max(0, self.slider_vol.value() - 5))
        elif key == Qt.Key_L:
            self.btn_list.toggle()
        elif key == Qt.Key_Escape:
            if self.expanded:
                self.btn_list.setChecked(False)
            else:
                self.hide()
        elif key == Qt.Key_T and mods & (Qt.ControlModifier | Qt.MetaModifier):
            self.set_always_on_top(not self.always_on_top)
        else:
            super().keyPressEvent(e)

    def quit_app(self) -> None:
        self.settings.setValue("pos", self.pos())
        self.player.stop()
        self.tray.hide()
        # Ferme aussi le dialogue de téléchargement s'il tourne encore.
        self.dlg.proc.kill()
        QApplication.quit()

    def closeEvent(self, e) -> None:
        self.quit_app()


# --------------------------------------------------------------------------- #
# Auto-test
# --------------------------------------------------------------------------- #

def _test_change_dir(w) -> tuple[bool, str]:
    """Vérifie qu'on peut basculer vers un autre dossier et en revenir."""
    import tempfile
    original = w.music_dir
    tmp = Path(tempfile.mkdtemp(prefix="workplay-dir-"))
    try:
        w.change_music_dir(tmp)
        switched = w.music_dir == tmp and w.tracks == []
        w.change_music_dir(original)
        restored = w.music_dir == original and len(w.tracks) >= 1
        return (switched and restored,
                f"bascule={'OK' if switched else 'KO'} "
                f"retour={'OK' if restored else 'KO'} "
                f"({len(w.tracks)} pistes)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _screenshot_mode(app: QApplication, w: "Player", dest: Path) -> int:
    """Capture le widget seul, pour la documentation du projet.

    w.grab() rend le widget lui-même, pas l'écran : la capture ne peut donc
    contenir aucune fenêtre voisine ni aucun élément du bureau.
    """
    def shoot():
        w.btn_list.setChecked(True)          # playlist visible pour la démo
        QTimer.singleShot(400, lambda: (
            w.grab().save(str(dest), "PNG"),
            print(f"capture -> {dest}", file=sys.stderr),
            app.quit(),
        ))

    QTimer.singleShot(3000, shoot)
    return app.exec()


def _test_settings_dialog(w) -> tuple[bool, str]:
    """Le dialogue de réglages se construit et propose les deux dossiers."""
    dlg = SettingsDialog(w.music_dir, w.download_dir)
    ok = (hasattr(dlg, "chk_same") and hasattr(dlg, "btn_dl_row")
          and str(dlg.music_dir) == str(w.music_dir))
    dlg.deleteLater()
    return (ok, f"bibliothèque={dlg.music_dir.name}")


def _test_settings_apply(w) -> tuple[bool, str]:
    """Appliquer les réglages change réellement les deux dossiers."""
    import tempfile
    original_music, original_dl = w.music_dir, w.download_dir
    tmp_lib = Path(tempfile.mkdtemp(prefix="wp-lib-"))
    tmp_dl = Path(tempfile.mkdtemp(prefix="wp-dl-"))
    try:
        dlg = SettingsDialog(tmp_lib, tmp_dl)
        w._apply_settings(dlg)
        changed = (w.music_dir == tmp_lib and w.download_dir == tmp_dl)
        # Retour à l'état initial.
        back = SettingsDialog(original_music, original_dl)
        w._apply_settings(back)
        restored = (w.music_dir == original_music and w.download_dir == original_dl)
        return (changed and restored,
                f"appliqué={'OK' if changed else 'KO'} "
                f"restauré={'OK' if restored else 'KO'}")
    finally:
        for d in (tmp_lib, tmp_dl):
            shutil.rmtree(d, ignore_errors=True)


def _test_clipboard(w) -> tuple[bool, str]:
    """Une URL YouTube copiée se retrouve dans le champ de téléchargement."""
    cb = QApplication.clipboard()
    backup = cb.text()
    try:
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        cb.setText(url)
        w.dlg.input.clear()
        w.add_clipboard_url()
        got = w.dlg.input.toPlainText().strip()
        return (got == url, f"champ={got[:46] or 'vide'}")
    finally:
        cb.setText(backup)


def _test_separate_dir(w) -> tuple[bool, str]:
    """Un fichier posé dans le dossier de téléchargement rejoint la bibliothèque."""
    import tempfile
    original_dl = w.download_dir
    original_music = w.music_dir
    original_tracks = list(w.tracks)
    original_index = w.index
    tmp_dl = Path(tempfile.mkdtemp(prefix="wp-collect-"))
    lib = Path(tempfile.mkdtemp(prefix="wp-dest-"))
    try:
        w.set_download_dir(tmp_dl)
        w.music_dir = lib
        # Un fichier factice dans le dossier de téléchargement.
        fake = tmp_dl / "Titre de test.mp3"
        fake.write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
        moved = w._collect_downloads()
        arrived = (lib / fake.name).exists()
        # Un second passage ne doit pas écraser ni dupliquer.
        (tmp_dl / fake.name).write_bytes(b"ID3\x03\x00\x00\x00\x00\x00\x00")
        moved2 = w._collect_downloads()
        no_overwrite = moved2 == 0
        return (moved == 1 and arrived and no_overwrite,
                f"déplacés={moved} arrivé={arrived} pas d'écrasement={no_overwrite}")
    finally:
        # Restaure exactement l'état d'avant : sans cela la playlist
        # pointerait sur un dossier temporaire supprimé.
        w.music_dir = original_music
        w.tracks = original_tracks
        w.index = original_index
        w.set_download_dir(original_dl)
        w.refresh_tracks(force=True)
        shutil.rmtree(tmp_dl, ignore_errors=True)
        shutil.rmtree(lib, ignore_errors=True)


def _test_no_focus(w) -> tuple[bool, str]:
    w.hide()
    QApplication.processEvents()
    w.show()
    w.raise_()
    QApplication.processEvents()
    focused = QApplication.focusWindow()
    return (focused is not w, f"fenêtre au focus={focused!r}")


def _pause(w) -> bool:
    w.toggle_play()
    return w.player.playbackState() == QMediaPlayer.PausedState


def _play(w) -> bool:
    w.toggle_play()
    return w.player.playbackState() == QMediaPlayer.PlayingState


def _setvol(w, v: int) -> bool:
    w.slider_vol.setValue(v)
    return abs(w.audio.volume() - v / 100.0) < 0.01


def _report(results) -> None:
    print("\n=== AUTO-TEST ===", file=sys.stderr)
    for label, ok, detail in results:
        print(f"  [{'OK ' if ok else 'ÉCHEC'}] {label:<30} {detail}", file=sys.stderr)
    failed = [r[0] for r in results if not r[1]]
    total = len(results)
    print(f"\n  {total - len(failed)}/{total} vérifications réussies", file=sys.stderr)
    if failed:
        print(f"  ÉCHECS : {', '.join(failed)}", file=sys.stderr)
    else:
        print("  Tout répond correctement.", file=sys.stderr)


def _self_test(app: QApplication, w: "Player", url: str | None = None) -> int:
    """Exerce les contrôles et le téléchargement, puis vérifie l'état réel."""
    results: list[tuple[str, bool, str]] = []
    tmpdir = None

    def test_download():
        """Télécharge réellement une URL dans un dossier temporaire."""
        nonlocal tmpdir
        if not url:
            return (False, "aucune URL de test fournie")
        import tempfile
        tmpdir = Path(tempfile.mkdtemp(prefix="namadingo-test-"))
        proc = QProcess()
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.setProcessEnvironment(process_env())
        proc.setWorkingDirectory(str(tmpdir))
        ytdlp = find_ytdlp()
        proc.start(ytdlp, YTDLP_ARGS + [url])
        if not proc.waitForFinished(120_000):
            return (False, "délai dépassé")
        files = scan_tracks(tmpdir)
        return (
            proc.exitCode() == 0 and len(files) == 1,
            f"code={proc.exitCode()} fichier={files[0].name if files else 'aucun'}",
        )

    steps = iter([
        ("playlist chargée", lambda: (len(w.tracks) >= 10, f"{len(w.tracks)} pistes")),
        ("lecture auto au démarrage", lambda: (
            w.player.playbackState() == QMediaPlayer.PlayingState,
            f"state={w.player.playbackState()}")),
        ("position avance", lambda: (w.player.position() > 300,
                                     f"pos={w.player.position()}ms")),
        ("titre affiché", lambda: (w.lbl_title.text() != "—", w.lbl_title.text())),
        ("pause", lambda: (_pause(w), f"state={w.player.playbackState()}")),
        ("reprise", lambda: (_play(w), f"state={w.player.playbackState()}")),
        ("piste suivante", lambda: (
            w.next_track() or w.index == 1, f"index={w.index}")),
        ("piste précédente", lambda: (
            w.prev_track() or w.index == 0, f"index={w.index}")),
        ("barre de progression", lambda: (
            w.slider.maximum() > 0, f"max={w.slider.maximum()}ms")),
        ("durée affichée", lambda: (w.lbl_dur.text() != "0:00", w.lbl_dur.text())),
        ("volume modifiable", lambda: (_setvol(w, 42), f"vol={w.audio.volume():.2f}")),
        ("playlist dépliable", lambda: (
            w.btn_list.setChecked(True) or w.list.isVisible(),
            f"h={w.height()}px")),
        # --- nouveautés -----------------------------------------------------
        ("always on top actif", lambda: (
            bool(w.windowFlags() & Qt.WindowStaysOnTopHint), "drapeau posé")),
        ("aucune ré-assertion périodique", lambda: (
            not hasattr(w, "top_timer"),
            "WorkPlay ne se remet jamais devant tout seul")),
        ("affichage sans activation", lambda: (
            w.testAttribute(Qt.WA_ShowWithoutActivating),
            "WA_ShowWithoutActivating posé")),
        ("le widget ne prend pas le focus", lambda: _test_no_focus(w)),
        ("always on top désactivable", lambda: (
            (w.set_always_on_top(False),
             not (w.windowFlags() & Qt.WindowStaysOnTopHint))[1], "drapeau retiré")),
        ("always on top réactivable", lambda: (
            (w.set_always_on_top(True),
             bool(w.windowFlags() & Qt.WindowStaysOnTopHint))[1], "drapeau reposé")),
        ("tray icon présent", lambda: (
            w.tray is not None and w.tray.isVisible(), "icône affichée")),
        ("menu tray complet", lambda: (
            len(w.tray.contextMenu().actions()) >= 8,
            f"{len(w.tray.contextMenu().actions())} entrées")),
        ("dossier musique configurable", lambda: (
            hasattr(w, "choose_music_dir") and hasattr(w, "change_music_dir"),
            str(w.music_dir))),
        ("changement de dossier", lambda: _test_change_dir(w)),
        ("yt-dlp localisé", lambda: (find_ytdlp() is not None, str(find_ytdlp()))),
        ("dialogue URLs construit", lambda: (
            w.dlg is not None and w.dlg.input is not None, "champ de saisie prêt")),
        ("URLs filtrées", lambda: (
            (w.dlg.input.setPlainText("pas une url\nhttps://a/b\nhttps://c/d"),
             len([l for l in w.dlg.input.toPlainText().splitlines()
                  if URL_RE.match(l.strip())]) == 2)[1],
            "2 URLs valides sur 3 lignes")),
        ("rescan playlist", lambda: (
            (w.refresh_tracks() or True), f"{len(w.tracks)} pistes")),
        ("réglages : dialogue construit", lambda: _test_settings_dialog(w)),
        ("réglages : applique les dossiers", lambda: _test_settings_apply(w)),
        ("presse-papiers : URL détectée", lambda: _test_clipboard(w)),
        ("téléchargement vers dossier séparé", lambda: _test_separate_dir(w)),
        ("téléchargement réel", test_download),
    ])

    def cleanup():
        nonlocal tmpdir
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
            tmpdir = None

    # Garde-fou : un test qui se bloque ne doit jamais figer la vérification
    # indéfiniment sans rien dire.
    watchdog = QTimer()
    watchdog.setSingleShot(True)

    def bail():
        results.append(("(délai global dépassé)", False,
                        "arrêt forcé — une étape ne rend pas la main"))
        _report(results)
        cleanup()
        app.quit()

    watchdog.timeout.connect(bail)
    watchdog.start(150_000)

    def step():
        try:
            label, fn = next(steps)
        except StopIteration:
            watchdog.stop()
            _report(results)
            cleanup()
            app.quit()
            return
        # Trace immédiate : si une étape se bloque, on sait laquelle.
        print(f"  … {label}", file=sys.stderr, flush=True)
        try:
            ok, detail = fn()
            results.append((label, bool(ok), detail))
        except Exception as exc:  # noqa: BLE001 - le test rapporte, il ne meurt pas
            results.append((label, False, f"EXCEPTION {exc!r}"))
        QTimer.singleShot(400, step)

    QTimer.singleShot(2500, step)
    return app.exec()


# --------------------------------------------------------------------------- #
# Point d'entrée
# --------------------------------------------------------------------------- #

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("WorkPlay")
    app.setOrganizationName("com.m5max")
    app.setOrganizationDomain("com.m5max")
    # Le tray doit survivre à la fermeture de la fenêtre.
    app.setQuitOnLastWindowClosed(False)

    # Ordre de priorité : variable d'environnement > choix mémorisé > défaut.
    prefs = QSettings("com.m5max", "WorkPlay")
    saved = prefs.value("music_dir")
    music_dir = Path(
        os.environ.get("WORKPLAY_DIR") or saved or MUSIC_DIR_DEFAULT
    ).expanduser()
    saved_dl = prefs.value("download_dir")
    download_dir = Path(saved_dl).expanduser() if saved_dl else music_dir

    # Au premier lancement on crée le dossier au lieu d'échouer : l'utilisateur
    # n'a qu'à y déposer ses fichiers, ou les télécharger depuis le tray.
    for d in {music_dir, download_dir}:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Impossible de créer {d} : {exc}", file=sys.stderr)
            return 1

    w = Player(music_dir, download_dir)
    w.show()
    w.raise_()
    # Volontairement pas d'activateWindow() : WorkPlay ne doit jamais
    # s'imposer au premier plan ni voler le focus au lancement.

    if os.environ.get("WORKPLAY_SHOT"):
        return _screenshot_mode(app, w, Path(os.environ["WORKPLAY_SHOT"]))

    if os.environ.get("WORKPLAY_SELFTEST"):
        return _self_test(app, w, os.environ.get("WORKPLAY_TEST_URL"))

    if os.environ.get("WORKPLAY_DEBUG"):
        def _probe():
            g = w.frameGeometry()
            print(
                f"[debug] visible={w.isVisible()} x={g.x()} y={g.y()} "
                f"w={g.width()} h={g.height()} tracks={len(w.tracks)} "
                f"state={w.player.playbackState()} pos={w.player.position()} "
                f"ontop={bool(w.windowFlags() & Qt.WindowStaysOnTopHint)} "
                f"tray={w.tray.isVisible()}",
                file=sys.stderr, flush=True,
            )
        for delay in (600, 2500, 6000):
            QTimer.singleShot(delay, _probe)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
