"""Application modules.

Each module is a Django app under this package, laid out per 02 §4. The five
enforced layers (L0-L4) are declared in ``.importlinter``; this package exists
so that module imports read ``apps.accounts.models`` rather than a bare
``accounts.models``, which keeps the layer contract expressible as a path
rule.
"""
