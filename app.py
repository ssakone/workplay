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
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, QUrl, QSettings, QPoint, QProcess, QProcessEnvironment, Signal,
)
from PySide6.QtGui import (
    QColor, QFont, QIcon, QPainter, QPixmap, QTextCursor, QKeySequence, QShortcut,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QWidget, QFrame, QLabel, QPushButton, QSlider,
    QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem, QDialog,
    QPlainTextEdit, QProgressBar, QMenu, QSystemTrayIcon,
    QGraphicsDropShadowEffect, QFileDialog, QCheckBox, QComboBox, QInputDialog,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

# Version de l'application. C'est la SEULE source de vérité côté code : le
# bundle .app la reprend depuis workplay.spec, et tools/build.sh vérifie que
# les deux concordent pour éviter qu'une release annonce un mauvais numéro.
APP_VERSION = "1.5.0"

# Mises à jour : dépôt public, releases GitHub.
UPDATE_REPO = "ssakone/workplay"
UPDATE_API = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"

# Identité de signature attendue. Une mise à jour qui ne porte pas CE Team ID
# est refusée : c'est ce qui empêche d'installer un binaire substitué.
EXPECTED_TEAM_ID = "Z64CCL2WWK"

# Les versions remplacées sont conservées ici plutôt que supprimées, pour
# pouvoir revenir en arrière si une mise à jour déplaît.
BACKUP_DIR = (
    Path.home() / "Library" / "Application Support" / "WorkPlay" / "versions"
)

# Nombre de versions précédentes conservées (les plus anciennes sont purgées).
BACKUP_KEEP = 3

# Dossier musical par défaut. Modifiable depuis la barre de menus ;
# l'application le choisit elle-même, aucun chemin n'est codé en dur.
MUSIC_DIR_DEFAULT = Path.home() / "Music" / "WorkPlay"

# Dossier des vidéos téléchargées. Séparé de la musique : une vidéo n'a pas sa
# place dans la liste de lecture audio.
VIDEO_DIR_DEFAULT = Path.home() / "Movies" / "WorkPlay"

# Playlists nommées : de simples fichiers JSON. Une playlist ne contient que
# des noms de fichiers, jamais l'audio lui-même — le morceau reste unique dans
# la bibliothèque.
PLAYLISTS_DIR = (
    Path.home() / "Library" / "Application Support" / "WorkPlay" / "playlists"
)

# Modes de répétition.
REPEAT_OFF, REPEAT_ONE, REPEAT_ALL = "off", "one", "all"
REPEAT_ORDER = [REPEAT_OFF, REPEAT_ONE, REPEAT_ALL]
REPEAT_LABEL = {
    REPEAT_OFF: ("↻", "Répétition désactivée"),
    REPEAT_ONE: ("↻1", "Répéter le morceau en cours"),
    REPEAT_ALL: ("↻∞", "Répéter toute la liste (boucle)"),
}

# Ordre d'affichage souhaité. Les fichiers listés ici passent en tête, dans cet
# ordre ; tout le reste suit par ordre alphabétique. Vide par défaut :
# l'application est générique.
PREFERRED_ORDER: list[str] = []

# Filtres de la bibliothèque : ce que l'utilisateur choisit de voir dans la liste.
FILTER_ALL, FILTER_AUDIO, FILTER_VIDEO = "all", "audio", "video"
FILTER_LABEL = {
    FILTER_ALL: "Tout (audio + vidéo)",
    FILTER_AUDIO: "Musique seulement",
    FILTER_VIDEO: "Vidéos seulement",
}


def matches_filter(path: Path, mode: str) -> bool:
    """Vrai si le fichier appartient au filtre demandé."""
    suffix = path.suffix.lower()
    if mode == FILTER_AUDIO:
        return suffix in AUDIO_EXTS
    if mode == FILTER_VIDEO:
        return suffix in VIDEO_EXTS
    return suffix in AUDIO_EXTS | VIDEO_EXTS

# Exemple — décommente et adapte pour épingler tes morceaux en tête :
# PREFERRED_ORDER = ["Mon morceau préféré", "Un autre titre"]

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}

# Formats vidéo. Séparés des formats audio : ils vivent dans le dossier vidéo
# et ne polluent pas la liste de lecture musicale.
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

# Emplacements habituels des binaires Homebrew, ajoutés au PATH du sous-processus
# (le PATH hérité d'une app lancée depuis le Finder est minimal).
EXTRA_PATHS = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]

# --- Téléchargement vidéo --------------------------------------------------
# Qualités proposées. Chaque sélecteur privilégie le couple H.264 + AAC, que
# macOS décode nativement, et ne retombe sur un autre codec que si la source
# n'offre rien d'autre.
#
# POURQUOI : YouTube propose par défaut de l'AV1, souvent choisi comme
# « meilleure qualité ». Or le décodeur AV1 de Qt n'a pas de décodage matériel
# ici et reste bloqué en BufferingMedia — son audible, image noire, aucune
# erreur signalée. H.264 évite complètement ce piège.

def _video_selector(height: int | None) -> str:
    res = f"[height<={height}]" if height else ""
    optimized = f"bv*{res}[vcodec^=avc1]+ba[acodec^=mp4a]"
    fallback = f"bv*{res}+ba/b{res}"
    return f"{optimized}/{fallback}"


VIDEO_QUALITY_PRESETS = [
    ("Meilleure qualité", _video_selector(None)),
    ("1080p", _video_selector(1080)),
    ("720p", _video_selector(720)),
    ("480p", _video_selector(480)),
    ("360p (léger)", _video_selector(360)),
]


def video_download_args(selector: str) -> list[str]:
    """Arguments yt-dlp pour une vidéo, fusionnée en MP4."""
    return [
        "-f", selector,
        "--merge-output-format", "mp4",
        # Sans cela, yt-dlp peut produire du MP4 contenant de l'AV1 ou de
        # l'Opus, illisibles par le moteur de Qt.
        "--remux-video", "mp4",
        "--newline",
        "--ignore-errors",
        "--no-overwrites",
        "-o", "%(title)s.%(ext)s",
    ]


def probe_video_codec(path: Path) -> str | None:
    """Renvoie le codec de la piste vidéo d'un fichier, ou None.

    Sert à détecter après coup une vidéo que Qt ne saura pas décoder.
    """
    ffprobe = shutil.which("ffprobe") or next(
        (str(Path(b) / "ffprobe") for b in EXTRA_PATHS
         if (Path(b) / "ffprobe").exists()), None
    )
    if not ffprobe or not Path(path).is_file():
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


# Codecs que le moteur multimédia de Qt décode de façon fiable sous macOS.
PLAYABLE_VIDEO_CODECS = {"h264", "hevc", "mpeg4", "vp8", "vp9", "mjpeg"}


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
    """Liste les fichiers audio d'un dossier, dans l'ordre d'affichage."""
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir()
             if p.suffix.lower() in AUDIO_EXTS and not p.name.startswith(".")]
    return sorted(files, key=lambda p: natural_key(p.name))


def scan_media(directories: list[Path]) -> list[Path]:
    """Liste audio + vidéo de plusieurs dossiers, dédoublonnés par chemin.

    Un fichier vidéo dans un dossier audio (ou l'inverse) est classé selon
    l'extension : la liste est fidèle au contenu, pas à la case du dossier.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for p in directory.iterdir():
            if p.name.startswith(".") or p in seen:
                continue
            if p.suffix.lower() in AUDIO_EXTS | VIDEO_EXTS:
                seen.add(p)
                out.append(p)
    return sorted(out, key=lambda p: natural_key(p.name))


def find_ytdlp() -> str | None:
    """Localise yt-dlp, en tenant compte des PATH minimaux des apps GUI."""
    for base in EXTRA_PATHS:
        cand = Path(base) / "yt-dlp"
        if cand.exists():
            return str(cand)
    return shutil.which("yt-dlp")


def find_ffmpeg() -> str | None:
    """ffmpeg fusionne vidéo et audio, et convertit en MP3."""
    for base in EXTRA_PATHS:
        cand = Path(base) / "ffmpeg"
        if cand.exists():
            return str(cand)
    return shutil.which("ffmpeg")


def sanitize_name(name: str) -> str:
    """Nom de playlist -> nom de fichier sûr."""
    cleaned = re.sub(r"[^\w\- ]", "_", name, flags=re.UNICODE).strip()
    return cleaned or "playlist"


# --------------------------------------------------------------------------- #
# Playlists nommées
# --------------------------------------------------------------------------- #

class PlaylistStore:
    """Playlists nommées, persistées en JSON.

    Une playlist ne contient que des *noms de fichiers* : le morceau reste
    unique dans la bibliothèque. Déplacer un fichier rend l'entrée introuvable
    sans rien casser — la playlist reste lisible et réparable.
    """

    def __init__(self, base: Path = PLAYLISTS_DIR):
        self.base = Path(base)
        try:
            self.base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _path(self, name: str) -> Path:
        return self.base / f"{sanitize_name(name)}.json"

    def names(self) -> list[str]:
        if not self.base.is_dir():
            return []
        found = []
        for p in sorted(self.base.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                found.append(data.get("name") or p.stem)
            except (OSError, ValueError):
                continue
        return found

    def load(self, name: str) -> list[str]:
        p = self._path(name)
        if not p.is_file():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        tracks = data.get("tracks")
        return ([t for t in tracks if isinstance(t, str)]
                if isinstance(tracks, list) else [])

    def save(self, name: str, tracks: list[str]) -> None:
        self._path(name).write_text(
            json.dumps({"name": name, "tracks": list(tracks)},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def delete(self, name: str) -> None:
        try:
            self._path(name).unlink()
        except OSError:
            pass

    def remove_track(self, name: str, filename: str) -> None:
        self.save(name, [t for t in self.load(name) if t != filename])

    def add_track(self, name: str, filename: str) -> bool:
        """Ajoute un morceau ; renvoie False s'il y était déjà."""
        tracks = self.load(name)
        if filename in tracks:
            return False
        tracks.append(filename)
        self.save(name, tracks)
        return True


def resolve_playlist(names: list[str], music_dirs: list[Path]) -> list[Path]:
    """Noms de fichiers -> chemins existants, dans l'ordre de la playlist.

    Plusieurs dossiers : un nom est cherché dans chacun, dans l'ordre où ils
    sont déclarés. Le premier dossier qui contient le fichier gagne.
    """
    out: list[Path] = []
    for name in names:
        for d in music_dirs:
            cand = d / name
            if cand.is_file():
                out.append(cand)
                break
    return out


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
        # Noms volontairement préfixés : « done » et « failed » écraseraient
        # des méthodes de QDialog (dont done(), que Qt appelle en interne),
        # ce qui cassait la fermeture du dialogue.
        self.n_done = 0
        self.n_failed = 0
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
        self._prepare_dirs()
        self.proc.setWorkingDirectory(str(self.music_dir))
        self.proc.readyReadStandardOutput.connect(self._on_output)
        # Un échec de lancement doit se voir, pas disparaître.
        self.proc.errorOccurred.connect(self._on_error)
        self.proc.finished.connect(self._on_proc_finished)

        if find_ytdlp() is None:
            self._log("yt-dlp introuvable — installe-le : brew install yt-dlp ffmpeg")

    def _prepare_dirs(self) -> None:
        """Crée les dossiers de travail : un dossier absent fait échouer le
        lancement de yt-dlp, ce qui était auparavant totalement silencieux."""
        for d in {self.music_dir, self.download_dir}:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    def _on_error(self, error) -> None:
        self._log(f"⚠︎ échec du lancement de yt-dlp (code {int(error)})")
        self.btn_add.setEnabled(True)

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
                f"Terminé : {self.n_done} morceau(x) ajouté(s)"
                + (f", {self.n_failed} échec(s)." if self.n_failed else ".")
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
            self.n_done += 1
        else:
            self.n_failed += 1
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

class _FolderList(QWidget):
    """Liste de dossiers avec ajout / suppression, pour le dialogue réglages.

    Chaque dossier ajouté est scanné par la bibliothèque ; l'utilisateur en
    ajoute autant qu'il veut, et les retire d'un clic sur « − ».
    """

    changed = Signal()

    def __init__(self, paths: list[Path], parent=None):
        super().__init__(parent)
        self.paths = [Path(p) for p in paths]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.list = QListWidget()
        self.list.setFixedHeight(96)
        self.list.setStyleSheet(
            "QListWidget{background:rgba(255,255,255,10);"
            "border:1px solid rgba(255,255,255,30);border-radius:8px;}"
            "QListWidget::item{padding:4px 8px;}"
        )
        lay.addWidget(self.list)

        row = QHBoxLayout()
        row.setSpacing(6)
        btn_add = QPushButton("＋ Ajouter un dossier…")
        btn_add.setObjectName("Ghost")
        btn_add.setFixedHeight(26)
        btn_add.clicked.connect(self._add)
        row.addWidget(btn_add)

        self.btn_del = QPushButton("− Retirer")
        self.btn_del.setObjectName("Ghost")
        self.btn_del.setFixedHeight(26)
        self.btn_del.clicked.connect(self._remove)
        row.addWidget(self.btn_del)
        row.addStretch(1)
        lay.addLayout(row)
        # Le premier refill a besoin du bouton pour l'activer/désactiver.
        self._refill()

    def _refill(self) -> None:
        self.list.clear()
        for p in self.paths:
            QListWidgetItem(str(p), self.list)
        self.btn_del.setEnabled(bool(self.paths))

    def _add(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Ajouter un dossier", str(self.paths[-1] if self.paths
                                             else Path.home())
        )
        if chosen:
            path = Path(chosen)
            if path not in self.paths:
                self.paths.append(path)
                self._refill()
                self.changed.emit()

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.paths):
            del self.paths[row]
            self._refill()
            self.changed.emit()


class SettingsDialog(QDialog):
    """Réglages : dossiers de la bibliothèque, dossier vidéo, fenêtre vidéo.

    Les dossiers se choisissent par le sélecteur macOS plutôt qu'en les tapant :
    c'est plus sûr et cela évite les chemins invalides. On peut en ajouter
    autant qu'on veut — la bibliothèque lit tout ce qui est déclaré.
    """

    applied = Signal()

    def __init__(self, music_dirs: list[Path], video_dirs: list[Path],
                 video_separate_window: bool,
                 download_dir: Path, parent=None):
        super().__init__(parent)
        self.music_dirs = [Path(d) for d in music_dirs]
        self.video_dirs = [Path(d) for d in video_dirs]
        self.video_separate_window = bool(video_separate_window)
        self.download_dir = Path(download_dir)
        # Le téléchargement atterrit dans le premier dossier audio s'il existe.
        self.same_as_library = (
            self.download_dir == self.music_dirs[0] if self.music_dirs else True
        )

        self.setWindowTitle("Réglages WorkPlay")
        self.setMinimumWidth(600)
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

        # --- Dossiers audio --------------------------------------------------
        root.addWidget(self._section(
            "Bibliothèque audio",
            "Dossiers où WorkPlay cherche vos fichiers audio. "
            "Ajoutez-en autant que vous voulez.",
        ))
        self.folders_music = _FolderList(self.music_dirs, self)
        self.folders_music.changed.connect(self._sync_enabled)
        root.addWidget(self.folders_music)

        # --- Dossiers vidéo --------------------------------------------------
        root.addWidget(self._section(
            "Bibliothèque vidéo",
            "Dossiers où WorkPlay cherche vos fichiers vidéo.",
        ))
        self.folders_video = _FolderList(self.video_dirs, self)
        root.addWidget(self.folders_video)

        # --- Fenêtre vidéo séparée -------------------------------------------
        self.chk_separate = QCheckBox(
            "Ouvrir la vidéo dans une fenêtre séparée"
        )
        self.chk_separate.setChecked(self.video_separate_window)
        self.chk_separate.setToolTip(
            "Si décoché, la vidéo joue dans le widget compact (son seul)."
        )
        root.addWidget(self.chk_separate)

        # --- Dossier de téléchargement --------------------------------------
        root.addWidget(self._section(
            "Téléchargement",
            "Où arrivent les morceaux récupérés depuis une URL.",
        ))
        self.chk_same = QCheckBox(
            "Télécharger dans le premier dossier audio"
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
    def _choose_download(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choisir le dossier de téléchargement", str(self.download_dir)
        )
        if chosen:
            self.download_dir = Path(chosen)
            self.lbl_dl.setText(str(self.download_dir))

    def _on_same_toggled(self) -> None:
        self.same_as_library = self.chk_same.isChecked()
        if self.same_as_library and self.music_dirs:
            self.download_dir = self.music_dirs[0]
            self.lbl_dl.setText(str(self.download_dir))
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        # Le second dossier n'a de sens que s'il diffère du premier.
        for i in range(self.btn_dl_row.count()):
            w = self.btn_dl_row.itemAt(i).widget()
            if w is not None:
                w.setEnabled(not self.same_as_library)
        self.lbl_dl.setEnabled(not self.same_as_library)
        # La case « même dossier » n'a de sens que s'il y a au moins un dossier
        # audio ; sans lui, on ne sait pas où livrer.
        self.chk_same.setEnabled(bool(self.folders_music.paths))

    def _accept(self) -> None:
        # On recopie les listes mutées par _FolderList dans les attributs
        # publics du dialogue — c'est ce que Player relit ensuite.
        self.music_dirs = list(self.folders_music.paths)
        self.video_dirs = list(self.folders_video.paths)
        self.video_separate_window = self.chk_separate.isChecked()
        self.applied.emit()
        self.accept()


# --------------------------------------------------------------------------- #
# Mises à jour
# --------------------------------------------------------------------------- #

def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.10' -> (1, 2, 10). Comparaison numérique, pas alphabétique.

    Sans cela, '1.2.10' passerait pour antérieur à '1.2.9'.
    """
    cleaned = re.sub(r"^v", "", (text or "").strip())
    parts = re.findall(r"\d+", cleaned)
    return tuple(int(p) for p in parts[:4]) or (0,)


def is_newer(candidate: str, current: str) -> bool:
    """Vrai si `candidate` est une version strictement postérieure."""
    return parse_version(candidate) > parse_version(current)


def installed_app_path() -> Path | None:
    """Chemin du bundle .app en cours d'exécution, ou None hors bundle.

    En développement (app.py lancé directement), il n'y a pas de bundle :
    la mise à jour est alors désactivée plutôt que d'opérer sur un chemin
    fantaisiste.
    """
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def verify_bundle(path: Path) -> tuple[bool, str]:
    """Vérifie qu'un bundle téléchargé est authentique AVANT de l'installer.

    Trois contrôles, dans cet ordre :
      1. la signature de code est valide et complète ;
      2. le Team ID est bien celui attendu — sinon n'importe quel binaire
         signé par n'importe qui serait accepté ;
      3. Gatekeeper l'accepte (donc la notarisation Apple est reconnue).
    Un échec sur l'un des trois annule l'installation.
    """
    if not path.is_dir():
        return (False, "bundle introuvable")

    try:
        sig = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(path)],
            capture_output=True, text=True, timeout=180,
        )
        if sig.returncode != 0:
            return (False, f"signature invalide : {sig.stderr.strip()[:120]}")

        info = subprocess.run(
            ["codesign", "-dv", "--verbose=2", str(path)],
            capture_output=True, text=True, timeout=120,
        )
        blob = info.stderr + info.stdout
        found = re.search(r"TeamIdentifier=(\S+)", blob)
        team = found.group(1) if found else "absent"
        if team != EXPECTED_TEAM_ID:
            return (False, f"Team ID inattendu : {team}")

        gate = subprocess.run(
            ["spctl", "--assess", "--type", "execute", str(path)],
            capture_output=True, text=True, timeout=180,
        )
        if gate.returncode != 0:
            return (False, "refusé par Gatekeeper (non notarisé ?)")
    except (OSError, subprocess.SubprocessError) as exc:
        return (False, f"vérification impossible : {exc}")

    return (True, f"signé {team}, notarisé")


def backup_current(app_path: Path) -> Path | None:
    """Archive la version installée avant de la remplacer.

    Remplaçait-on l'application sans copie, la version précédente serait
    définitivement perdue — c'est ce qui se passait jusqu'ici, puisque glisser
    un .app dans /Applications écrase l'ancien sans rien conserver.
    """
    version = bundle_version(app_path) or "inconnue"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = BACKUP_DIR / f"WorkPlay-{version}-{stamp}.app"
        # ditto préserve les liens, permissions et signatures du bundle,
        # contrairement à une copie naïve.
        res = subprocess.run(
            ["ditto", str(app_path), str(dest)],
            capture_output=True, text=True, timeout=600,
        )
        if res.returncode != 0 or not dest.is_dir():
            return None
        prune_backups()
        return dest
    except (OSError, subprocess.SubprocessError):
        return None


def list_backups() -> list[Path]:
    if not BACKUP_DIR.is_dir():
        return []
    return sorted(
        (p for p in BACKUP_DIR.iterdir() if p.suffix == ".app"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def prune_backups(keep: int = BACKUP_KEEP) -> int:
    """Garde les `keep` sauvegardes les plus récentes, supprime le reste."""
    removed = 0
    for old in list_backups()[keep:]:
        try:
            shutil.rmtree(old, ignore_errors=True)
            removed += 1
        except OSError:
            pass
    return removed


def bundle_version(app_path: Path) -> str | None:
    """Lit CFBundleShortVersionString d'un bundle."""
    plist = Path(app_path) / "Contents" / "Info.plist"
    if not plist.is_file():
        return None
    try:
        out = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c",
             "Print :CFBundleShortVersionString", str(plist)],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def fetch_latest_release(timeout: int = 20) -> dict | None:
    """Interroge l'API GitHub pour la dernière release publiée.

    Renvoie None en cas de coupure réseau ou de réponse inattendue : une mise
    à jour indisponible ne doit jamais empêcher d'écouter sa musique.
    """
    req = urllib.request.Request(
        UPDATE_API,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"WorkPlay/{APP_VERSION}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None

    tag = data.get("tag_name")
    if not isinstance(tag, str):
        return None

    dmg = None
    for asset in data.get("assets") or []:
        name = asset.get("name", "")
        if name.endswith(".dmg"):
            dmg = {"name": name,
                   "url": asset.get("browser_download_url"),
                   "size": asset.get("size", 0)}
            break

    return {
        "tag": tag,
        "version": re.sub(r"^v", "", tag),
        "name": data.get("name") or tag,
        "notes": data.get("body") or "",
        "dmg": dmg,
        "html_url": data.get("html_url", ""),
    }


# --------------------------------------------------------------------------- #
# Dialogue « Mise à jour »
# --------------------------------------------------------------------------- #

class UpdateDialog(QDialog):
    """Détecte, télécharge, vérifie et installe une nouvelle version.

    Chaîne complète en un clic :
      téléchargement du DMG -> montage -> vérification (signature, Team ID,
      Gatekeeper) -> archivage de la version en place -> remplacement ->
      relance.

    Rien n'est installé avant que la vérification ait réussi, et la version
    précédente est toujours conservée : une mise à jour ratée reste réversible.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.release: dict | None = None
        self.dmg_path: Path | None = None
        self.mount_point: Path | None = None
        self.busy = False

        self.setWindowTitle("Mise à jour de WorkPlay")
        self.setMinimumWidth(560)
        self.setStyleSheet(STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("Mise à jour")
        title.setObjectName("DlgTitle")
        root.addWidget(title)

        self.lbl_state = QLabel(f"Version installée : {APP_VERSION}")
        self.lbl_state.setObjectName("DlgHint")
        self.lbl_state.setWordWrap(True)
        root.addWidget(self.lbl_state)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        root.addWidget(self.bar)

        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(150)
        root.addWidget(self.log)

        buttons = QHBoxLayout()
        self.btn_check = QPushButton("Rechercher")
        self.btn_check.setObjectName("Ghost")
        self.btn_check.clicked.connect(lambda: self.check(manual=True))
        buttons.addWidget(self.btn_check)

        self.btn_rollback = QPushButton("Revenir à la version précédente")
        self.btn_rollback.setObjectName("Ghost")
        self.btn_rollback.clicked.connect(self.rollback)
        buttons.addWidget(self.btn_rollback)

        buttons.addStretch(1)

        self.btn_install = QPushButton("Installer")
        self.btn_install.setObjectName("Primary")
        self.btn_install.setEnabled(False)
        self.btn_install.clicked.connect(self.install)
        buttons.addWidget(self.btn_install)
        root.addLayout(buttons)

        self._refresh_rollback_button()

    # ------------------------------------------------------------- journal --
    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)
        self.log.moveCursor(QTextCursor.End)

    def _refresh_rollback_button(self) -> None:
        saved = list_backups()
        self.btn_rollback.setEnabled(bool(saved) and not self.busy)
        if saved:
            self.btn_rollback.setToolTip(
                f"{len(saved)} version(s) conservée(s) — la plus récente : "
                f"{saved[0].name}"
            )
        else:
            self.btn_rollback.setToolTip("Aucune version précédente conservée")

    # ------------------------------------------------------------ recherche --
    def check(self, manual: bool = False) -> None:
        """Interroge GitHub. En mode automatique, reste silencieux si à jour."""
        if self.busy:
            return
        if manual:
            self._log("Recherche d'une nouvelle version…")
        rel = fetch_latest_release()
        if rel is None:
            self.lbl_state.setText(
                f"Version installée : {APP_VERSION} — vérification impossible"
            )
            if manual:
                self._log("Impossible de joindre GitHub (réseau ?).")
            return

        self.release = rel
        if not is_newer(rel["version"], APP_VERSION):
            self.lbl_state.setText(
                f"WorkPlay {APP_VERSION} est à jour "
                f"(dernière publiée : {rel['version']})."
            )
            self.btn_install.setEnabled(False)
            if manual:
                self._log("Aucune nouvelle version.")
            return

        if not rel.get("dmg") or not rel["dmg"].get("url"):
            self.lbl_state.setText(
                f"Version {rel['version']} publiée, mais sans image disque."
            )
            self._log("La release ne contient pas de .dmg installable.")
            return

        size_mb = rel["dmg"]["size"] / 1048576
        self.lbl_state.setText(
            f"WorkPlay {rel['version']} est disponible "
            f"(vous avez {APP_VERSION}) — {size_mb:.0f} Mo."
        )
        self.btn_install.setEnabled(True)
        self._log(f"Nouvelle version : {rel['name']}")
        first = (rel.get("notes") or "").strip().splitlines()
        for line in first[:6]:
            if line.strip():
                self._log(f"  {line.strip()[:110]}")

    # ------------------------------------------------------- installation --
    def install(self) -> None:
        """Télécharge, vérifie, archive, remplace, relance."""
        if self.busy or not self.release:
            return
        target = installed_app_path()
        if target is None:
            self._log(
                "Mise à jour indisponible : WorkPlay ne tourne pas depuis un "
                "bundle .app (mode développement)."
            )
            return

        self.busy = True
        self.btn_install.setEnabled(False)
        self.btn_check.setEnabled(False)
        self._refresh_rollback_button()
        try:
            self._run_install(target)
        finally:
            self._cleanup()
            self.busy = False
            self.btn_check.setEnabled(True)
            self._refresh_rollback_button()

    def _run_install(self, target: Path) -> None:
        rel = self.release
        assert rel is not None
        dmg = rel["dmg"]

        # 1. Téléchargement
        self._log(f"Téléchargement de {dmg['name']}…")
        tmp = Path(tempfile.mkdtemp(prefix="workplay-update-"))
        self.dmg_path = tmp / dmg["name"]
        if not self._download(dmg["url"], self.dmg_path, dmg.get("size", 0)):
            self._log("Téléchargement interrompu.")
            return

        # 2. Montage
        self._log("Montage de l'image disque…")
        self.mount_point = tmp / "mnt"
        self.mount_point.mkdir(exist_ok=True)
        mount = subprocess.run(
            ["hdiutil", "attach", str(self.dmg_path),
             "-mountpoint", str(self.mount_point), "-nobrowse", "-quiet"],
            capture_output=True, text=True, timeout=300,
        )
        if mount.returncode != 0:
            self._log(f"Montage impossible : {mount.stderr.strip()[:120]}")
            return

        new_app = self.mount_point / "WorkPlay.app"
        if not new_app.is_dir():
            self._log("L'image ne contient pas WorkPlay.app.")
            return

        # 3. Vérification de sécurité — avant toute écriture
        self._log("Vérification de la signature et de la notarisation…")
        ok, detail = verify_bundle(new_app)
        if not ok:
            self._log(f"⚠︎ Mise à jour REFUSÉE : {detail}")
            self._log("Rien n'a été installé.")
            return
        self._log(f"✓ {detail}")

        found_version = bundle_version(new_app) or "?"
        if not is_newer(found_version, APP_VERSION):
            self._log(
                f"L'image contient la version {found_version}, "
                f"qui n'est pas plus récente. Abandon."
            )
            return

        # 4. Archivage de la version en place
        self._log("Sauvegarde de la version actuelle…")
        saved = backup_current(target)
        if saved is None:
            self._log("⚠︎ Sauvegarde impossible — installation annulée.")
            return
        self._log(f"✓ conservée : {saved.name}")

        # 5. Remplacement
        self._log(f"Installation de la version {found_version}…")
        staging = target.with_name("WorkPlay.app.new")
        shutil.rmtree(staging, ignore_errors=True)
        copy = subprocess.run(
            ["ditto", str(new_app), str(staging)],
            capture_output=True, text=True, timeout=900,
        )
        if copy.returncode != 0:
            shutil.rmtree(staging, ignore_errors=True)
            self._log(f"Copie échouée : {copy.stderr.strip()[:120]}")
            return

        # On échange en dernier, quand la nouvelle copie est complète : ainsi
        # une interruption ne laisse jamais /Applications sans application.
        try:
            shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)
        except OSError as exc:
            self._log(f"Remplacement impossible : {exc}")
            self._log(f"La version précédente reste dans {saved}")
            return

        self.bar.setValue(100)
        self._log(f"✓ WorkPlay {found_version} installé.")
        self._log("Redémarrage…")
        QTimer.singleShot(1200, lambda: self._relaunch(target))

    def _download(self, url: str, dest: Path, expected: int) -> bool:
        """Télécharge en affichant la progression, sans figer l'interface."""
        req = urllib.request.Request(
            url, headers={"User-Agent": f"WorkPlay/{APP_VERSION}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp, \
                    open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or expected or 0)
                read = 0
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    out.write(chunk)
                    read += len(chunk)
                    if total:
                        self.bar.setValue(min(99, int(read * 100 / total)))
                    QApplication.processEvents()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            self._log(f"Erreur de téléchargement : {exc}")
            return False
        return dest.is_file() and dest.stat().st_size > 0

    def _relaunch(self, app_path: Path) -> None:
        subprocess.Popen(["open", "-n", str(app_path)])
        QApplication.quit()

    # ---------------------------------------------------------- rollback --
    def rollback(self) -> None:
        """Restaure la sauvegarde la plus récente."""
        if self.busy:
            return
        saved = list_backups()
        target = installed_app_path()
        if not saved:
            self._log("Aucune version précédente conservée.")
            return
        if target is None:
            self._log("Restauration indisponible hors bundle .app.")
            return

        previous = saved[0]
        version = bundle_version(previous) or "?"
        self.busy = True
        try:
            self._log(f"Restauration de la version {version}…")
            staging = target.with_name("WorkPlay.app.rollback")
            shutil.rmtree(staging, ignore_errors=True)
            res = subprocess.run(
                ["ditto", str(previous), str(staging)],
                capture_output=True, text=True, timeout=900,
            )
            if res.returncode != 0:
                shutil.rmtree(staging, ignore_errors=True)
                self._log("Copie de la sauvegarde impossible.")
                return
            shutil.rmtree(target, ignore_errors=True)
            staging.rename(target)
            self._log(f"✓ Version {version} restaurée. Redémarrage…")
            QTimer.singleShot(1200, lambda: self._relaunch(target))
        except OSError as exc:
            self._log(f"Restauration impossible : {exc}")
        finally:
            self.busy = False
            self._refresh_rollback_button()

    # ------------------------------------------------------------ nettoyage --
    def _cleanup(self) -> None:
        if self.mount_point and self.mount_point.exists():
            subprocess.run(["hdiutil", "detach", str(self.mount_point),
                            "-quiet", "-force"],
                           capture_output=True, timeout=120)
        if self.dmg_path:
            shutil.rmtree(self.dmg_path.parent, ignore_errors=True)
        self.mount_point = None
        self.dmg_path = None

    def closeEvent(self, e) -> None:
        if self.busy:
            e.ignore()
            self.hide()
            return
        self._cleanup()
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
# Fenêtre vidéo
# --------------------------------------------------------------------------- #

class VideoWindow(QWidget):
    """Fenêtre d'image, volontairement simple.

    Elle partage le QMediaPlayer du widget : la même lecture alimente l'image
    et le son, il n'y a donc rien à synchroniser. L'audio reste géré par le
    lecteur principal (volume, position), cette fenêtre ne fait qu'afficher.

    Contrairement au widget, elle prend le focus : c'est une fenêtre que
    l'utilisateur ouvre volontairement pour regarder quelque chose.
    """

    closed = Signal()

    def __init__(self, path: Path, player: QMediaPlayer, parent=None):
        super().__init__(parent)
        self.path = Path(path)
        self.player = player

        self.setWindowTitle(path.stem)
        self.resize(960, 600)
        self.setStyleSheet("background-color: #0b0b0e;")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.video = QVideoWidget()
        self.video.setStyleSheet("background-color: #000000;")
        self.video.setMinimumSize(320, 180)
        # Proportions toujours respectées : Qt centre l'image et ajoute des
        # bandes noires plutôt que de rogner. C'est le comportement voulu.
        self.video.setAspectRatioMode(Qt.KeepAspectRatio)

        # PAS DE QScrollArea AUTOUR DU RENDU VIDÉO
        #   Sur macOS, QVideoWidget est une couche native : elle ignore le
        #   viewport d'une zone défilante et peint par-dessus toute la
        #   fenêtre. Cela masquait la barre de contrôle et affichait l'image à
        #   la mauvaise échelle — d'où « on ne voit qu'une zone, et plus de
        #   contrôles ». Le rendu vidéo va donc directement dans la mise en
        #   page, et le zoom agit sur la TAILLE DE LA FENÊTRE.
        root.addWidget(self.video, 1)

        # REMARQUE IMPORTANTE SUR L'ORDRE
        #   Attacher la sortie vidéo avant que le widget soit réalisé à
        #   l'écran laisse un écran noir : Qt crée alors un « sink » qui n'est
        #   relié à aucune surface native. Le symptôme est trompeur, car le son
        #   joue et l'état reste PlayingState sans la moindre erreur.
        #   On attache donc de nouveau dans showEvent(), une fois la fenêtre
        #   réellement affichée.
        self.player.setVideoOutput(self.video)

        # --- Barre de contrôle, masquée en plein écran -----------------------
        self.bar = QWidget()
        self.bar.setStyleSheet("background-color: rgba(16,16,20,235);")
        bar = QHBoxLayout(self.bar)
        bar.setContentsMargins(10, 7, 10, 7)
        bar.setSpacing(8)

        self.lbl = QLabel(path.stem)
        self.lbl.setStyleSheet("color: #f2f2f7; font-size: 12px;")
        bar.addWidget(self.lbl, 1)

        self.btn_play = QPushButton("❚❚")
        self.btn_play.setObjectName("Mini")
        self.btn_play.setFixedSize(26, 22)
        self.btn_play.clicked.connect(self.toggle_play)
        self.btn_play.setStyleSheet("color:#f2f2f7;background:transparent;border:none;")
        bar.addWidget(self.btn_play)

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setFixedSize(26, 22)
        self.btn_zoom_out.setToolTip("Réduire la fenêtre  (molette vers le bas)")
        self.btn_zoom_out.clicked.connect(lambda: self.zoom_by(1 / 1.25))
        self.btn_zoom_out.setStyleSheet("color:#f2f2f7;background:transparent;border:none;")
        bar.addWidget(self.btn_zoom_out)

        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setFixedSize(26, 22)
        self.btn_zoom_in.setToolTip("Agrandir la fenêtre  (molette vers le haut)")
        self.btn_zoom_in.clicked.connect(lambda: self.zoom_by(1.25))
        self.btn_zoom_in.setStyleSheet("color:#f2f2f7;background:transparent;border:none;")
        bar.addWidget(self.btn_zoom_in)

        self.lbl_zoom = QLabel("")
        self.lbl_zoom.setFixedWidth(46)
        self.lbl_zoom.setStyleSheet(
            "color:rgba(235,235,245,180);font-size:11px;"
        )
        bar.addWidget(self.lbl_zoom)

        self.btn_fit = QPushButton("⇱")
        self.btn_fit.setFixedSize(26, 22)
        self.btn_fit.setToolTip("Ajuster à la taille de la vidéo  (A)")
        self.btn_fit.clicked.connect(self.fit_video)
        self.btn_fit.setStyleSheet("color:#f2f2f7;background:transparent;border:none;")
        bar.addWidget(self.btn_fit)

        self.btn_full = QPushButton("⛶")
        self.btn_full.setFixedSize(26, 22)
        self.btn_full.setToolTip("Plein écran  (F)")
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        self.btn_full.setStyleSheet("color:#f2f2f7;background:transparent;border:none;")
        bar.addWidget(self.btn_full)

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(26, 22)
        self.btn_close.setToolTip("Fermer la vidéo  (Échap)")
        self.btn_close.clicked.connect(self.close)
        self.btn_close.setStyleSheet("color:#f2f2f7;background:transparent;border:none;")
        bar.addWidget(self.btn_close)

        root.addWidget(self.bar)

        # --- Barre de progression + temps ----------------------------------
        # Sans elle, une vidéo est impossible à piloter : on ne peut ni voir où
        # on en est, ni avancer, ni revenir en arrière.
        self.seek_row = QWidget()
        self.seek_row.setStyleSheet("background-color: rgba(16,16,20,235);")
        seek = QHBoxLayout(self.seek_row)
        seek.setContentsMargins(10, 0, 10, 7)
        seek.setSpacing(8)

        self.lbl_pos = QLabel("0:00")
        self.lbl_pos.setStyleSheet(
            "color:rgba(235,235,245,180);font-size:11px;"
        )
        self.lbl_pos.setFixedWidth(46)
        seek.addWidget(self.lbl_pos)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.setStyleSheet(
            "QSlider::groove:horizontal{height:4px;"
            "background:rgba(255,255,255,40);border-radius:2px;}"
            "QSlider::sub-page:horizontal{background:#7c5cff;border-radius:2px;}"
            "QSlider::handle:horizontal{width:12px;height:12px;margin:-4px 0;"
            "border-radius:6px;background:#f2f2f7;}"
        )
        self.slider.sliderPressed.connect(self._seek_start)
        self.slider.sliderReleased.connect(self._seek_end)
        self.slider.sliderMoved.connect(self._seek_move)
        seek.addWidget(self.slider, 1)

        self.lbl_dur = QLabel("0:00")
        self.lbl_dur.setStyleSheet(
            "color:rgba(235,235,245,180);font-size:11px;"
        )
        self.lbl_dur.setFixedWidth(46)
        self.lbl_dur.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        seek.addWidget(self.lbl_dur)

        root.addWidget(self.seek_row)

        # La fenêtre partage le lecteur du widget : on écoute les mêmes signaux
        # pour rester synchronisée, quel que soit l'endroit d'où on pilote.
        self._seeking = False
        player.positionChanged.connect(self._on_position)
        player.durationChanged.connect(self._on_duration)
        player.playbackStateChanged.connect(lambda _s: self._sync_button())

        # Raccourcis : la fenêtre a le focus, ils sont donc fiables.
        QShortcut(QKeySequence(Qt.Key_Space), self, self.toggle_play)
        QShortcut(QKeySequence(Qt.Key_F), self, self.toggle_fullscreen)
        QShortcut(QKeySequence(Qt.Key_A), self, self.fit_video)
        QShortcut(QKeySequence(Qt.Key_Escape), self, self._escape)
        QShortcut(QKeySequence(Qt.Key_Left), self, lambda: self.skip(-10_000))
        QShortcut(QKeySequence(Qt.Key_Right), self, lambda: self.skip(10_000))

        self.zoom = 1.0
        # Base de référence du zoom quand Qt ne connaît pas la taille vidéo.
        self._base_size = (960, 564)
        self._sync_button()

        # Bandeau d'avertissement, masqué par défaut.
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet(
            "background: rgba(255,138,76,225); color:#16161b;"
            "font-size:12px; font-weight:600; padding:8px 12px;"
        )
        self.warning.hide()
        root.insertWidget(0, self.warning)

    def warn(self, message: str) -> None:
        """Affiche un avertissement lisible au-dessus de l'image."""
        self.warning.setText(message)
        self.warning.show()

    # --------------------------------------------------------- progression --
    def _on_position(self, ms: int) -> None:
        if not self._seeking:
            self.slider.setValue(ms)
        self.lbl_pos.setText(format_time(ms))

    def _on_duration(self, ms: int) -> None:
        self.slider.setRange(0, ms)
        self.lbl_dur.setText(format_time(ms))

    def _seek_start(self) -> None:
        self._seeking = True

    def _seek_move(self, v: int) -> None:
        self.lbl_pos.setText(format_time(v))

    def _seek_end(self) -> None:
        self.player.setPosition(self.slider.value())
        self._seeking = False

    def skip(self, delta_ms: int) -> None:
        """Avance ou recule de quelques secondes (flèches ← →)."""
        pos = max(0, self.player.position() + delta_ms)
        dur = self.player.duration()
        if dur and pos > dur:
            pos = dur
        self.player.setPosition(pos)

    # ------------------------------------------------------------ contrôle --
    def showEvent(self, e) -> None:
        """Ré-attache la sortie vidéo une fois la fenêtre réalisée.

        C'est le correctif de l'écran noir : sans ce second attachement, le
        rendu reste sans surface native et n'affiche rien.
        """
        super().showEvent(e)
        self.player.setVideoOutput(self.video)
        # La barre doit rester au-dessus de la couche vidéo native.
        self.bar.raise_()
        # Dès que Qt annonce la taille de la vidéo, on ajuste la fenêtre.
        QTimer.singleShot(700, self._first_fit)

    def _first_fit(self, attempt: int = 0) -> None:
        """Ajuste la fenêtre dès que les dimensions vidéo sont connues.

        Qt ne les annonce pas toujours : on réessaie quelques fois, puis on
        ajuste malgré tout avec la base de référence. Sans cette limite, la
        boucle attendait éternellement et l'ajustement ne se faisait jamais.
        """
        if self.isFullScreen():
            return
        vw, _vh = self._source_size()
        if vw or attempt >= 5:
            self._apply_zoom()
        else:
            QTimer.singleShot(700, lambda: self._first_fit(attempt + 1))

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()
        self._sync_button()

    def _sync_button(self) -> None:
        playing = self.player.playbackState() == QMediaPlayer.PlayingState
        self.btn_play.setText("❚❚" if playing else "▶")

    def toggle_fit(self) -> None:
        """Redimensionne la fenêtre à la taille naturelle de la vidéo.

        L'image reste toujours entièrement visible : Qt la centre dans la zone
        disponible avec des bandes noires si besoin. « Ajuster » ne rogne
        jamais — c'est ce qui cassait l'affichage avant.
        """
        self.zoom = 1.0
        self._apply_zoom()

    def fit_video(self) -> None:
        """Alias lisible de l'ajustement."""
        self.toggle_fit()

    def zoom_by(self, factor: float) -> None:
        """Agrandit ou réduit en redimensionnant la FENÊTRE.

        On agit sur la fenêtre plutôt que sur le widget vidéo : la couche de
        rendu native de macOS ne se laisse pas recadrer dans un viewport, donc
        la seule façon fiable d'agrandir sans rogner est de faire grandir la
        fenêtre elle-même.
        """
        self.zoom = max(0.5, min(4.0, self.zoom * factor))
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        """Applique le facteur de zoom à la taille de la fenêtre.

        Qt n'annonce pas toujours les dimensions de la vidéo (videoSize peut
        rester nul). On garde donc une base de référence propre : la taille
        source si elle est connue, sinon la taille initiale de la fenêtre.
        Sans cela le zoom restait sans effet.
        """
        if self.isFullScreen():
            self.lbl_zoom.setText("plein écran")
            return

        bar_h = self.bar.height() if self.bar.isVisible() else 0
        bar_h += self.seek_row.height() if self.seek_row.isVisible() else 0
        vw, vh = self._source_size()
        if vw:
            base_w, base_h = vw, vh
            label_fit = f"{vw}×{vh}"
        else:
            base_w, base_h = self._base_size
            label_fit = "ajusté"

        self.resize(max(320, int(base_w * self.zoom)),
                    max(180, int(base_h * self.zoom)) + bar_h)
        self.lbl_zoom.setText(
            label_fit if abs(self.zoom - 1.0) < 0.01 else f"{self.zoom:.2f}×"
        )

    def _source_size(self) -> tuple[int, int]:
        """Dimensions réelles de la vidéo (0,0 si Qt ne les connaît pas encore).

        On ne retombe PAS sur la taille du widget : cela rendait toute mesure
        circulaire et masquait le problème d'échelle.
        """
        sink = self.player.videoSink()
        size = sink.videoSize() if sink else None
        if size is not None and size.isValid() and size.width() > 0:
            return size.width(), size.height()
        return 0, 0

    def wheelEvent(self, e) -> None:
        """Molette : agrandir / réduire la fenêtre."""
        delta = e.angleDelta().y()
        if delta > 0:
            self.zoom_by(1.25)
        elif delta < 0:
            self.zoom_by(1 / 1.25)
        else:
            super().wheelEvent(e)

    def toggle_fullscreen(self) -> None:
        """Plein écran aller/retour, avec barre de contrôle masquée."""
        if self.isFullScreen():
            self.showNormal()
            self.bar.show()
            self.seek_row.show()
            self.btn_full.setText("⛶")
            self._apply_zoom()
        else:
            self.showFullScreen()
            self.bar.hide()
            self.seek_row.hide()
            self.btn_full.setText("⤡")
            self.lbl_zoom.setText("plein écran")

    def _escape(self) -> None:
        """Échap quitte d'abord le plein écran, puis ferme la fenêtre."""
        if self.isFullScreen():
            self.toggle_fullscreen()
        else:
            self.close()

    def mouseDoubleClickEvent(self, e) -> None:
        self.toggle_fullscreen()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Right:
            self.player.setPosition(self.player.position() + 5000)
        elif e.key() == Qt.Key_Left:
            self.player.setPosition(max(0, self.player.position() - 5000))
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e) -> None:
        # Rend la sortie vidéo au lecteur audio : sans cela, le widget
        # continuerait de décoder vers une fenêtre détruite.
        self.player.setVideoOutput(None)
        self.closed.emit()
        super().closeEvent(e)


# --------------------------------------------------------------------------- #
# Dialogue « Télécharger une vidéo »
# --------------------------------------------------------------------------- #

class VideoDownloadDialog(QDialog):
    """Télécharge une vidéo YouTube, avec choix de la qualité.

    La qualité influe directement sur la taille du fichier : le sélecteur
    yt-dlp prend le meilleur flux compatible avec la limite choisie, puis
    ffmpeg fusionne l'image et le son en MP4.
    """

    finished_ok = Signal(Path)

    def __init__(self, video_dir: Path, parent=None):
        super().__init__(parent)
        self.video_dir = video_dir
        self.produced: Path | None = None

        self.setWindowTitle("Télécharger une vidéo")
        self.setMinimumWidth(560)
        self.setStyleSheet(STYLE)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel("Télécharger une vidéo")
        title.setObjectName("DlgTitle")
        root.addWidget(title)

        hint = QLabel(
            "Colle le lien de la vidéo, choisis la qualité, puis lance le "
            "téléchargement.\nLe fichier est déposé dans le dossier vidéo."
        )
        hint.setObjectName("DlgHint")
        root.addWidget(hint)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("https://www.youtube.com/watch?v=...")
        self.input.setFixedHeight(64)
        root.addWidget(self.input)

        # --- Qualité ---------------------------------------------------------
        qrow = QHBoxLayout()
        qrow.setSpacing(8)
        qlabel = QLabel("Qualité")
        qlabel.setObjectName("DlgHint")
        qrow.addWidget(qlabel)
        self.quality = QComboBox()
        self.quality.setStyleSheet(
            "QComboBox{background:rgba(255,255,255,20);color:#f2f2f7;"
            "border:1px solid rgba(255,255,255,45);border-radius:8px;"
            "padding:5px 10px;font-size:12px;}"
            "QComboBox QAbstractItemView{background:#1c1c22;color:#f2f2f7;"
            "selection-background-color:#ff8a4c;}"
        )
        for label, _selector in VIDEO_QUALITY_PRESETS:
            self.quality.addItem(label)
        qrow.addWidget(self.quality, 1)

        self.btn_formats = QPushButton("Voir les qualités réelles")
        self.btn_formats.setObjectName("Ghost")
        self.btn_formats.setToolTip(
            "Interroge la vidéo pour lister les résolutions disponibles"
        )
        self.btn_formats.clicked.connect(self.probe_formats)
        qrow.addWidget(self.btn_formats)
        root.addLayout(qrow)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        root.addWidget(self.bar)

        self.log = QPlainTextEdit()
        self.log.setObjectName("Log")
        self.log.setReadOnly(True)
        self.log.setFixedHeight(130)
        root.addWidget(self.log)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_open_folder = QPushButton("Ouvrir le dossier")
        self.btn_open_folder.setObjectName("Ghost")
        self.btn_open_folder.clicked.connect(
            lambda: os.system(f'open "{self.video_dir}"')
        )
        buttons.addWidget(self.btn_open_folder)

        self.btn_dl = QPushButton("Télécharger")
        self.btn_dl.setObjectName("Primary")
        self.btn_dl.clicked.connect(self.start)
        buttons.addWidget(self.btn_dl)
        root.addLayout(buttons)

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.setProcessEnvironment(process_env())
        self._prepare_dir(self.video_dir)
        self.proc.setWorkingDirectory(str(self.video_dir))
        self.proc.readyReadStandardOutput.connect(self._on_output)
        # Sans ce signal, un binaire introuvable ou un dossier de travail
        # invalide ne produit AUCUN message : le bouton semble simplement ne
        # rien faire. C'est exactement ce qui masquait le bug du dossier vidéo.
        self.proc.errorOccurred.connect(self._on_error)
        self.proc.finished.connect(self._on_finished)

        self.probe = QProcess(self)
        self.probe.setProcessChannelMode(QProcess.MergedChannels)
        self.probe.setProcessEnvironment(process_env())
        self.probe.readyReadStandardOutput.connect(self._on_probe_output)
        self.probe.errorOccurred.connect(self._on_error)
        self.probe.finished.connect(self._on_probe_finished)
        self._probe_lines: list[str] = []

    @staticmethod
    def _prepare_dir(path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _on_error(self, error) -> None:
        """Rend visible un échec de lancement, au lieu de le laisser muet."""
        reasons = {
            QProcess.FailedToStart: "le programme n'a pas pu démarrer",
            QProcess.Crashed: "le programme s'est interrompu",
            QProcess.Timedout: "délai dépassé",
            QProcess.WriteError: "erreur d'écriture",
            QProcess.ReadError: "erreur de lecture",
        }
        self._log(
            f"⚠︎ {reasons.get(error, 'erreur inconnue')} "
            f"(dossier : {self.video_dir})"
        )
        self.btn_dl.setEnabled(True)
        self.btn_formats.setEnabled(True)

        self.probe = QProcess(self)
        self.probe.setProcessChannelMode(QProcess.MergedChannels)
        self.probe.setProcessEnvironment(process_env())
        self.probe.finished.connect(self._on_probe_finished)

    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)
        self.log.moveCursor(QTextCursor.End)

    # ----------------------------------------------------------- formats ---
    def probe_formats(self) -> None:
        """Demande à yt-dlp les résolutions réellement disponibles."""
        url = self.input.toPlainText().strip().splitlines()
        url = url[0].strip() if url else ""
        if not URL_RE.match(url):
            self._log("Colle d'abord un lien valide.")
            return
        ytdlp = find_ytdlp()
        if ytdlp is None:
            self._log("yt-dlp introuvable.")
            return
        self.btn_formats.setEnabled(False)
        self._log("— Analyse des qualités disponibles —")
        self.probe.start(ytdlp, ["-F", url])

    def _on_probe_finished(self, code: int, _status) -> None:
        self.btn_formats.setEnabled(True)
        if code != 0:
            self._log("Analyse impossible (vidéo privée ou réseau ?).")
            return
        self._log("Résolutions proposées par la source :")
        seen = set()
        for line in self._probe_lines:
            m = re.match(r"\s*(\d+)\s+(\d+x\d+|audio only)", line)
            if m and m.group(2) not in seen:
                seen.add(m.group(2))
                self._log(f"    {m.group(1)}  →  {m.group(2)}")
        if not seen:
            self._log("    (aucune résolution lisible)")
        self._log("Choisis la qualité correspondante ci-dessus.")

    # -------------------------------------------------------- téléchargement --
    def start(self) -> None:
        url = self.input.toPlainText().strip().splitlines()
        url = url[0].strip() if url else ""
        if not URL_RE.match(url):
            self._log("Aucun lien valide.")
            return
        ytdlp = find_ytdlp()
        if ytdlp is None:
            self._log("yt-dlp introuvable — brew install yt-dlp ffmpeg")
            return
        if find_ffmpeg() is None:
            self._log("ffmpeg introuvable — nécessaire pour fusionner la vidéo.")
            return

        selector = VIDEO_QUALITY_PRESETS[self.quality.currentIndex()][1]
        self._log(f"↓ {self.quality.currentText()} — {url}")
        self.bar.setValue(0)
        self.btn_dl.setEnabled(False)
        self.produced = None
        self.proc.start(ytdlp, video_download_args(selector) + [url])

    def _on_output(self) -> None:
        chunk = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            m = PROGRESS_RE.search(line)
            if m:
                self.bar.setValue(int(float(m.group(1))))
            if line.startswith(("[download]", "[Merger]", "[ExtractAudio]",
                                "ERROR", "WARNING")):
                self._log(line[:150])

    def _on_probe_output(self) -> None:
        chunk = bytes(self.probe.readAllStandardOutput()).decode("utf-8", "replace")
        self._probe_lines.extend(chunk.splitlines())

    def _on_finished(self, code: int, _status) -> None:
        self.btn_dl.setEnabled(True)
        if code != 0:
            self._log(f"Échec (code {code}).")
            return
        self.bar.setValue(100)
        # Retrouve le fichier produit le plus récent du dossier.
        files = sorted(
            (p for p in self.video_dir.iterdir()
             if p.suffix.lower() in VIDEO_EXTS),
            key=lambda p: p.stat().st_mtime,
        )
        if files:
            self.produced = files[-1]
            self._log(f"Terminé : {self.produced.name}")
            self.finished_ok.emit(self.produced)

    def closeEvent(self, e) -> None:
        for proc in (self.proc, self.probe):
            if proc.state() != QProcess.NotRunning:
                e.ignore()
                self.hide()
                return
        super().closeEvent(e)


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

    def __init__(self, music_dirs: list[Path], download_dir: Path | None = None):
        super().__init__()
        self.music_dirs = [Path(d) for d in music_dirs]
        # Les dossiers vidéo sont initialisés avant le scan : la bibliothèque
        # unifiée lit tout ce qui est déclaré, audio et vidéo confondus.
        self.video_dirs: list[Path] = [VIDEO_DIR_DEFAULT]
        # La vidéo s'ouvre dans une fenêtre séparée par défaut — la plus
        # intuitive pour un widget compact qui ne montre pas l'image.
        self.video_separate_window: bool = True
        # Dossier d'atterrissage des téléchargements : par défaut le premier
        # dossier audio, pour que le morceau apparaisse aussitôt dans la liste.
        self.download_dir = download_dir or (
            self.music_dirs[0] if self.music_dirs else Path.home()
        )
        # Filtre de la liste : « musique seulement » permet de revenir à un
        # lecteur purement audio, sans les vidéos dans la liste.
        self.library_filter: str = FILTER_ALL
        self.tracks: list[Path] = scan_media(self.music_dirs + self.video_dirs)
        self.index = -1
        self._drag_offset: QPoint | None = None
        self._seeking = False
        self.expanded = False

        self.settings = QSettings("com.m5max", "WorkPlay")
        self.always_on_top = self.settings.value("always_on_top", False, type=bool)
        self.repeat = self.settings.value("repeat", REPEAT_OFF)
        if self.repeat not in REPEAT_ORDER:
            self.repeat = REPEAT_OFF
        self.playlists = PlaylistStore()
        self.current_playlist: str | None = None
        self.video_win: VideoWindow | None = None

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

        # Vérification des mises à jour, après le démarrage pour ne pas
        # retarder la lecture. Désactivable pour les tests.
        if not os.environ.get("WORKPLAY_NO_UPDATE_CHECK"):
            QTimer.singleShot(4000, self.check_updates_silently)

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

    def _persist(self) -> None:
        """Force l'écriture immédiate des réglages sur disque.

        QSettings met les écritures en tampon et ne les enregistre qu'à sa
        destruction. Comme l'application se ferme souvent par un quit() ou un
        kill, rien n'arrivait sur le disque : le fichier restait vide, et le
        volume, la position, le mode de répétition et les dossiers étaient
        perdus à chaque lancement.
        """
        self.settings.sync()

    def set_always_on_top(self, on: bool) -> None:
        self.always_on_top = bool(on)
        self.settings.setValue("always_on_top", self.always_on_top)
        self._persist()
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

        self.btn_repeat = QPushButton(REPEAT_LABEL[self.repeat][0])
        self.btn_repeat.setObjectName("Mini")
        self.btn_repeat.setFixedSize(24, 22)
        self.btn_repeat.setToolTip(REPEAT_LABEL[self.repeat][1] + "  (R)")
        self.btn_repeat.clicked.connect(self.cycle_repeat)

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
        row3.addWidget(self.btn_repeat)
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
        base = f"{n} piste{'s' if n > 1 else ''}"
        if self.current_playlist:
            return f"{base} · playlist « {self.current_playlist} »"
        return base

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
        self.act_folder = menu.addAction("Ouvrir le premier dossier musique")
        self.act_folder.triggered.connect(
            lambda: os.system(f'open "{self.music_dirs[0]}"') if self.music_dirs else None
        )

        self.act_choose = menu.addAction("Ajouter un dossier musique…")
        self.act_choose.triggered.connect(self.choose_music_dir)

        self.act_settings = menu.addAction("Réglages…")
        self.act_settings.triggered.connect(self.open_settings_dialog)

        menu.addSeparator()

        # --- Playlists -------------------------------------------------------
        self.act_new_pl = menu.addAction("Nouvelle playlist…")
        self.act_new_pl.triggered.connect(self.new_playlist_dialog)

        self.act_save_pl = menu.addAction("Enregistrer la liste affichée…")
        self.act_save_pl.triggered.connect(self.save_as_playlist_dialog)

        self.menu_playlists = menu.addMenu("Jouer une playlist")
        self.refresh_playlist_menu()

        # --- Filtre de la liste ----------------------------------------------
        self.menu_filter = menu.addMenu("Afficher dans la liste")
        self.filter_actions = {}
        for mode in (FILTER_ALL, FILTER_AUDIO, FILTER_VIDEO):
            act = self.menu_filter.addAction(FILTER_LABEL[mode])
            act.setCheckable(True)
            act.setChecked(self.library_filter == mode)
            act.triggered.connect(lambda _=False, m=mode: self.set_filter(m))
            self.filter_actions[mode] = act

        # --- Répétition ------------------------------------------------------
        menu.addSeparator()
        self.menu_repeat = menu.addMenu("Répétition")
        self.repeat_actions = {}
        for mode in REPEAT_ORDER:
            glyph, label = REPEAT_LABEL[mode]
            act = self.menu_repeat.addAction(f"{glyph}  {label}")
            act.setCheckable(True)
            act.setChecked(self.repeat == mode)
            act.triggered.connect(lambda _=False, m=mode: self.set_repeat(m))
            self.repeat_actions[mode] = act
        self.menu_repeat.addSeparator()
        self.menu_repeat.addAction("Mode suivant  (R)").triggered.connect(
            self.cycle_repeat
        )

        # --- Vidéo -----------------------------------------------------------
        menu.addSeparator()
        self.act_dl_video = menu.addAction("Télécharger une vidéo…")
        self.act_dl_video.triggered.connect(self.open_video_download)

        self.act_open_video = menu.addAction("Ouvrir une vidéo…")
        self.act_open_video.triggered.connect(self.open_video_picker)

        self.act_video_folder = menu.addAction("Ouvrir le premier dossier vidéo")
        self.act_video_folder.triggered.connect(
            lambda: os.system(f'open "{self.video_dirs[0]}"') if self.video_dirs else None
        )

        self.act_rescan = menu.addAction("Rescanner la playlist")
        self.act_rescan.triggered.connect(self.refresh_tracks)

        menu.addSeparator()

        self.act_top = menu.addAction("Toujours au premier plan")
        self.act_top.setCheckable(True)
        self.act_top.setChecked(self.always_on_top)
        self.act_top.triggered.connect(self.set_always_on_top)
        menu.addSeparator()

        self.act_update = menu.addAction("Rechercher une mise à jour…")
        self.act_update.triggered.connect(self.open_update_dialog)

        menu.addSeparator()

        act_quit = menu.addAction("Quitter")
        act_quit.triggered.connect(self.quit_app)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

        self.set_repeat(self.repeat)

    def refresh_tray(self) -> None:
        """Synchronise l'état des cases du menu avec l'état réel."""
        for mode, act in self.repeat_actions.items():
            act.setChecked(self.repeat == mode)

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
        self.dlg = DownloadDialog(
            self.music_dirs[0] if self.music_dirs else self.download_dir,
            self.download_dir, parent=None)
        self.dlg.finished_all.connect(self._after_downloads)
        self.vdlg = VideoDownloadDialog(
            self.video_dirs[0] if self.video_dirs else self.download_dir,
            parent=None)
        self.vdlg.finished_ok.connect(self._on_video_downloaded)
        self.udlg = UpdateDialog(parent=None)

    def open_update_dialog(self) -> None:
        self.udlg.show()
        self.udlg.raise_()
        self.udlg.activateWindow()   # action volontaire de l'utilisateur
        self.udlg.check(manual=True)

    def check_updates_silently(self) -> None:
        """Vérifie au démarrage et prévient seulement s'il y a du nouveau.

        Une notification discrète suffit : rien ne s'installe sans que vous
        l'ayez demandé, et l'absence de réseau passe inaperçue.
        """
        rel = fetch_latest_release()
        if rel is None or not is_newer(rel["version"], APP_VERSION):
            return
        self.tray.showMessage(
            f"WorkPlay {rel['version']} est disponible",
            "Barre de menus → Rechercher une mise à jour… pour l'installer.",
            QSystemTrayIcon.Information, 8000,
        )
        self.act_update.setText(
            f"Mettre à jour vers {rel['version']}…"
        )

    def open_video_download(self) -> None:
        self.vdlg.show()
        self.vdlg.raise_()
        self.vdlg.activateWindow()   # action volontaire de l'utilisateur

    def _on_video_downloaded(self, path: Path) -> None:
        """Propose de regarder la vidéo dès qu'elle est prête."""
        self.play_video(path)

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

    # --------------------------------------------------------- dossiers ----
    def choose_music_dir(self) -> None:
        """Ajoute un dossier musical à la bibliothèque."""
        chosen = QFileDialog.getExistingDirectory(
            None, "Choisir un dossier musique", str(self.music_dirs[0])
        )
        if chosen:
            self.change_music_dirs(self.music_dirs + [Path(chosen)])

    def change_music_dirs(self, dirs: list[Path]) -> None:
        """Remplace la liste audio, mémorise le choix, relance la lecture."""
        if dirs == self.music_dirs:
            return
        self.set_music_dirs(dirs)
        self.index = -1
        self.refresh_tracks(force=True)
        if self.tracks:
            self.play_index(0)
        else:
            self.player.stop()
            self.lbl_title.setText("Aucun fichier audio")
        self.lbl_artist.setText(self._count_label())

    def set_music_dirs(self, dirs: list[Path]) -> None:
        """Mémorise la liste des dossiers audio et recharge la bibliothèque."""
        self.music_dirs = [Path(d) for d in dirs]
        self.settings.setValue("music_dirs", json.dumps(
            [str(d) for d in self.music_dirs]))
        self._persist()
        # Le dialogue de téléchargement écrit dans le premier dossier audio.
        if self.music_dirs:
            self.dlg.music_dir = self.music_dirs[0]
            self.dlg.proc.setWorkingDirectory(str(self.music_dirs[0]))

    def set_video_separate_window(self, enabled: bool) -> None:
        """Ouvre la vidéo dans une fenêtre séparée (True) ou dans le widget."""
        self.video_separate_window = bool(enabled)
        self.settings.setValue("video_separate_window",
                               "1" if enabled else "0")
        self._persist()
        # Si on coupe la fenêtre pendant la lecture, on la referme proprement.
        if not enabled and self.video_win is not None:
            self.video_win.hide()

    def _after_downloads(self) -> None:
        """Après un lot : rapatrie les fichiers si les dossiers diffèrent,
        puis met la playlist à jour."""
        if self.music_dirs and self.download_dir != self.music_dirs[0]:
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
            dest = self.music_dirs[0] / src.name
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
        self._persist()
        self.dlg.download_dir = path
        self.dlg.proc.setWorkingDirectory(str(path))

    def set_video_dirs(self, dirs: list[Path]) -> None:
        """Change les dossiers vidéo et s'assure que le premier existe."""
        if dirs:
            try:
                dirs[0].mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
        self.video_dirs = [Path(d) for d in dirs]
        self.settings.setValue("video_dirs", json.dumps(
            [str(d) for d in self.video_dirs]))
        self._persist()
        if self.video_dirs:
            self.vdlg.video_dir = self.video_dirs[0]
            self.vdlg.proc.setWorkingDirectory(str(self.video_dirs[0]))

    def open_settings_dialog(self) -> None:
        dlg = SettingsDialog(
            self.music_dirs,
            self.video_dirs,
            self.video_separate_window,
            self.download_dir,
            parent=None,
        )
        dlg.applied.connect(lambda: self._apply_settings(dlg))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()   # action volontaire : l'utilisateur veut ce dialogue

    def _apply_settings(self, dlg: "SettingsDialog") -> None:
        # Les listes sont déjà recopiées par le dialogue avant l'émission.
        self.set_music_dirs(dlg.music_dirs)
        self.set_video_dirs(dlg.video_dirs)
        self.set_video_separate_window(dlg.video_separate_window)
        self.set_download_dir(Path(dlg.download_dir))
        self.index = -1
        self.refresh_tracks(force=True)
        self.lbl_artist.setText(self._count_label())
    # ---------------------------------------------------------- répétition --
    def set_repeat(self, mode: str) -> None:
        if mode not in REPEAT_ORDER:
            mode = REPEAT_OFF
        self.repeat = mode
        self.settings.setValue("repeat", mode)
        self._persist()
        glyph, tip = REPEAT_LABEL[mode]
        self.btn_repeat.setText(glyph)
        self.btn_repeat.setToolTip(tip + "  (R)")
        # Surligne le bouton quand un mode est actif.
        self.btn_repeat.setEnabled(True)
        active = mode != REPEAT_OFF
        self.btn_repeat.setStyleSheet(
            "color: #ff8a4c;" if active else ""
        )

    def cycle_repeat(self) -> None:
        i = REPEAT_ORDER.index(self.repeat)
        self.set_repeat(REPEAT_ORDER[(i + 1) % len(REPEAT_ORDER)])

    # ----------------------------------------------------------- playlists --
    def refresh_playlist_menu(self) -> None:
        """Reconstruit le sous-menu des playlists existantes."""
        self.menu_playlists.clear()
        names = self.playlists.names()
        if not names:
            empty = self.menu_playlists.addAction("(aucune playlist)")
            empty.setEnabled(False)
        for name in names:
            act = self.menu_playlists.addAction(name)
            act.triggered.connect(lambda _=False, n=name: self.load_playlist(n))
        self.menu_playlists.addSeparator()
        self.menu_playlists.addAction("Toute la bibliothèque").triggered.connect(
            lambda: self.load_library()
        )

    def load_library(self) -> None:
        """Revient à la bibliothèque complète, filtrée selon le choix affiché."""
        self.current_playlist = None
        self.tracks = self._scan_filtered()
        self._fill_list()
        self.lbl_artist.setText(self._count_label())
        if self.tracks:
            self.play_index(0)
        else:
            self.player.stop()
            self.lbl_title.setText("Aucun fichier")

    def _scan_filtered(self) -> list[Path]:
        """Tous les fichiers déclarés, restreints au filtre courant."""
        return [p for p in scan_media(self.music_dirs + self.video_dirs)
                if matches_filter(p, self.library_filter)]

    def set_filter(self, mode: str) -> None:
        """Bascule la liste sur un filtre : tout, musique seule, vidéos seules.

        En quittant le mode vidéo on ferme la fenêtre d'image : l'utilisateur
        revient à un lecteur audio, il ne doit pas rester une vidéo ouverte.
        """
        if mode not in FILTER_LABEL or mode == self.library_filter:
            return
        self.library_filter = mode
        self.settings.setValue("library_filter", mode)
        self._persist()
        for m, act in self.filter_actions.items():
            act.setChecked(m == mode)
        if mode != FILTER_VIDEO:
            self.close_video()
        # Une playlist explicite n'est pas concernée : c'est un choix de
        # l'utilisateur sur un contenu déjà fixé.
        if self.current_playlist:
            return
        self.index = -1
        self.refresh_tracks(force=True)
        if self.tracks and self.player.playbackState() != QMediaPlayer.PlayingState:
            self.play_index(0)
        elif not self.tracks:
            self.player.stop()
            self.lbl_title.setText("Aucun fichier")

    def load_playlist(self, name: str) -> None:
        """Charge une playlist nommée et démarre sa lecture."""
        resolved = resolve_playlist(self.playlists.load(name), self.music_dirs)
        if not resolved:
            self.dlg._log(f"Playlist « {name} » : aucun fichier trouvé.")
            return
        self.current_playlist = name
        self.tracks = resolved
        self.index = -1
        self._fill_list()
        self.lbl_artist.setText(self._count_label())
        self.play_index(0)

    def create_playlist(self, name: str, tracks: list[Path] | None = None) -> None:
        """Crée une playlist, éventuellement à partir de la liste affichée."""
        self.playlists.save(name, [p.name for p in (tracks or self.tracks)])
        self.refresh_playlist_menu()

    def new_playlist_dialog(self) -> None:
        name, ok = QInputDialog.getText(
            None, "Nouvelle playlist", "Nom de la playlist :"
        )
        if ok and name.strip():
            self.create_playlist(name.strip())

    def save_as_playlist_dialog(self) -> None:
        """Enregistre la liste affichée comme nouvelle playlist."""
        name, ok = QInputDialog.getText(
            None, "Enregistrer la liste",
            "Nom de la playlist (la liste actuellement affichée) :",
        )
        if ok and name.strip():
            self.create_playlist(name.strip())

    def add_current_to_playlist(self, name: str) -> None:
        """Ajoute le morceau en cours à une playlist existante."""
        if not (0 <= self.index < len(self.tracks)):
            return
        added = self.playlists.add_track(name, self.tracks[self.index].name)
        self.dlg._log(
            f"« {self.tracks[self.index].stem} » "
            + (f"ajouté à « {name} »." if added else f"déjà dans « {name} ».")
        )

    def remove_from_playlist(self) -> None:
        """Retire le morceau sélectionné de la playlist courante."""
        if not self.current_playlist or not (0 <= self.index < len(self.tracks)):
            return
        self.playlists.remove_track(self.current_playlist, self.tracks[self.index].name)
        self.load_playlist(self.current_playlist)

    def delete_playlist(self, name: str) -> None:
        self.playlists.delete(name)
        if self.current_playlist == name:
            self.load_library()
        self.refresh_playlist_menu()

    # ----------------------------------------------------------------- vidéo --
    def open_video(self, path: Path) -> None:
        """Ouvre la fenêtre vidéo sur un fichier."""
        if self.video_win is not None:
            self.video_win.close()
        self.video_win = VideoWindow(path, self.player, self)
        self.video_win.closed.connect(self._on_video_closed)
        self.video_win.show()
        # Force la création de la fenêtre native, puis ré-attache le rendu :
        # c'est ce qui fait la différence entre une image et un écran noir.
        self.video_win.video.winId()
        self.player.setVideoOutput(self.video_win.video)
        self.video_win.raise_()

    def close_video(self) -> None:
        if self.video_win is not None:
            self.video_win.close()
            self.video_win = None

    def _on_video_closed(self) -> None:
        self.video_win = None

    def list_videos(self) -> list[Path]:
        """Toutes les vidéos connues, tous dossiers confondus."""
        out: list[Path] = []
        for d in self.video_dirs:
            if not d.is_dir():
                continue
            out.extend(
                p for p in d.iterdir()
                if p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")
            )
        return sorted(out, key=lambda p: natural_key(p.name))

    def play_video(self, path: Path) -> None:
        """Bascule la lecture sur une vidéo.

        Si « fenêtre séparée » est activé, la fenêtre d'image est affichée
        AVANT la source pour que la première frame soit rendue ; sinon la vidéo
        joue dans le widget (son seul, pas d'image).
        """
        self.lbl_title.setText(path.stem)
        if self.video_separate_window:
            self.open_video(path)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def open_video_picker(self) -> None:
        """Choisit une vidéo parmi celles des dossiers déclarés."""
        videos = self.list_videos()
        if not videos:
            self.dlg._log(
                "Aucune vidéo dans les dossiers configurés. "
                "Utilise « Télécharger une vidéo… » ou ajoute un dossier."
            )
            return
        labels = [p.stem for p in videos]
        choice, ok = QInputDialog.getItem(
            None, "Ouvrir une vidéo", "Vidéo :", labels, 0, False
        )
        if ok and choice:
            self.play_video(videos[labels.index(choice)])

    # ------------------------------------------------------------ playlist --
    def refresh_tracks(self, force: bool = False) -> None:
        """Relit tous les dossiers et met la liste à jour sans couper la lecture."""
        playing = self.tracks[self.index].stem if 0 <= self.index < len(self.tracks) else None
        new = self._scan_filtered()
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
        self.player.mediaStatusChanged.connect(self._on_media_status_video)

    # ------------------------------------------------------------ lecture --
    def play_index(self, i: int) -> None:
        if not self.tracks:
            self.lbl_title.setText("Aucun fichier")
            return
        self.index = i % len(self.tracks)
        track = self.tracks[self.index]
        self.list.setCurrentRow(self.index)
        # Les vidéos partent sur leur propre chemin : elles ont besoin d'une
        # surface de rendu, que le widget compact ne fournit pas.
        if track.suffix.lower() in VIDEO_EXTS:
            self.play_video(track)
            self.lbl_artist.setText(
                f"{self.index + 1}/{len(self.tracks)} · vidéo"
            )
            return
        # Passage d'une vidéo à de la musique : on referme l'image, sinon la
        # fenêtre resterait ouverte sur un écran noir.
        self.close_video()
        self.player.setSource(QUrl.fromLocalFile(str(track)))
        self.player.play()
        self.lbl_title.setText(track.stem)
        self.lbl_artist.setText(f"{self.index + 1}/{len(self.tracks)}")

    def toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            if self.index < 0 and self.tracks:
                self.play_index(0)
            else:
                self.player.play()

    def next_track(self) -> None:
        self.advance(1)

    def prev_track(self) -> None:
        # Comportement habituel : revient au début si on est déjà avancé.
        if self.player.position() > 3000:
            self.player.setPosition(0)
        else:
            self.advance(-1)

    def advance(self, step: int) -> None:
        """Change de morceau en respectant le mode de répétition.

        C'est ici que se décident les trois comportements demandés :
        - « one »  : on rejoue le morceau courant, indéfiniment ;
        - « all »  : en bout de liste on repart au début (boucle) ;
        - « off »  : en bout de liste on s'arrête, sans revenir au début.
        Un geste manuel (bouton, flèche) reste toujours possible : seule la fin
        de liste change de comportement.
        """
        if not self.tracks:
            return

        if self.repeat == REPEAT_ONE and step > 0:
            # Rejouer le morceau courant depuis le début.
            self.player.setPosition(0)
            self.player.play()
            return

        last = len(self.tracks) - 1
        target = self.index + step

        if target > last:
            if self.repeat == REPEAT_ALL:
                target = 0
            else:
                # Fin de liste, sans boucle : on s'arrête proprement ici.
                self.player.stop()
                self.lbl_artist.setText("fin de la liste")
                return
        elif target < 0:
            target = last if self.repeat == REPEAT_ALL else 0

        self.play_index(target)

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

    def _watch_video_decode(self) -> None:
        """Détecte une vidéo que le moteur ne sait pas décoder.

        Le symptôme est trompeur : la lecture semble démarrer, le son joue,
        mais l'image reste noire et aucune erreur n'est signalée. Qt reste
        bloqué en BufferingMedia. C'est ce qui se produisait avec les vidéos
        AV1 ; mieux vaut le dire que laisser un écran noir.
        """
        if self.video_win is None:
            return
        if self.player.mediaStatus() == QMediaPlayer.BufferingMedia \
                and self.player.position() == 0 \
                and self.player.playbackState() == QMediaPlayer.PlayingState:
            codec = probe_video_codec(self.video_win.path)
            if codec and codec.lower() not in PLAYABLE_VIDEO_CODECS:
                self.video_win.warn(
                    f"Ce moteur ne décode pas la vidéo « {codec.upper()} ».\n"
                    "Télécharge la vidéo à nouveau : la qualité H.264 est "
                    "maintenant privilégiée."
                )

    def _on_media_status_video(self, status) -> None:
        if self.video_win is not None and status == QMediaPlayer.BufferingMedia:
            # Laisse une chance au tampon, puis conclut si rien n'arrive.
            QTimer.singleShot(4000, self._watch_video_decode)

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
        self._persist()
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
        self._persist()

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
        elif key == Qt.Key_R:
            self.cycle_repeat()
        elif key == Qt.Key_Escape:
            if self.expanded:
                self.btn_list.setChecked(False)
            else:
                self.hide()
        else:
            super().keyPressEvent(e)

    def quit_app(self) -> None:
        self.settings.setValue("pos", self.pos())
        self.player.stop()
        self.close_video()
        self._persist()
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
    """Vérifie qu'on peut ajouter un dossier et recharger la liste complète."""
    import tempfile
    original = list(w.music_dirs)
    tmp = Path(tempfile.mkdtemp(prefix="workplay-dir-"))
    try:
        # Un dossier vide ajouté ne casse rien et la liste reste celle d'avant.
        w.change_music_dirs(original + [tmp])
        added = tmp in w.music_dirs and len(w.tracks) >= 1
        w.change_music_dirs(original)
        restored = w.music_dirs == original and len(w.tracks) >= 1
        return (added and restored,
                f"ajout={'OK' if added else 'KO'} "
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
    """Le dialogue de réglages se construit et propose les dossiers."""
    dlg = SettingsDialog(w.music_dirs, w.video_dirs,
                         w.video_separate_window, w.download_dir)
    ok = (hasattr(dlg, "chk_same") and hasattr(dlg, "btn_dl_row")
          and hasattr(dlg, "folders_music") and hasattr(dlg, "folders_video")
          and dlg.music_dirs == w.music_dirs)
    dlg.deleteLater()
    return (ok, f"audio={len(dlg.music_dirs)} vidéo={len(dlg.video_dirs)}")


def _test_settings_apply(w) -> tuple[bool, str]:
    """Appliquer les réglages change réellement les listes de dossiers."""
    import tempfile
    original_music = list(w.music_dirs)
    original_video = list(w.video_dirs)
    original_dl = w.download_dir
    tmp_lib = Path(tempfile.mkdtemp(prefix="wp-lib-"))
    tmp_vid = Path(tempfile.mkdtemp(prefix="wp-vid-"))
    tmp_dl = Path(tempfile.mkdtemp(prefix="wp-dl-"))
    try:
        dlg = SettingsDialog([tmp_lib], [tmp_vid], False, tmp_dl)
        dlg.applied.connect(lambda: w._apply_settings(dlg))
        dlg.folders_music.paths = [tmp_lib]
        dlg.folders_video.paths = [tmp_vid]
        dlg.chk_separate.setChecked(False)
        dlg._accept()
        changed = (w.music_dirs == [tmp_lib] and w.video_dirs == [tmp_vid]
                   and w.download_dir == tmp_dl and not w.video_separate_window)
        # Retour à l'état initial.
        back = SettingsDialog(original_music, original_video,
                              True, original_dl)
        back.applied.connect(lambda: w._apply_settings(back))
        back.folders_music.paths = list(original_music)
        back.folders_video.paths = list(original_video)
        back.chk_separate.setChecked(True)
        back._accept()
        restored = (w.music_dirs == original_music
                    and w.video_dirs == original_video
                    and w.download_dir == original_dl
                    and w.video_separate_window)
        return (changed and restored,
                f"appliqué={'OK' if changed else 'KO'} "
                f"restauré={'OK' if restored else 'KO'}")
    finally:
        for d in (tmp_lib, tmp_vid, tmp_dl):
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
    original_music = list(w.music_dirs)
    original_tracks = list(w.tracks)
    original_index = w.index
    tmp_dl = Path(tempfile.mkdtemp(prefix="wp-collect-"))
    lib = Path(tempfile.mkdtemp(prefix="wp-dest-"))
    try:
        w.set_download_dir(tmp_dl)
        w.music_dirs = [lib]
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
        w.music_dirs = original_music
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


def _test_playlist_save() -> tuple[bool, str]:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-pl-"))
    try:
        store = PlaylistStore(tmp)
        store.save("Mes hits", ["a.mp3", "b.mp3"])
        ok = store.names() == ["Mes hits"] and store.load("Mes hits") == ["a.mp3", "b.mp3"]
        return (ok, f"écrit puis relu : {store.load('Mes hits')}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_playlist_load() -> tuple[bool, str]:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-pl-"))
    lib = Path(tempfile.mkdtemp(prefix="wp-lib-"))
    try:
        (lib / "x.mp3").write_bytes(b"ID3\x03\x00")
        (lib / "y.mp3").write_bytes(b"ID3\x03\x00")
        store = PlaylistStore(tmp)
        store.save("Deux", ["x.mp3", "y.mp3"])
        resolved = resolve_playlist(store.load("Deux"), [lib])
        return (len(resolved) == 2 and resolved[0].name == "x.mp3",
                f"{len(resolved)} chemins résolus")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(lib, ignore_errors=True)


def _test_playlist_dup() -> tuple[bool, str]:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-pl-"))
    try:
        store = PlaylistStore(tmp)
        first = store.add_track("L", "a.mp3")
        second = store.add_track("L", "a.mp3")
        return (first and not second,
                f"1er ajout={first} 2e ajout(doublon)={second}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_playlist_remove() -> tuple[bool, str]:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-pl-"))
    try:
        store = PlaylistStore(tmp)
        store.save("L", ["a.mp3", "b.mp3", "c.mp3"])
        store.remove_track("L", "b.mp3")
        left = store.load("L")
        return (left == ["a.mp3", "c.mp3"], f"restant={left}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_playlist_missing() -> tuple[bool, str]:
    """Un fichier déclaré mais absent ne doit pas casser la lecture."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-pl-"))
    lib = Path(tempfile.mkdtemp(prefix="wp-lib-"))
    try:
        (lib / "present.mp3").write_bytes(b"ID3\x03\x00")
        store = PlaylistStore(tmp)
        store.save("L", ["absent.mp3", "present.mp3"])
        resolved = resolve_playlist(store.load("L"), [lib])
        return (len(resolved) == 1 and resolved[0].name == "present.mp3",
                f"{len(resolved)}/2 fichier(s) trouvé(s), l'absent est ignoré")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(lib, ignore_errors=True)


def _test_playlist_play(w) -> tuple[bool, str]:
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-pl-"))
    try:
        # On ne prend que des audio : la playlist résout dans music_dirs.
        names = [p.name for p in w.tracks
                 if p.suffix.lower() in AUDIO_EXTS][:3]
        w.playlists.base = tmp
        w.playlists.save("Test", names)
        w.load_playlist("Test")
        ok = len(w.tracks) == len(names) and w.tracks[0].name == names[0]
        return (ok, f"liste jouée : {[p.stem for p in w.tracks]}")
    finally:
        w.playlists.base = PLAYLISTS_DIR
        w.load_library()
        shutil.rmtree(tmp, ignore_errors=True)


def _test_playlist_next(w) -> tuple[bool, str]:
    """En mode 'off', après le dernier morceau d'une playlist on s'arrête."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-pl-"))
    try:
        names = [p.name for p in w.tracks
                 if p.suffix.lower() in AUDIO_EXTS][:2]
        w.playlists.base = tmp
        w.playlists.save("Court", names)
        w.load_playlist("Court")
        w.set_repeat(REPEAT_OFF)
        w.play_index(1)                 # dernier morceau de la liste
        w.next_track()                  # ne doit pas repartir au début
        stayed = w.index == 1
        return (stayed, f"index après la fin={w.index} (attendu 1)")
    finally:
        w.playlists.base = PLAYLISTS_DIR
        w.set_repeat(REPEAT_OFF)
        w.load_library()
        shutil.rmtree(tmp, ignore_errors=True)


def _test_repeat_cycle(w) -> tuple[bool, str]:
    original = w.repeat
    try:
        seen = []
        for _ in range(4):
            w.cycle_repeat()
            seen.append(w.repeat)
        expected = [REPEAT_ONE, REPEAT_ALL, REPEAT_OFF, REPEAT_ONE]
        return (seen == expected, f"cycle : {seen}")
    finally:
        w.set_repeat(original)


def _test_repeat_persist(w) -> tuple[bool, str]:
    original = w.repeat
    try:
        w.set_repeat(REPEAT_ALL)
        saved = QSettings("com.m5max", "WorkPlay").value("repeat")
        w.set_repeat(REPEAT_OFF)
        stored = QSettings("com.m5max", "WorkPlay").value("repeat")
        return (saved == REPEAT_ALL and stored == REPEAT_OFF,
                f"écrit={saved} puis={stored}")
    finally:
        w.set_repeat(original)


def _test_repeat_one(w) -> tuple[bool, str]:
    """Mode 'one' : relancer le même morceau ne doit pas changer d'index."""
    original, idx = w.repeat, w.index
    try:
        w.set_repeat(REPEAT_ONE)
        w.play_index(1)
        before = w.index
        w.next_track()
        return (w.index == before, f"morceau répété (index={w.index})")
    finally:
        w.set_repeat(original)
        if idx >= 0:
            w.index = idx


def _test_repeat_all(w) -> tuple[bool, str]:
    """Mode 'all' : après le dernier morceau on revient au premier."""
    original, idx = w.repeat, w.index
    try:
        w.set_repeat(REPEAT_ALL)
        w.play_index(len(w.tracks) - 1)
        w.next_track()
        return (w.index == 0, f"index après le dernier = {w.index} (attendu 0)")
    finally:
        w.set_repeat(original)
        if idx >= 0:
            w.index = idx


def _test_video_args() -> tuple[bool, str]:
    """Chaque qualité proposée produit un sélecteur yt-dlp cohérent."""
    problems = []
    for label, selector in VIDEO_QUALITY_PRESETS:
        args = video_download_args(selector)
        if "-f" not in args or selector not in args:
            problems.append(label)
        if "--merge-output-format" not in args:
            problems.append(f"{label} (pas de fusion)")
    return (not problems,
            f"{len(VIDEO_QUALITY_PRESETS)} qualités, "
            f"problèmes={problems or 'aucun'}")


def _test_video_window(w) -> tuple[bool, str]:
    """La fenêtre vidéo s'ouvre, joue un fichier et se ferme."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-vid-"))
    try:
        # Un vrai petit MP4, fabriqué avec ffmpeg s'il est disponible.
        ff = find_ffmpeg()
        if ff is None:
            return (False, "ffmpeg absent")
        out = tmp / "test.mp4"
        proc = QProcess()
        proc.start(ff, ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                        "-t", "2", "-pix_fmt", "yuv420p", "-y", str(out)])
        proc.waitForFinished(60_000)
        if not out.exists():
            return (False, "génération du mp4 de test impossible")
        # Le test force le mode fenêtre pour vérifier le chemin d'ouverture.
        w.set_video_separate_window(True)
        w.open_video(out)
        opened = w.video_win is not None and w.video_win.isVisible()
        w.close_video()
        return (opened, f"ouverte={opened}, fermée={w.video_win is None}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_video_fullscreen(w) -> tuple[bool, str]:
    """Le plein écran est accessible et réversible."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-vid-"))
    try:
        ff = find_ffmpeg()
        if ff is None:
            return (False, "ffmpeg absent")
        out = tmp / "fs.mp4"
        proc = QProcess()
        proc.start(ff, ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                        "-t", "2", "-pix_fmt", "yuv420p", "-y", str(out)])
        proc.waitForFinished(60_000)
        if not out.exists():
            return (False, "génération du mp4 de test impossible")
        w.open_video(out)
        win = w.video_win
        win.toggle_fullscreen()
        entered = win.isFullScreen()
        win.toggle_fullscreen()
        left = not win.isFullScreen()
        w.close_video()
        return (entered and left, f"plein écran={entered}, retour={left}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_video_dir_creation(w) -> tuple[bool, str]:
    """Le dossier vidéo doit être créé même s'il n'existe pas au départ.

    C'est la cause du bug « le téléchargement vidéo ne marche pas » : un
    dossier de travail absent fait échouer le lancement de yt-dlp, sans aucun
    message. Ce test le vérifie explicitement.
    """
    import tempfile
    parent = Path(tempfile.mkdtemp(prefix="wp-vdir-"))
    target = parent / "sous" / "dossier"      # volontairement inexistant
    try:
        w.set_video_dirs([target])
        created = target.is_dir()
        wd = w.vdlg.proc.workingDirectory()
        ok = created and wd == str(target)
        return (ok, f"créé={created} dossier de travail={wd}")
    finally:
        w.set_video_dirs(w.settings.value("video_dirs",
                                          json.dumps([str(VIDEO_DIR_DEFAULT)]))
                        and [Path(d) for d in json.loads(w.settings.value("video_dirs"))]
                        or [VIDEO_DIR_DEFAULT])
        shutil.rmtree(parent, ignore_errors=True)


def _make_test_clip() -> tuple[Path | None, str]:
    """Génère un court mp4 H.264 jouable par Qt. Retourne (chemin, erreur)."""
    ff = find_ffmpeg()
    if ff is None:
        return None, "ffmpeg absent"
    tmp = Path(tempfile.mkdtemp(prefix="wp-clip-"))
    out = tmp / "clip.mp4"
    proc = QProcess()
    proc.start(ff, ["-y", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=15",
                    "-t", "2", "-pix_fmt", "yuv420p", str(out)])
    proc.waitForFinished(60_000)
    if not out.is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        return None, "génération du mp4 impossible"
    return out, ""


def _test_library_filter(w) -> tuple[bool, str]:
    """Le filtre « musique seulement » retire les vidéos de la liste."""
    import tempfile
    original_filter = w.library_filter
    original_dirs = list(w.video_dirs)
    clip = None
    tmp = Path(tempfile.mkdtemp(prefix="wp-filter-"))
    try:
        made, err = _make_test_clip()
        if made is None:
            return (False, err)
        clip = made.parent
        shutil.move(str(made), str(tmp / "film.mp4"))
        # La vidéo n'apparaît dans la liste que si son dossier est déclaré.
        w.video_dirs = [tmp]
        # set_filter() est un no-op si le mode ne change pas : on force le
        # rescan pour que la mesure porte bien sur les nouveaux dossiers.
        w.refresh_tracks(force=True)

        w.set_filter(FILTER_ALL)
        w.refresh_tracks(force=True)
        n_all = len(w.tracks)
        has_video = any(p.suffix.lower() in VIDEO_EXTS for p in w.tracks)

        w.set_filter(FILTER_AUDIO)
        only_audio = all(p.suffix.lower() in AUDIO_EXTS for p in w.tracks)
        n_audio = len(w.tracks)

        w.set_filter(FILTER_VIDEO)
        only_video = all(p.suffix.lower() in VIDEO_EXTS for p in w.tracks)
        n_video = len(w.tracks)

        ok = (has_video and only_audio and only_video
              and n_all == n_audio + n_video and n_video >= 1)
        return (ok, f"tout={n_all} audio={n_audio} vidéo={n_video}")
    finally:
        w.video_dirs = original_dirs
        w.set_filter(original_filter)
        w.refresh_tracks(force=True)
        shutil.rmtree(tmp, ignore_errors=True)
        if clip is not None:
            shutil.rmtree(clip, ignore_errors=True)


def _test_play_index_routes_video(w) -> tuple[bool, str]:
    """Jouer une piste vidéo passe par play_video, pas par le lecteur brut.

    C'est le bug qui rendait le lecteur vidéo « nul » : le double-clic sur une
    vidéo dans la liste chargeait le fichier sans jamais ouvrir l'image.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-route-"))
    original_dirs = list(w.video_dirs)
    original_filter = w.library_filter
    original_separate = w.video_separate_window
    try:
        made, err = _make_test_clip()
        if made is None:
            return (False, err)
        clip = tmp / "film.mp4"
        shutil.move(str(made), str(clip))
        shutil.rmtree(made.parent, ignore_errors=True)

        w.video_dirs = [tmp]
        w.set_filter(FILTER_ALL)
        w.refresh_tracks(force=True)
        if clip not in w.tracks:
            return (False, "la vidéo n'est pas dans la liste")

        # On intercepte play_video : c'est lui qui doit ouvrir l'image.
        calls: list[Path] = []
        original_play_video = w.play_video
        w.play_video = lambda p: calls.append(p)  # type: ignore[assignment]
        try:
            # play_video étant neutralisé, play_index ne touche plus à la
            # source : on la vide avant pour que le test ne mesure pas les
            # restes du test précédent (d'où son comportement erratique).
            w.player.stop()
            w.player.setSource(QUrl())
            w.play_index(w.tracks.index(clip))
        finally:
            w.play_video = original_play_video  # type: ignore[assignment]

        # Et le lecteur ne doit PAS être chargé directement avec la vidéo :
        # la source reste vide, puisque c'est play_video qui s'en occupe.
        src = w.player.source().toLocalFile()
        routed = calls == [clip]
        not_raw = src == ""
        ok = routed and not_raw
        return (ok, f"routé={routed} source_brute={'vide' if not_raw else src}")
    finally:
        w.player.stop()
        w.player.setSource(QUrl())
        w.video_dirs = original_dirs
        w.set_filter(original_filter)
        w.set_video_separate_window(original_separate)
        w.refresh_tracks(force=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _test_audio_after_video(w) -> tuple[bool, str]:
    """Revenir à la musique ferme la fenêtre vidéo : pas d'écran noir laissé."""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-back-"))
    original_dirs = list(w.video_dirs)
    original_filter = w.library_filter
    original_separate = w.video_separate_window
    try:
        made, err = _make_test_clip()
        if made is None:
            return (False, err)
        clip = tmp / "film.mp4"
        shutil.move(str(made), str(clip))
        shutil.rmtree(made.parent, ignore_errors=True)

        w.video_dirs = [tmp]
        w.set_video_separate_window(True)
        w.set_filter(FILTER_ALL)
        w.refresh_tracks(force=True)

        w.play_video(clip)
        opened = w.video_win is not None and w.video_win.isVisible()

        # Repasser sur un morceau doit refermer l'image.
        audio = next((p for p in w.tracks
                      if p.suffix.lower() in AUDIO_EXTS), None)
        if audio is None:
            return (False, "aucun morceau audio en bibliothèque")
        w.play_index(w.tracks.index(audio))
        closed = not (w.video_win is not None and w.video_win.isVisible())
        on_audio = w.player.source().toLocalFile() == str(audio)

        ok = opened and closed and on_audio
        return (ok, f"ouverte={opened} refermée={closed} "
                    f"audio={'OK' if on_audio else 'KO'}")
    finally:
        w.close_video()
        w.player.stop()
        w.video_dirs = original_dirs
        w.set_filter(original_filter)
        w.set_video_separate_window(original_separate)
        w.refresh_tracks(force=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _test_video_window_seek(w) -> tuple[bool, str]:
    """La fenêtre vidéo pilote la lecture : barre, temps, saut, raccourcis.

    On vérifie le câblage plutôt que le chargement média réel : ce dernier est
    asynchrone et dépend de l'état du lecteur, donc non déterministe dans une
    suite de tests. Ici chaque signal est injecté directement, ce qui prouve
    que l'interface réagit comme l'utilisateur l'attend.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-seek-"))
    try:
        ff = find_ffmpeg()
        if ff is None:
            return (False, "ffmpeg absent")
        out = tmp / "clip.mp4"
        proc = QProcess()
        proc.start(ff, ["-y", "-v", "error", "-f", "lavfi",
                        "-i", "testsrc=size=320x240:rate=15",
                        "-t", "2", "-pix_fmt", "yuv420p", str(out)])
        proc.waitForFinished(60_000)
        if not out.is_file():
            return (False, "génération du mp4 impossible")

        w.set_video_separate_window(True)
        w.play_video(out)
        win = w.video_win
        if win is None or not hasattr(win, "slider"):
            w.close_video()
            return (False, "fenêtre ou barre absente")

        # La durée annoncée pilote la plage du curseur et le label de droite.
        win._on_duration(125_000)
        dur_ok = (win.slider.maximum() == 125_000
                  and win.lbl_dur.text() == "2:05")

        # La position courante suit dans le curseur et le label de gauche.
        win._on_position(60_000)
        pos_ok = (win.slider.value() == 60_000
                  and win.lbl_pos.text() == "1:00")

        # Pendant un glisser, le curseur ne doit pas être écrasé par le lecteur.
        win._seek_start()
        win._on_position(10_000)
        drag_ok = (win._seeking and win.slider.value() == 60_000
                   and win.lbl_pos.text() == "0:10")
        win._seek_end()
        released_ok = not win._seeking

        # skip() doit calculer une nouvelle position bornée, sans déborder.
        seen: list[int] = []
        player = win.player
        original_set = player.setPosition
        original_pos = player.position
        original_dur = player.duration
        try:
            player.position = lambda: 5_000            # type: ignore
            player.duration = lambda: 125_000          # type: ignore
            player.setPosition = lambda ms: seen.append(ms)  # type: ignore
            win.skip(3_000)
            win.skip(-10_000)      # ne doit jamais passer sous zéro
            player.position = lambda: 124_000          # type: ignore
            win.skip(10_000)
        finally:
            player.setPosition = original_set          # type: ignore
            player.position = original_pos             # type: ignore
            player.duration = original_dur             # type: ignore
        # Attendu : 8000, 0 (borné), 125000 (borné à la durée).
        skip_ok = seen == [8_000, 0, 125_000]

        # Les flèches doivent être branchées sur la fenêtre.
        shortcuts = {str(sc.key().toString())
                     for sc in win.findChildren(QShortcut)}
        keys_ok = "Left" in shortcuts and "Right" in shortcuts

        w.close_video()
        checks = {"durée": dur_ok, "position": pos_ok, "glisser": drag_ok,
                  "relâcher": released_ok, "skip": skip_ok,
                  "flèches": keys_ok}
        ok = all(checks.values())
        details = " ".join(f"{k}={'OK' if v else 'KO'}"
                           for k, v in checks.items())
        return (ok, f"{details} positions={seen}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_video_separate_window(w) -> tuple[bool, str]:
    """La fenêtre vidéo peut être coupée : la lecture reste mais pas d'image."""
    original = w.video_separate_window
    try:
        w.set_video_separate_window(False)
        disabled = not w.video_separate_window
        w.set_video_separate_window(True)
        enabled = w.video_separate_window
        return (disabled and enabled,
                f"désactivé={disabled} réactivé={enabled}")
    finally:
        w.set_video_separate_window(original)


def _test_version_compare() -> tuple[bool, str]:
    """La comparaison doit être numérique, pas alphabétique."""
    cases = [
        ("1.3.0", "1.2.1", True),
        ("v1.2.10", "1.2.9", True),      # piege classique du tri texte
        ("1.2.1", "1.2.1", False),
        ("1.2.0", "1.2.1", False),
        ("2.0.0", "1.9.9", True),
    ]
    bad = [f"{a} vs {b}" for a, b, want in cases if is_newer(a, b) != want]
    return (not bad, f"{len(cases)} cas, échecs={bad or 'aucun'}")


def _test_fetch_release() -> tuple[bool, str]:
    """L'API GitHub répond et expose un .dmg téléchargeable."""
    rel = fetch_latest_release()
    if rel is None:
        return (False, "API injoignable (réseau ?)")
    has_dmg = bool(rel.get("dmg") and rel["dmg"].get("url"))
    return (bool(rel.get("version")) and has_dmg,
            f"dernière={rel['version']} dmg={rel['dmg']['name'] if has_dmg else 'absent'}")


def _test_backup() -> tuple[bool, str]:
    """Archiver un bundle doit produire une copie complète et lisible."""
    global BACKUP_DIR
    original = BACKUP_DIR
    tmp = Path(tempfile.mkdtemp(prefix="wp-bk-"))
    try:
        BACKUP_DIR = tmp / "versions"
        fake = tmp / "WorkPlay.app"
        (fake / "Contents").mkdir(parents=True)
        (fake / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            '<key>CFBundleShortVersionString</key><string>9.9.9</string>'
            '</dict></plist>\n', encoding="utf-8"
        )
        (fake / "Contents" / "marqueur.txt").write_text("contenu", encoding="utf-8")
        saved = backup_current(fake)
        ok = (saved is not None and saved.is_dir()
              and (saved / "Contents" / "marqueur.txt").is_file()
              and "9.9.9" in saved.name)
        return (ok, f"archive={saved.name if saved else 'aucune'}")
    finally:
        BACKUP_DIR = original
        shutil.rmtree(tmp, ignore_errors=True)


def _test_prune() -> tuple[bool, str]:
    """Seules les N sauvegardes les plus récentes sont conservées."""
    global BACKUP_DIR
    original = BACKUP_DIR
    tmp = Path(tempfile.mkdtemp(prefix="wp-pr-"))
    try:
        BACKUP_DIR = tmp / "versions"
        BACKUP_DIR.mkdir(parents=True)
        import time
        for i in range(6):
            d = BACKUP_DIR / f"WorkPlay-1.0.{i}-2026.app"
            d.mkdir()
            (d / "x").write_text(str(i), encoding="utf-8")
            time.sleep(0.02)          # horodatages distincts
        removed = prune_backups(keep=3)
        left = list_backups()
        return (len(left) == 3 and removed == 3,
                f"{removed} purgée(s), {len(left)} conservée(s)")
    finally:
        BACKUP_DIR = original
        shutil.rmtree(tmp, ignore_errors=True)


def _test_verify_rejects() -> tuple[bool, str]:
    """Un bundle non signé doit être REFUSÉ.

    C'est le test de sécurité le plus important : sans ce refus, l'updater
    installerait n'importe quel fichier téléchargé.
    """
    tmp = Path(tempfile.mkdtemp(prefix="wp-fake-"))
    try:
        fake = tmp / "WorkPlay.app"
        (fake / "Contents" / "MacOS").mkdir(parents=True)
        (fake / "Contents" / "MacOS" / "WorkPlay").write_text(
            "#!/bin/sh\necho pirate\n", encoding="utf-8"
        )
        ok, detail = verify_bundle(fake)
        # Le résultat ATTENDU est un refus.
        return (not ok, f"refusé comme prévu : {detail[:70]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_verify_accepts() -> tuple[bool, str]:
    """Le bundle réellement installé doit passer la vérification."""
    installed = Path("/Applications/WorkPlay.app")
    if not installed.is_dir():
        return (True, "aucune app installée — test sans objet")
    ok, detail = verify_bundle(installed)
    return (ok, detail[:80])


def _test_version_sync() -> tuple[bool, str]:
    """APP_VERSION doit correspondre à la version du spec de build.

    Sans ce contrôle, l'application pourrait annoncer une version différente
    de celle du bundle et se croire éternellement à jour (ou l'inverse).
    """
    spec = Path(__file__).parent / "workplay.spec"
    if not spec.is_file():
        return (True, "spec absent — test sans objet")
    found = re.search(
        r'CFBundleShortVersionString"\s*:\s*"([^"]+)"',
        spec.read_text(encoding="utf-8"),
    )
    spec_version = found.group(1) if found else "?"
    return (spec_version == APP_VERSION,
            f"code={APP_VERSION} spec={spec_version}")


def _test_video_zoom(w) -> tuple[bool, str]:
    """Le zoom agrandit l'image SANS la rogner, et l'ajustement la remet.

    C'est le correctif du « on ne voit qu'une partie de la vidéo » :
    KeepAspectRatioByExpanding remplissait la fenêtre en coupant les bords.
    """
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="wp-zoom-"))
    try:
        ff = find_ffmpeg()
        if ff is None:
            return (False, "ffmpeg absent")
        out = tmp / "z.mp4"
        proc = QProcess()
        proc.start(ff, ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=15",
                        "-t", "3", "-pix_fmt", "yuv420p", "-y", str(out)])
        proc.waitForFinished(60_000)
        if not out.exists():
            return (False, "génération du mp4 impossible")
        w.open_video(out)
        win = w.video_win
        win.setGeometry(100, 100, 800, 500)
        for _ in range(60):                     # laisse Qt annoncer la taille
            QApplication.processEvents()
        # 1. Le mode de rendu ne doit jamais rogner.
        no_crop = win.video.aspectRatioMode() != Qt.KeepAspectRatioByExpanding
        # 2. La barre de contrôle doit être visible ET avoir une hauteur.
        bar_ok = win.bar.isVisible() and win.bar.height() > 10
        # 3. Le rendu vidéo ne doit pas dépasser la fenêtre : c'est ce qui
        #    faisait disparaître la barre sous la couche vidéo native.
        fits = (win.video.height() + win.bar.height()) <= win.height() + 2
        # 4. Le zoom doit changer la taille de la fenêtre.
        before = win.width()
        win.zoom_by(1.25)
        for _ in range(30):
            QApplication.processEvents()
        grew = win.width() > before or win._source_size() == (0, 0)
        w.close_video()
        ok = no_crop and bar_ok and fits and grew
        return (ok, f"sans rognage={no_crop} barre visible={bar_ok} "
                    f"vidéo+barre tiennent dans la fenêtre={fits} zoom={grew}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _test_video_codec(w) -> tuple[bool, str]:
    """Le fichier téléchargé doit avoir un codec que Qt sait décoder.

    Ce test comble le trou qui a laissé passer l'écran noir : les tests
    précédents vérifiaient que la vidéo *se télécharge*, jamais qu'elle
    *se décode*. Une vidéo AV1 se télécharge parfaitement et n'affiche rien.
    """
    import tempfile
    url = os.environ.get("WORKPLAY_TEST_URL")
    if not url:
        return (False, "aucune URL de test fournie")
    tmp = Path(tempfile.mkdtemp(prefix="wp-codec-"))
    original = list(w.video_dirs)
    try:
        w.set_video_dirs([tmp])
        dlg = w.vdlg
        dlg.input.setPlainText(url)
        dlg.quality.setCurrentIndex(4)          # 360p : le plus rapide
        dlg.start()
        if not dlg.proc.waitForFinished(180_000):
            dlg.proc.kill()
            return (False, "délai dépassé")
        for _ in range(20):
            QApplication.processEvents()
            if dlg.produced is not None:
                break
        if dlg.produced is None:
            return (False, "aucun fichier produit")
        codec = probe_video_codec(dlg.produced)
        ok = codec is not None and codec.lower() in PLAYABLE_VIDEO_CODECS
        return (ok, f"codec vidéo = {codec} "
                    f"({'décodable' if ok else 'NON décodable par Qt'})")
    finally:
        w.set_video_dirs(original)
        shutil.rmtree(tmp, ignore_errors=True)


def _test_video_download(w) -> tuple[bool, str]:
    """Télécharge une vraie vidéo en passant par le dialogue de l'interface.

    Le test précédent ne vérifiait que les arguments yt-dlp, ce qui laissait
    passer un dossier de travail invalide : ici on exerce le chemin complet.
    """
    import tempfile
    url = os.environ.get("WORKPLAY_TEST_URL")
    if not url:
        return (False, "aucune URL de test fournie")
    tmp = Path(tempfile.mkdtemp(prefix="wp-vdl-"))
    original = list(w.video_dirs)
    try:
        w.set_video_dirs([tmp])
        dlg = w.vdlg
        dlg.input.setPlainText(url)
        dlg.quality.setCurrentIndex(4)          # 360p : le plus rapide
        dlg.start()
        if not dlg.proc.waitForFinished(180_000):
            dlg.proc.kill()
            return (False, "délai dépassé")
        # Laisse le signal de fin traiter le résultat.
        for _ in range(20):
            QApplication.processEvents()
            if dlg.produced is not None:
                break
        produced = dlg.produced
        size_mb = (produced.stat().st_size / 1048576) if produced else 0
        ok = produced is not None and produced.suffix.lower() in VIDEO_EXTS and size_mb > 0.05
        return (ok,
                f"{produced.name if produced else 'aucun fichier'} "
                f"({size_mb:.1f} Mo, code={dlg.proc.exitCode()})")
    finally:
        w.set_video_dirs(original)
        shutil.rmtree(tmp, ignore_errors=True)


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
        ("always on top désactivé par défaut", lambda: (
            not w.always_on_top, f"valeur={w.always_on_top}")),
        ("always on top activable via le menu", lambda: (
            (w.set_always_on_top(True),
             bool(w.windowFlags() & Qt.WindowStaysOnTopHint))[1], "drapeau posé")),
        ("always on top désactivable", lambda: (
            (w.set_always_on_top(False),
             not (w.windowFlags() & Qt.WindowStaysOnTopHint))[1], "drapeau retiré")),
        ("vidéo : zoom sans rognage", lambda: _test_video_zoom(w)),
        # --- mises à jour ----------------------------------------------------
        ("maj : comparaison de versions", lambda: _test_version_compare()),
        ("maj : release GitHub lisible", lambda: _test_fetch_release()),
        ("maj : sauvegarde de la version en place", lambda: _test_backup()),
        ("maj : purge des vieilles sauvegardes", lambda: _test_prune()),
        ("maj : bundle non signé REFUSÉ", lambda: _test_verify_rejects()),
        ("maj : bundle installé accepté", lambda: _test_verify_accepts()),
        ("maj : version du code = version du bundle", lambda: _test_version_sync()),
        ("maj : dialogue construit", lambda: (
            w.udlg is not None and hasattr(w.udlg, "btn_install"),
            f"version annoncée = {APP_VERSION}")),
        ("tray icon présent", lambda: (
            w.tray is not None and w.tray.isVisible(), "icône affichée")),
        ("menu tray complet", lambda: (
            len(w.tray.contextMenu().actions()) >= 8,
            f"{len(w.tray.contextMenu().actions())} entrées")),
        ("dossiers musique configurables", lambda: (
            hasattr(w, "choose_music_dir") and hasattr(w, "change_music_dirs")
            and hasattr(w, "set_music_dirs"),
            f"{len(w.music_dirs)} dossier(s) : {w.music_dirs}")),
        ("changement de dossier", lambda: _test_change_dir(w)),
        # --- playlists -------------------------------------------------------
        ("playlist : création et sauvegarde", lambda: _test_playlist_save()),
        ("playlist : chargement", lambda: _test_playlist_load()),
        ("playlist : doublon refusé", lambda: _test_playlist_dup()),
        ("playlist : suppression d'un morceau", lambda: _test_playlist_remove()),
        ("playlist : fichier manquant ignoré", lambda: _test_playlist_missing()),
        ("playlist : jouer la liste", lambda: _test_playlist_play(w)),
        ("playlist : prochain morceau suit la liste", lambda: _test_playlist_next(w)),
        # --- répétition ------------------------------------------------------
        ("répétition : cycle des 3 modes", lambda: _test_repeat_cycle(w)),
        ("répétition : mode conservé entre sessions", lambda: _test_repeat_persist(w)),
        ("répétition : morceau rejoué en mode one", lambda: _test_repeat_one(w)),
        ("répétition : boucle en mode all", lambda: _test_repeat_all(w)),
        # --- vidéo -----------------------------------------------------------
        ("vidéo : qualité → sélecteur yt-dlp", lambda: _test_video_args()),
        ("vidéo : fenêtre de lecture", lambda: _test_video_window(w)),
        ("vidéo : plein écran accessible", lambda: _test_video_fullscreen(w)),
        ("vidéo : dossier absent créé à la volée", lambda: _test_video_dir_creation(w)),
        ("vidéo : fenêtre séparée désactivable", lambda: _test_video_separate_window(w)),
        ("liste : filtre musique/vidéo/tout", lambda: _test_library_filter(w)),
        ("liste : la vidéo part vers la fenêtre", lambda: _test_play_index_routes_video(w)),
        ("retour musique ferme la fenêtre vidéo", lambda: _test_audio_after_video(w)),
        ("vidéo : barre de progression + temps", lambda: _test_video_window_seek(w)),
        ("vidéo : codec décodable par Qt", lambda: _test_video_codec(w)),
        ("vidéo : téléchargement réel via le dialogue", lambda: _test_video_download(w)),
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

    def _load_dirs(key: str, fallback: Path) -> list[Path]:
        """Lit une liste JSON ; si vide ou invalide, retombe sur le défaut."""
        raw = prefs.value(key)
        if raw:
            try:
                dirs = json.loads(raw)
                if isinstance(dirs, list):
                    return [Path(d).expanduser() for d in dirs]
            except (json.JSONDecodeError, TypeError):
                pass
        # Compatibilité : l'ancienne clé unique est convertie en liste.
        old = prefs.value(key.replace("_dirs", "_dir"))
        if old:
            return [Path(old).expanduser()]
        return [fallback]

    music_dirs = _load_dirs("music_dirs", MUSIC_DIR_DEFAULT)
    video_dirs = _load_dirs("video_dirs", VIDEO_DIR_DEFAULT)

    # Le dossier de téléchargement reste unique : c'est le point d'atterrissage.
    saved_dl = prefs.value("download_dir")
    download_dir = Path(saved_dl).expanduser() if saved_dl else (
        music_dirs[0] if music_dirs else Path.home()
    )

    # Au premier lancement on crée les dossiers au lieu d'échouer.
    for d in set(music_dirs + [download_dir] + video_dirs):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"Impossible de créer {d} : {exc}", file=sys.stderr)
            return 1

    w = Player(music_dirs, download_dir)
    w.set_video_dirs(video_dirs)
    w.set_video_separate_window(
        prefs.value("video_separate_window", "1") == "1"
    )
    # Le filtre mémorisé est restauré sans recharger : la liste est déjà
    # construite selon lui par _scan_filtered au prochain refresh.
    saved_filter = prefs.value("library_filter", FILTER_ALL)
    if saved_filter in FILTER_LABEL:
        w.library_filter = saved_filter
        for m, act in w.filter_actions.items():
            act.setChecked(m == saved_filter)
        w.refresh_tracks(force=True)
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
