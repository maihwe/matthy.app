"""API conventions shared by every endpoint — 04 §1.

This package holds the shapes that make a client author write error handling,
pagination, and money formatting **once** (04 §1). It is project plumbing
rather than a domain module, which is why it lives beside ``config`` and not
in ``apps/`` — the architecture's module list (02 §4) has no place for it, and
inventing an ``apps/core`` would create a module with no layer assignment.
"""
