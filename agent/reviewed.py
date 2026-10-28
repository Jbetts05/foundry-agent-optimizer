"""Reviewed optimizer configuration; the original weak version remains pinned."""

from pathlib import Path

if __package__:
    from .main import create_host
else:
    from main import create_host


def create_reviewed_host():
    return create_host(
        append_fixed_policy=False,
        config_dir=Path(__file__).parent / ".reviewed_configs",
    )


if __name__ == "__main__":
    create_reviewed_host().run()
