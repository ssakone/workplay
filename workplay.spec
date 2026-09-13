# -*- mode: python ; coding: utf-8 -*-
"""Spécification PyInstaller pour WorkPlay.

Objectif : un bundle .app autonome (aucun Python à installer côté utilisateur).
On exclut agressivement les modules Qt inutilisés — PySide6 embarque sinon
plusieurs centaines de Mo de bibliothèques (WebEngine, QML, 3D…) dont un
lecteur audio n'a aucun besoin.
"""

from pathlib import Path

APP_NAME = "WorkPlay"
BUNDLE_ID = "com.m5max.workplay"
ICON = str(Path("assets/AppIcon.icns").resolve())

# Modules Qt à écarter : rien de tout cela n'est importé par app.py.
EXCLUDES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuick3D", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtGraphs", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSerialPort", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtRemoteObjects",
    "PySide6.QtSensors", "PySide6.QtTextToSpeech", "PySide6.QtVirtualKeyboard",
    # Bibliothèques scientifiques / inutiles ici
    "tkinter", "unittest", "pydoc", "doctest", "test",
    "numpy", "scipy", "matplotlib", "pandas", "PIL",
]

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[("assets/AppIcon.icns", "assets")],
    hiddenimports=["PySide6.QtMultimedia"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX casse la signature de code sur macOS
    console=False,           # app GUI : pas de terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,  # on signe nous-mêmes après le build
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon=ICON,
    bundle_identifier=BUNDLE_ID,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
        "NSHumanReadableCopyright": "MIT",
        "NSMicrophoneUsageDescription": "Aucun enregistrement — lecture seule.",
    },
)
