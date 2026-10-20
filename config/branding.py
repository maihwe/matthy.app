"""The product's identity, in one place.

The brand is **IHWEZA** (``Together We Move.``). The repository, the Python
package, and the Django project keep the name ``matthy.app`` / ``config``.

That split is deliberate and recorded as P1-1 in PHASE_1_PLAN §5: branding is
presentation, and renaming the package would churn all fourteen Phase 0
documents for no structural gain. Nothing outside this module and the templates
that read it should contain the brand name as a literal — a product name
scattered across the codebase is a rename that never quite finishes.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "BRAND_COLOR_ACCENT",
    "BRAND_COLOR_PRIMARY",
    "BRAND_LOGO_MARK",
    "BRAND_NAME",
    "BRAND_TAGLINE",
    "PLATFORM_NAME",
]

PLATFORM_NAME: Final = "matthy.app"
"""The project's internal name — the repository, package, and docs."""

BRAND_NAME: Final = "IHWEZA"
"""The product's user-facing name."""

BRAND_TAGLINE: Final = "Together We Move."

# Palette, taken from the wallet mark. Values are the mid-tones rather than
# the gradient stops, so they hold contrast on both light and dark surfaces.
BRAND_COLOR_PRIMARY: Final = "#0B6B3A"
BRAND_COLOR_ACCENT: Final = "#F5B301"

BRAND_LOGO_MARK: Final = "brand/ihweza-mark-256.png"
"""Path within STATIC_URL. See static/brand/README.md for the asset set."""
