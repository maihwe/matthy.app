"""Business logic for ``accounts`` (02 §4: ``services.py`` *or* ``services/`` package).

Every rule that decides *whether* something may happen lives in this package.
The layers above and below it have narrower jobs:

* **``views.py``** parses, authorises, delegates and serialises. It contains no
  rule and, in particular, no branch on account status.
* **``models.py``** enforces what the database can enforce — uniqueness, CHECK
  constraints, immutability — so that a future code path which forgets a rule
  still cannot write the impossible state.
* **``selectors.py``** answers read-only questions.

**Nothing here raises an HTTP error.** These services raise the domain
exceptions in :mod:`apps.accounts.services.errors`, and the API layer is what
turns one into a status code. A service that imported DRF's exceptions would be
unusable from a Celery task, a management command or a test that is not making a
request — all three of which call these services.

**Transactions are the caller's, except where stated.** A service that needs
several writes to be one unit wraps itself in ``transaction.atomic()``; a service
that is a single statement does not, so that it joins whatever transaction its
caller opened. Phase 1 writes no money, but the shape is fixed now because the
financial modules inherit it.
"""
