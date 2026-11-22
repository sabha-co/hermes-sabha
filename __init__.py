"""Sabha platform plugin for Hermes Agent."""


def register(ctx) -> None:
    from . import cli
    from .adapter import register as register_adapter

    register_adapter(ctx)
    ctx.register_cli_command(name="sabha", help="Connect Hermes to a Sabha app",
                             setup_fn=cli.register_cli, handler_fn=cli.dispatch)


__all__ = ["register"]
