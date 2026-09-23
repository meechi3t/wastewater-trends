#!/usr/bin/env python3
"""Generate the site icons and the link-preview (Open Graph) image.

Run only when the branding changes:

    pip install pillow
    python scripts/make_images.py

Outputs, all committed to docs/:
    tip10-logo.png       32px badge used in the footer credit
    favicon-32.png       browser tab
    favicon-180.png      iOS home screen / apple-touch-icon
    og-image.png         1200x630 card shown when the link is shared

Source artwork is the Tip10 monogram, silver on navy. Pillow is a
development-only dependency: it is not needed to build the dataset or serve
the site.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
LOGO_SOURCE = Path.home() / "Desktop/Tip10.tech_rebuild/public/logo-original.png"

# Brand colours, taken from the Tip10 site's own recolor-logo.mjs.
NAVY = (0x0A, 0x15, 0x36)
SILVER = (0xC8, 0xCD, 0xD6)
IVORY = (0xEE, 0xE5, 0xD1)

# System fonts, so this needs no font files checked in. SFNS carries the
# weights macOS uses for UI text.
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def square_icons(source: Image.Image) -> None:
    """Favicon and touch icon, with the logo's square navy field intact."""
    for size, name in ((32, "favicon-32.png"), (180, "favicon-180.png"), (32, "tip10-logo.png")):
        icon = source.resize((size * 4, size * 4), Image.LANCZOS)
        icon = icon.resize((size, size), Image.LANCZOS)
        icon.save(DOCS / name, optimize=True)
        print(f"  wrote docs/{name} ({size}x{size})")


def og_image(source: Image.Image) -> None:
    """1200x630 card for iMessage, WhatsApp, Slack, and the rest."""
    width, height = 1200, 630
    card = Image.new("RGB", (width, height), NAVY)
    draw = ImageDraw.Draw(card)

    mark = source.convert("RGBA").resize((168, 168), Image.LANCZOS)
    card.paste(mark, (88, 86), mark)

    title = font(FONT_BOLD, 82)
    subtitle = font(FONT_REGULAR, 34)
    small = font(FONT_REGULAR, 26)

    draw.text((88, 300), "Wastewater Trends", font=title, fill=IVORY)
    draw.text((88, 400),
              "See which illnesses are showing up in", font=subtitle, fill=SILVER)
    draw.text((88, 444),
              "your community's wastewater right now.", font=subtitle, fill=SILVER)

    draw.text((88, 526), "Visualized by Tip10 Technologies", font=small, fill=SILVER)
    draw.text((88, 560), "Data by WastewaterSCAN", font=small, fill=(0x7A, 0x84, 0x9B))

    card.save(DOCS / "og-image.png", optimize=True)
    print(f"  wrote docs/og-image.png ({width}x{height})")


def main() -> int:
    if not LOGO_SOURCE.exists():
        print(f"ERROR: logo not found at {LOGO_SOURCE}", file=sys.stderr)
        return 1
    source = Image.open(LOGO_SOURCE).convert("RGBA")
    print(f"source: {LOGO_SOURCE} ({source.width}x{source.height})")
    square_icons(source)
    og_image(source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
