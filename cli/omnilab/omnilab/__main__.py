"""Entry point for `python -m omnilab` and the `omnilab` console script."""

from .cli import app


def main() -> None:
    """Console-script entry point declared in pyproject.toml."""
    app()


if __name__ == "__main__":
    main()
