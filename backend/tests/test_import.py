"""Import guard for the P01 dependency lane (P01 acceptance criterion 2).

The module-scope imports are the check: a missing or broken locked dependency
fails collection before any assertion runs. The single assertion below guards
the contract-frozen durable engine version (contracts.md C05: ``dbos==3.2.0``).
"""

from importlib.metadata import version

import dbos  # noqa: F401  (import is the check)
import fastapi  # noqa: F401  (import is the check)

import mai  # noqa: F401  (import is the check)


def test_frozen_durable_engine_pin() -> None:
    # PyPI distribution is `dbos` (GitHub repo: dbos-transact-py).
    assert version("dbos") == "3.2.0"
