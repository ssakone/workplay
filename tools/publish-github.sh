#!/usr/bin/env bash
#
# Publie une release GitHub pour WorkPlay : pousse les commits et le tag,
# crée la release, puis téléverse le DMG et le ZIP notarisés.
#
# Le jeton est lu dans le trousseau macOS (le même que git push) et n'est
# jamais affiché ni écrit sur le disque.
#
# Usage : ./tools/publish-github.sh <version> [fichier-notes.md]
#   ./tools/publish-github.sh 1.5.0
set -euo pipefail

VERSION="${1:?usage : publish-github.sh <version>}"
NOTES_FILE="${2:-}"

cd "$(dirname "$0")/.."
ROOT="$PWD"
REL="$ROOT/dist/release"
API="https://api.github.com/repos/ssakone/workplay"
# Les pièces jointes d'une release ne passent PAS par api.github.com :
# GitHub exige uploads.github.com, sinon l'appel renvoie 404.
UPLOADS="https://uploads.github.com/repos/ssakone/workplay"

# --- Jeton : trousseau macOS, jamais imprimé -------------------------------
TOKEN="$(printf 'protocol=https\nhost=github.com\n\n' \
  | git credential fill 2>/dev/null | sed -n 's/^password=//p' | head -1)"
if [[ -z "$TOKEN" ]]; then
  echo "✗ Aucun jeton GitHub trouvé dans le trousseau." >&2
  exit 1
fi

auth=( -H "Authorization: Bearer $TOKEN"
       -H "Accept: application/vnd.github+json"
       -H "X-GitHub-Api-Version: 2022-11-28" )

echo "▸ Poussée de main et du tag v$VERSION"
git push origin main
git push origin "v$VERSION"

# --- Corps de la release ---------------------------------------------------
if [[ -n "$NOTES_FILE" && -f "$NOTES_FILE" ]]; then
  BODY="$(cat "$NOTES_FILE")"
else
  BODY="$(git log --pretty=format:'- %s' "$(git describe --tags --abbrev=0 HEAD^ 2>/dev/null || echo HEAD~3)"..HEAD)"
fi

# --- Création de la release ------------------------------------------------
echo "▸ Création de la release v$VERSION"
payload="$(python3 - "$VERSION" "$BODY" <<'PY'
import json, sys
print(json.dumps({
    "tag_name": f"v{sys.argv[1]}",
    "name": f"WorkPlay {sys.argv[1]}",
    "body": sys.argv[2],
    "draft": False,
    "prerelease": False,
    "make_latest": "true",
}))
PY
)"

# Une release peut déjà exister si le script est relancé : on la retrouve.
if existing="$(curl -fsSL "${auth[@]}" "$API/releases/tags/v$VERSION" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)"; then
  REL_ID="$existing"
  echo "  (release existante, id=$REL_ID)"
else
  REL_ID="$(curl -fsSL "${auth[@]}" -X POST "$API/releases" \
    -d "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  echo "  créée, id=$REL_ID"
fi

# --- Téléversement des artefacts -------------------------------------------
for f in "$REL/WorkPlay-$VERSION.dmg" "$REL/WorkPlay-$VERSION.zip"; do
  [[ -f "$f" ]] || { echo "✗ Artefact absent : $f" >&2; exit 1; }
  name="$(basename "$f")"
  size="$(stat -f%z "$f")"
  echo "▸ Téléversement de $name ($((size / 1024 / 1024)) Mo)"

  # Un asset du même nom peut déjà être là après une relance : on le remplace.
  old="$(curl -fsSL "${auth[@]}" "$API/releases/$REL_ID/assets" \
    | python3 -c 'import json, sys
for a in json.load(sys.stdin):
    if a["name"] == sys.argv[1]:
        print(a["id"])
        break' "$name" 2>/dev/null || true)"
  [[ -n "$old" ]] && curl -fsSL "${auth[@]}" -X DELETE \
    "$API/releases/assets/$old" >/dev/null

  curl -fsSL "${auth[@]}" \
    -H "Content-Type: application/octet-stream" \
    --data-binary "@$f" \
    "$UPLOADS/releases/$REL_ID/assets?name=$name" \
    | python3 -c 'import json, sys
a = json.load(sys.stdin)
print("  OK " + a["name"] + " — " + str(a["size"]) + " octets — "
      + a["browser_download_url"])'
done

echo "▸ Release publiée : https://github.com/ssakone/workplay/releases/tag/v$VERSION"
