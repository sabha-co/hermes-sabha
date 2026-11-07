"""Sabha platform plugin for Hermes Agent."""


def register(ctx) -> None:
    from .adapter import register as register_adapter

    register_adapter(ctx)


__all__ = ["register"]
