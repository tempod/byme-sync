"""Rigenera le immagini della cartella brand/ dai sorgenti SVG.

Uso (dalla cartella principale del repository):
    pip install cairosvg pillow
    python strumenti/icone/genera_png.py

Crea in custom_components/byme_sync/brand/:
    icon.png 256x256, icon@2x.png 512x512, logo.png alto 128, logo@2x.png alto 256
    e le stesse con il prefisso dark_ per il tema scuro.
"""

from __future__ import annotations

import io
from pathlib import Path

import cairosvg
from PIL import Image

HERE = Path(__file__).resolve().parent
BRAND = HERE.parents[1] / "custom_components" / "byme_sync" / "brand"
RENDER = 2048  # risoluzione di lavoro prima del ridimensionamento


def render(svg: Path, width: int) -> Image.Image:
    png = cairosvg.svg2png(url=str(svg), output_width=width)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def trim(image: Image.Image) -> Image.Image:
    return image.crop(image.getbbox())


def square(image: Image.Image) -> Image.Image:
    side = max(image.size)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return out


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    for theme, prefix in (("light", ""), ("dark", "dark_")):
        icon = square(trim(render(HERE / f"icon-{theme}.svg", RENDER)))
        for size, suffix in ((256, ""), (512, "@2x")):
            icon.resize((size, size), Image.LANCZOS).save(
                BRAND / f"{prefix}icon{suffix}.png", optimize=True
            )
        logo = trim(render(HERE / f"logo-{theme}.svg", RENDER * 2))
        for height, suffix in ((128, ""), (256, "@2x")):
            width = round(logo.width * height / logo.height)
            logo.resize((width, height), Image.LANCZOS).save(
                BRAND / f"{prefix}logo{suffix}.png", optimize=True
            )
    print(f"Immagini aggiornate in {BRAND}")


if __name__ == "__main__":
    main()
