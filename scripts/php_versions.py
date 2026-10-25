#!/usr/bin/env python3
"""Read the shared PHP release list for local builds, CI and publication."""

import argparse
import json
from pathlib import Path
import re


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "php-versions.json"
SERIES = re.compile(r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)\Z")


def load_versions(path=DEFAULT_CONFIG):
    try:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise ValueError("Cannot read a valid php-versions.json") from error
    if not isinstance(values, list) or not values:
        raise ValueError("php-versions.json must contain a nonempty array of PHP series")
    if any(not isinstance(value, str) or not SERIES.fullmatch(value) for value in values):
        raise ValueError("PHP series must be strings in major.minor format")
    if len(set(values)) != len(values):
        raise ValueError("Duplicate PHP series in php-versions.json")
    return tuple(sorted(values, key=lambda value: tuple(map(int, value.split(".")))))


def resolve_version(value, versions):
    if value == "latest":
        return versions[-1]
    if value not in versions:
        raise ValueError("Unsupported PHP series; configured versions: " + ", ".join(versions))
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--json", action="store_true", help="Print the CI matrix array")
    output.add_argument("--list", action="store_true", help="Print one PHP series per line")
    output.add_argument("--latest", action="store_true", help="Print the highest configured PHP series")
    output.add_argument("--check", metavar="SERIES", help="Validate a series or resolve latest")
    args = parser.parse_args(argv)
    try:
        versions = load_versions(args.config)
        if args.json:
            print(json.dumps(versions, separators=(",", ":")))
        elif args.list:
            print("\n".join(versions))
        else:
            print(resolve_version(args.check if args.check is not None else "latest", versions))
    except ValueError as error:
        parser.exit(1, f"Cannot load PHP versions: {error}\n")


if __name__ == "__main__":
    main()
