# Icone By-me Sync

Sorgenti vettoriali delle immagini in `custom_components/byme_sync/brand/`.

| File | Uso |
|---|---|
| `icon-light.svg` | Icona per tema chiaro (`icon.png`, `icon@2x.png`) |
| `icon-dark.svg` | Icona per tema scuro (`dark_icon.png`, `dark_icon@2x.png`) |
| `logo-light.svg` | Logo per tema chiaro (`logo.png`, `logo@2x.png`) |
| `logo-dark.svg` | Logo per tema scuro (`dark_logo.png`, `dark_logo@2x.png`) |

Il testo del logo è già convertito in tracciati, quindi non serve avere il font installato.

Dopo aver modificato un SVG, rigenera le PNG:

```bash
pip install cairosvg pillow
python strumenti/icone/genera_png.py
```

Misure richieste da Home Assistant: icone quadrate 256×256 e 512×512 (@2x), logo con il lato corto tra 128 e 256 px (256–512 per @2x), PNG con sfondo trasparente e senza margini vuoti.
