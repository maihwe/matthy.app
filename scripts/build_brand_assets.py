#!/usr/bin/env python3
"""Build the IHWEZA brand derivatives — DEC-ASSET-001, 11 §5.1.

The source is a large raster poster kept outside the repository. This produces
the three files the application actually loads, at the sizes it loads them.

Two properties matter more than the image processing:

* **The source path is an argument, never a constant.** A hard-coded
  ``~/Downloads`` path makes one developer's filesystem a build requirement,
  and the failure appears on a colleague's machine as a missing file.
* **The size budget is enforced here, not observed.** The script exits non-zero
  when the total exceeds the budget. A budget a human reads in a log is a budget
  that gets exceeded once and then quietly raised.

Requires Pillow, which is a *build* dependency only — it is in
``requirements/dev.txt`` and never installed in the runtime image.

    python scripts/build_brand_assets.py ~/Downloads/logo.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image, ImageChops
except ImportError:  # pragma: no cover - a build-environment failure
    sys.exit(
        "Pillow is required to build brand assets.\n"
        "It is a build-time dependency: pip install -r requirements/dev.txt"
    )

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "static" / "brand"

# 11 §5.1. The three files together. Deliberately a total rather than a
# per-file limit: it is the clone size that matters, and a per-file limit just
# encourages one file to grow while the others stay small.
BUDGET_BYTES = 200 * 1024

# (filename, square edge in pixels). Square because every consumer — favicon,
# admin header, social card — renders it in a square box, and a non-square
# source would be letterboxed by each of them separately and differently.
DERIVATIVES: tuple[tuple[str, int], ...] = (
    ("ihweza-mark-32.png", 32),
    ("ihweza-mark-256.png", 256),
    ("ihweza-mark-1024.png", 1024),
)


def _trim_background(image: Image.Image, tolerance: int) -> Image.Image:
    """Crop the uniform border, using the top-left pixel as the background.

    A poster normally has a wide margin around the mark. Left in place, the
    margin is 70-90% of the pixels in every derivative — pixels that carry no
    information and cost bytes in all three files.
    """
    background = Image.new("RGBA", image.size, image.getpixel((0, 0)))
    difference = ImageChops.difference(image, background).convert("L")
    # A threshold rather than an exact match: a photographed or JPEG-compressed
    # source has background pixels that differ from the corner by a little.
    mask = difference.point(lambda value: 255 if value > tolerance else 0)
    bbox = mask.getbbox()
    if bbox is None:
        # Every pixel matched the background. The source is blank, or the
        # tolerance is larger than the mark's contrast against it.
        raise ValueError(
            "the whole image matches its background colour — the source is "
            "blank, or --tolerance is too high"
        )
    return image.crop(bbox)


def _square(image: Image.Image) -> Image.Image:
    """Pad to a square, so the mark is not distorted by the resize.

    Padding rather than stretching: a stretched logo is the most recognisable
    way for a brand to look broken.
    """
    width, height = image.size
    edge = max(width, height)
    if width == height:
        return image
    canvas = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    canvas.paste(image, ((edge - width) // 2, (edge - height) // 2), image)
    return canvas


def build(source: Path, output_dir: Path, tolerance: int) -> list[tuple[Path, int]]:
    """Write every derivative and return ``[(path, bytes)]``.

    The source is resized once from the *original* for each target size, never
    from a smaller derivative: resizing 1024 from 256 would bake in the losses
    of the intermediate step.
    """
    with Image.open(source) as opened:
        # RGBA throughout: the mark sits on the white admin background and the
        # dark login panel, so it needs a real alpha channel rather than a
        # flattened colour that only looks right on one of them.
        original = _square(_trim_background(opened.convert("RGBA"), tolerance))

    output_dir.mkdir(parents=True, exist_ok=True)

    written: list[tuple[Path, int]] = []
    for filename, edge in DERIVATIVES:
        target = output_dir / filename
        resized = original.resize((edge, edge), Image.Resampling.LANCZOS)
        resized.save(target, format="PNG", optimize=True)
        written.append((target, target.stat().st_size))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="path to the source image")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_DIR,
        help=f"where to write the derivatives (default: {OUTPUT_DIR})",
    )
    parser.add_argument(
        "--tolerance",
        type=int,
        default=12,
        help="0-255. How far a pixel may differ from the corner colour and "
        "still count as background. Raise it for a JPEG source, lower it if "
        "the trim is eating the mark.",
    )
    args = parser.parse_args(argv)

    if not args.source.is_file():
        parser.error(f"no such file: {args.source}")
    if not 0 <= args.tolerance <= 255:
        parser.error("--tolerance must be between 0 and 255")

    try:
        written = build(args.source, args.output_dir, args.tolerance)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"source: {args.source}")
    total = 0
    for path, size in written:
        total += size
        print(f"  {path.name:<24} {size / 1024:8.1f} KB")

    budget_kb = BUDGET_BYTES / 1024
    if total > BUDGET_BYTES:
        print(
            f"\nFAILED: {total / 1024:.1f} KB exceeds the {budget_kb:.0f} KB budget "
            "(11 §5.1).\n"
            "Reduce with: a tighter --tolerance, or drop the 1024 derivative if "
            "nothing actually consumes it.",
            file=sys.stderr,
        )
        return 1

    print(f"\nTotal {total / 1024:.1f} KB of {budget_kb:.0f} KB — within budget.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
