# Brand assets

The product brand is **IHWEZA** — *Together We Move.*

These are the **derivatives only**. The source is a large raster poster in the repository
owner's `~/Downloads`, which is deliberately not tracked.

## Why derivatives and not the original

11 §5.1 forbids large binaries, on the grounds that a repository which accumulates them
stops being cheaply clonable — and this one has to stay clonable, because it is also the
deployment artifact. The original is ~3.6 MB for a mark that is displayed at 32 px
favicon size on most requests. A 32 px favicon that costs 3.6 MB is not a design choice,
it is an accident of how the asset arrived.

DEC-ASSET-001: every committed brand asset is a derivative sized to its actual use, and
`scripts/build_brand_assets.py` is the only thing that writes into this directory —
because the one thing that makes a generated file worse than a hand-made one is a
generated file nobody can regenerate.

## Expected files

`config/branding.py` refers to these by path. A missing file is a broken template, not a
graceful degradation, so each one is asserted present by
`tests/unit/test_settings_contract.py` once this directory is populated.

| File | Size | Used by |
|---|---|---|
| `ihweza-mark-32.png` | 32×32 | Favicon, admin nav — the smallest asset, and the one that is loaded most |
| `ihweza-mark-256.png` | 256×256 | Django admin header logo (`BRAND_LOGO_MARK`), login page, emails |
| `ihweza-mark-1024.png` | 1024×1024 | Social preview cards, press, app store listings |

| Attribute | Value | Constraint |
|---|---|---|
| Format | PNG, RGBA | Transparency is required — the mark sits on both the white admin background and the dark login panel |
| Total budget | **< 200 KB** across all three | 11 §5.1 |
| Colour | `#0B6B3A` primary, `#F5B301` accent | `config.branding.BRAND_COLOR_*` |

## Generating them

`scripts/build_brand_assets.py` reads the source from a path given on the command line —
never a hard-coded one, so that a developer's filesystem never becomes a build
requirement — and writes the three files above.

```bash
python scripts/build_brand_assets.py ~/Downloads/"ChatGPT Image Sep 16, 2026, 05_01_35 PM.png"
```

It crops the uniform margin around the mark, pads to a square, resizes each target from
the original with Lanczos, and optimizes. It prints the byte count per file and **fails
if the total exceeds the 200 KB budget** — a budget checked by a human reading a log is a
budget that is exceeded once and then quietly raised.

It needs Pillow, which is a build-time dependency in `requirements/dev.txt` and is never
installed in the runtime image.

### What it does not do, and what that means

- **It does not remove a non-uniform background.** It trims a *uniform* margin and pads
  to a square. If the source has a coloured or patterned backdrop behind the mark, that
  backdrop survives into all three files. Removing it correctly needs a judgement about
  which pixels are the mark, and getting that wrong produces a mark with bites taken out
  of it — worse than a mark on a coloured square.
- **It does not align or optically centre the mark.** A mark that is off-centre in its
  source stays off-centre.
- **`--tolerance` needs a human.** The default of 12 suits a clean PNG. A JPEG source
  needs it higher; too high and the trim eats the mark's edges.

So the output of this script requires the owner's eyes before it is committed. That is
the one step this repository cannot automate, and pretending otherwise would mean
committing assets nobody looked at.

## Not in this directory

- The source image (not tracked; 11 §5.1)
- `.svg` sources — the logo arrived as a raster, and a traced SVG is a different mark
  wearing the same name. If a vector original is ever produced, it replaces these and is
  committed here.
- Any photograph of a person, or any asset containing real member data (11 §5.1)
