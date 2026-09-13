#!/usr/bin/env python3
"""Transforme le logo généré en jeu d'icônes macOS (.iconset -> .icns).

Le logo est un squircle sur fond blanc : on rend le blanc transparent pour
obtenir des coins propres, puis on recadre au plus juste et on décline
toutes les tailles exigées par macOS.
"""

import sys
from pathlib import Path

from PIL import Image

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/namadingo_logo.jpg")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/AppIcon.iconset")


def square_crop(im: Image.Image) -> Image.Image:
    """Recadre au carré central (le générateur peut rendre du 1:1.02)."""
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return im.crop((left, top, left + side, top + side))


def strip_white(im: Image.Image, threshold: int = 228) -> Image.Image:
    """Rend transparent le fond clair et recadre le squircle.

    Un alpha progressif sur les bords évite l'effet crénelé.
    """
    im = im.convert("RGBA")
    px = im.load()
    w, h = im.size

    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            mn = min(r, g, b)
            if mn >= threshold:
                px[x, y] = (r, g, b, 0)
            elif mn >= threshold - 30:
                # Zone de dégradé : alpha proportionnel -> bord lissé.
                t = (threshold - mn) / 30.0
                px[x, y] = (r, g, b, int(a * min(1.0, t)))

    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)

    # Regarde de nouveau carré après recadrage.
    side = max(im.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
    return canvas


def main() -> int:
    if not SRC.exists():
        print(f"Source introuvable : {SRC}", file=sys.stderr)
        return 1

    im = square_crop(Image.open(SRC).convert("RGBA"))
    im = strip_white(im)

    print(f"image traitée : {im.size[0]}x{im.size[1]}, "
          f"alpha moyen={_mean_alpha(im):.0f}/255")

    OUT.mkdir(parents=True, exist_ok=True)

    # Tailles requises par iconutil pour un .icns complet.
    specs = [
        (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
    ]
    for size, name in specs:
        im.resize((size, size), Image.LANCZOS).save(OUT / name)
        print(f"  {name:<24} {size}x{size}")

    return 0


def _mean_alpha(im: Image.Image) -> float:
    alpha = im.getchannel("A")
    hist = alpha.histogram()
    total = sum(hist)
    return sum(i * n for i, n in enumerate(hist)) / total if total else 0.0


if __name__ == "__main__":
    sys.exit(main())
