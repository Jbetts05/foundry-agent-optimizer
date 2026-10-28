"""Isolated weak-baseline host; the public main.py entry point is unchanged."""

from pathlib import Path

if __package__:
    from .main import create_host
else:
    from main import create_host


def create_experiment_host():
    return create_host(
        append_fixed_policy=False,
        config_dir=Path(__file__).parent / ".experiment_configs",
    )


if __name__ == "__main__":
    create_experiment_host().run()
