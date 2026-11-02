"""Command-line entry point. The engine lives in the other modules; this file only wires arguments."""
from __future__ import annotations

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="saverun", description=__doc__)
    parser.add_argument("--version", action="version", version=f"saverun {__version__}")
    parser.parse_args(argv)
    print("Aucune commande disponible pour l'instant (squelette).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
