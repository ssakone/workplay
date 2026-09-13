#!/usr/bin/env bash
#
# Prépare un trousseau temporaire contenant le certificat
# "Developer ID Application", afin de signer WorkPlay pour la distribution.
#
# WHY A TEMPORARY KEYCHAIN
#   Le trousseau de session ne contient qu'un certificat "Apple Development",
#   qui signe pour le développement local mais que Gatekeeper refuse pour la
#   distribution. Plutôt que d'importer le certificat de distribution dans le
#   trousseau de l'utilisateur (opération difficile à annuler proprement), on
#   ouvre un trousseau dédié, utilisé seulement le temps du build.
#
# Usage :
#   tools/sign-keychain.sh create    # crée et déverrouille le trousseau
#   tools/sign-keychain.sh name      # affiche le nom de l'identité à utiliser
#   tools/sign-keychain.sh destroy   # supprime le trousseau temporaire
#
# Variables (avec valeurs par défaut) :
#   WORKPLAY_KEYCHAIN   nom du trousseau       (défaut: workplay-signing.keychain-db)
#   WORKPLAY_P12        certificat .p12        (défaut: ~/Workstation/ME/certs-bumas/macos-developer-id.p12)
#   WORKPLAY_P12_PASS   fichier du mot de passe(défaut: ~/Workstation/ME/certs-bumas/macos-cert-password.txt)
#
set -euo pipefail

KEYCHAIN="${WORKPLAY_KEYCHAIN:-$HOME/Library/Keychains/workplay-signing.keychain-db}"
P12="${WORKPLAY_P12:-$HOME/Workstation/ME/certs-bumas/macos-developer-id.p12}"
P12_PASS_FILE="${WORKPLAY_P12_PASS:-$HOME/Workstation/ME/certs-bumas/macos-cert-password.txt}"

say() { printf '\033[1m▸ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Le mot de passe du trousseau lui-même est éphémère : il ne protège rien
# d'autre que ce coffre jetable, entre deux commandes du même script.
kc_pass() {
  if [ -f "$KEYCHAIN.pass" ]; then
    cat "$KEYCHAIN.pass"
  else
    openssl rand -hex 24 | tee "$KEYCHAIN.pass"
  fi
}

cmd_create() {
  [ -f "$P12" ] || die "certificat introuvable : $P12"
  [ -f "$P12_PASS_FILE" ] || die "mot de passe introuvable : $P12_PASS_FILE"

  say "Création du trousseau temporaire"
  security delete-keychain "$KEYCHAIN" 2>/dev/null || true
  security create-keychain -p "$(kc_pass)" "$KEYCHAIN"
  security set-keychain-settings -lut 21600 "$KEYCHAIN"   # verrou après 6 h
  security unlock-keychain -p "$(kc_pass)" "$KEYCHAIN"

  say "Import du certificat Developer ID"
  # Le mot de passe est lu depuis le fichier et passé directement à security :
  # il n'est jamais affiché ni écrit ailleurs.
  security import "$P12" -k "$KEYCHAIN" \
    -P "$(cat "$P12_PASS_FILE")" \
    -T /usr/bin/codesign -T /usr/bin/security >/dev/null

  # Sans cela, codesign demande une autorisation interactive à chaque appel.
  security set-key-partition-list -S apple-tool:,apple:,codesign: \
    -s -k "$(kc_pass)" "$KEYCHAIN" >/dev/null 2>&1

  # codesign ne cherche QUE dans les trousseaux de la liste de recherche :
  # un trousseau déverrouillé mais absent de cette liste reste invisible et
  # provoque un « no identity found ». On l'y ajoute donc explicitement.
  EXISTING="$(security list-keychains -d user | sed 's/^[[:space:]]*//; s/"//g' | tr '\n' ' ')"
  # shellcheck disable=SC2086
  security list-keychains -d user -s $EXISTING "$KEYCHAIN"

  say "Identité disponible"
  security find-identity -v -p codesigning "$KEYCHAIN" \
    | grep "Developer ID Application" \
    | sed -E 's/.*"(.*)"/  \1/'
}

cmd_name() {
  security find-identity -v -p codesigning "$KEYCHAIN" 2>/dev/null \
    | grep "Developer ID Application" | head -1 \
    | sed -E 's/.*"(.*)"/\1/'
}

cmd_destroy() {
  say "Suppression du trousseau temporaire"
  # On le retire d'abord de la liste de recherche, pour ne pas laisser une
  # référence morte derrière nous.
  REMAINING="$(security list-keychains -d user | sed 's/^[[:space:]]*//; s/"//g' \
              | grep -v "$KEYCHAIN" | tr '\n' ' ')"
  if [ -n "$REMAINING" ]; then
    # shellcheck disable=SC2086
    security list-keychains -d user -s $REMAINING
  fi
  security delete-keychain "$KEYCHAIN" 2>/dev/null || true
  rm -f "$KEYCHAIN.pass"
  say "Fait."
}

case "${1:-}" in
  create)  cmd_create ;;
  name)    cmd_name ;;
  destroy) cmd_destroy ;;
  *) die "usage: $0 {create|name|destroy}" ;;
esac
