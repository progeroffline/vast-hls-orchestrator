"""Console-script entry point: parse arguments, configure logging, run the pipeline."""

from __future__ import annotations

from .cli import parse_args
from .core.logging_setup import configure_logging
from .pipeline import run


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
