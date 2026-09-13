#!/bin/bash
# Construit et signe WorkPlay.app.
#
# Usage :
#   ./tools/build.sh                          # signature adhoc (par défaut)
#   ./tools/build.sh "Apple Development: ..." # signature avec une identité
#
# Une identité de signature n'est JAMAIS stockée dans le dépôt : passe-la en
# argument. Sans argument, le bundle est signé adhoc — suffisant pour un usage
# local, mais Gatekeeper le refusera sur une autre machine.
set -euo pipefail

cd "$(dirname "$0")/.."

IDENTITY="${1:--}"
APP="dist/WorkPlay.app"

echo "==> Nettoyage"
rm -rf build dist

echo "==> Build PyInstaller"
./.venv/bin/pyinstaller workplay.spec --noconfirm --clean

echo "==> Signature ($IDENTITY)"
if [ "$IDENTITY" = "-" ]; then
    codesign --deep --force --options runtime --sign - "$APP"
else
    codesign --deep --force --options runtime --timestamp \
        --entitlements assets/entitlements.plist --sign "$IDENTITY" "$APP"
fi

echo "==> Vérification"
codesign --verify --deep --strict --verbose=2 "$APP"

echo
echo "Terminé : $APP ($(du -sh "$APP" | cut -f1))"
echo
echo "Note Gatekeeper : sans certificat « Developer ID » payant, macOS refuse"
echo "l'ouverture par double-clic. Contournement : clic droit > Ouvrir, ou"
echo "  xattr -d com.apple.quarantine \"$APP\""
