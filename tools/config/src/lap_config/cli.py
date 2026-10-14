from __future__ import annotations

import argparse
from pathlib import Path

from lap_config.generate import load_config, write_generated_files


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def validate(repo_root: Path) -> None:
    load_config(repo_root)


def generate(repo_root: Path) -> None:
    written = write_generated_files(repo_root)
    for path in written:
        print(path.relative_to(repo_root).as_posix())


def main() -> None:
    parser = argparse.ArgumentParser(prog="lap-config")
    parser.add_argument("--repo-root", type=Path, default=_repo_root())
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("validate")
    subcommands.add_parser("generate")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    if args.command == "validate":
        validate(repo_root)
    elif args.command == "generate":
        generate(repo_root)
