"""Queue operations.

Enqueue, claim, completion, recovery, and expiration are implemented in a
later phase. Sessions stay short: do not hold a transaction open while
generating or rendering a plan.
"""
from __future__ import annotations
