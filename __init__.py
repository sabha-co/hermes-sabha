"""Sabha platform plugin for Hermes Agent.

The Hermes plugin loader imports this package and calls ``register(ctx)``.
Keep the entry point re-exported here so a directory plugin dropped into
``~/.hermes/plugins/`` is discovered without extra configuration.
"""

from .adapter import register

__all__ = ["register"]
