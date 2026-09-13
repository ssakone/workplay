#!/usr/bin/env bash
#
# Signe WorkPlay avec un certificat "Developer ID Application", le notarise
# auprès d'Apple, agrafe le ticket, puis fabrique les artefacts de release.
#
# WHY BOTH A ZIP AND A DMG
#   Le ZIP est ce que les utilisateurs décompressent ; le DMG est ce qu'ils
#   montent. Les deux doivent porter un ticket de notarisation agrafé, sinon
#   Gatekeeper refuse l'ouverture hors ligne sur une autre machine.
#
# PREREQUIS
#   - Un trousseau contenant le certificat Developer ID :
#       tools/sign-keychain.sh create
#   - Une clé API App Store Connect + son issuer (notarisation).
#
# Usage :
#   tools/release.sh 1.0.0
#
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"

# Configuration locale (certificat, clé API, issuer) : jamais dans le dépôt.
[ -f .env.local ] && . ./.env.local

VERSION="${1:-1.0.0}"
APP="dist/WorkPlay.app"
KEYCHAIN="${WORKPLAY_KEYCHAIN:-$HOME/Library/Keychains/workplay-signing.keychain-db}"
P12="${WORKPLAY_P12:?WORKPLAY_P12 manquant — copie .env.local.example en .env.local}"
P12_PASS_FILE="${WORKPLAY_P12_PASS:?WORKPLAY_P12_PASS manquant}"
API_KEY="${WORKPLAY_API_KEY:?WORKPLAY_API_KEY manquant}"
API_KEY_ID="${WORKPLAY_API_KEY_ID:?WORKPLAY_API_KEY_ID manquant}"
ISSUER="${WORKPLAY_API_ISSUER:?WORKPLAY_API_ISSUER manquant}"
KEYCHAIN_PASS_FILE="$KEYCHAIN.pass"

say() { printf '\033[1m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 0. Vérifications préalables
# --------------------------------------------------------------------------- #
[ -f "$KEYCHAIN" ] || die "trousseau introuvable. Lance d'abord : tools/sign-keychain.sh create"
security unlock-keychain -p "$(cat "$KEYCHAIN_PASS_FILE")" "$KEYCHAIN"

IDENTITY="$(security find-identity -v -p codesigning "$KEYCHAIN" \
            | grep "Developer ID Application" | head -1 \
            | sed -E 's/.*"(.*)"/\1/')"
[ -n "$IDENTITY" ] || die "aucun certificat 'Developer ID Application' dans $KEYCHAIN"

say "Identité : $IDENTITY"
say "Version  : $VERSION"
say "Issuer   : ${ISSUER:0:8}…"

mkdir -p dist/release
rm -f dist/release/*

# --------------------------------------------------------------------------- #
# 1. Signature de l'application
# --------------------------------------------------------------------------- #
say "Signature de $APP"
[ -d "$APP" ] || die "$APP absent — lance d'abord : ./tools/build.sh"
codesign --force --deep --options runtime --timestamp \
  --entitlements assets/entitlements.plist \
  --sign "$IDENTITY" --keychain "$KEYCHAIN" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

# --------------------------------------------------------------------------- #
# 2. Notarisation de l'application (via un zip)
# --------------------------------------------------------------------------- #
ZIP="dist/release/WorkPlay-$VERSION.zip"
say "Archive ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

say "Soumission de l'app à Apple (peut prendre plusieurs minutes)"
xcrun notarytool submit "$ZIP" \
  --key "$API_KEY" --key-id "$API_KEY_ID" --issuer "$ISSUER" \
  --wait --timeout 30m

say "Agrafage du ticket sur l'app"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"

# Le zip doit être refait APRÈS l'agrafage, sinon il ne contient pas le ticket.
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

# --------------------------------------------------------------------------- #
# 3. DMG
# --------------------------------------------------------------------------- #
DMG="dist/release/WorkPlay-$VERSION.dmg"
say "Création du DMG"
hdiutil create -volname "WorkPlay" -srcfolder "$APP" \
  -ov -format UDZO -quiet "$DMG"

say "Signature du DMG"
codesign --force --timestamp --sign "$IDENTITY" --keychain "$KEYCHAIN" "$DMG"

say "Soumission du DMG à Apple"
xcrun notarytool submit "$DMG" \
  --key "$API_KEY" --key-id "$API_KEY_ID" --issuer "$ISSUER" \
  --wait --timeout 30m

say "Agrafage du ticket sur le DMG"
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"

# --------------------------------------------------------------------------- #
# 4. Empreintes
# --------------------------------------------------------------------------- #
say "Empreintes SHA-256"
( cd dist/release && shasum -a 256 ./* | tee SHA256SUMS.txt )

# --------------------------------------------------------------------------- #
# 5. Vérification Gatekeeper : le test qui compte
# --------------------------------------------------------------------------- #
say "Évaluation Gatekeeper"
if spctl --assess --type execute --verbose=4 "$APP" 2>&1 | sed 's/^/    /'; then
  echo "    ✓ l'app est acceptée par Gatekeeper"
else
  die "Gatekeeper refuse encore l'app — la notarisation n'a pas abouti"
fi

echo
say "Artefacts prêts dans dist/release/"
ls -lh dist/release/
