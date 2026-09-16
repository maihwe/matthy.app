"""Concurrency tests: real threads, separate connections, real PostgreSQL.

13 §4 names this directory and 13 §2 names what belongs in it — lock ordering,
double-spend, lost update. Nothing else does: these are the properties that hold
across two database connections and are therefore invisible to every other suite,
which is why the failure mode they catch is *data corruption under load* rather
than a red test.

The marker is ``transaction``-scoped on purpose. A concurrency test that runs
inside pytest-django's usual wrapping transaction is not a weaker test, it is a
test that cannot work: the second connection cannot see the first's uncommitted
rows, so ``SELECT ... FOR UPDATE`` blocks until the wrapper ends — which is
teardown. The result is a hang, not a failure, and a hang in CI reads as
infrastructure trouble rather than as the bug it is.
"""
