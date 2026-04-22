"""
pyrere/context/__init__.py
──────────────────────────
Context-window / prompt-assembly layer — not yet implemented.

This package is reserved for a future release.  Accessing any attribute will
raise NotImplementedError with a clear message.  The error is deferred to
attribute access (via __getattr__) rather than raised at import time, so that
``import pyrere`` and ``import pyrere.context`` both succeed without crashing
— only actually *using* something from this package will raise.
"""

from __future__ import annotations


def __getattr__(name: str) -> object:
    raise NotImplementedError(
        f"pyrere.context.{name} is not yet implemented. "
        "pyrere.context will be available in a future release."
    )
