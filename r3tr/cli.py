"""Classic command-line front-end (same flags as the original v2 script).

Examples::

    # defaults: data/export_import_data.csv -> ./generated_scripts
    python -m r3tr.cli

    # custom CSV, output dir, client and forced overwrite
    python -m r3tr.cli -f data/export_import_data.csv -o /tmp/out -c 100 --force

    # comma separated file
    python -m r3tr.cli -d ","
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .core import (
    CSVFileNotFoundError,
    CSVFormatError,
    GenerationResult,
    NoDataError,
    OutputDirError,
    R3TRCoreError,
    conflicting_paths,
    generate_scripts,
    load_csv,
)

#: Project root (two levels up from this file) - used for default paths.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CSV = PROJECT_ROOT / "data" / "export_import_data.csv"
DEFAULT_OUTPUT = Path("generated_scripts")


def _confirm_overwrite(filepath: Path) -> bool:
    """Ask the user (terminal) whether an existing file may be overwritten.

    Keeps the interactive behaviour of the original script: anything other
    than ``y``/``yes`` counts as *no*.
    """
    while True:
        try:
            response = input(
                f"Warning: File '{filepath}' already exists. Overwrite? (y/n): "
            ).lower().strip()
        except EOFError:
            print("\nSkipping file (no interactive input).")
            return False
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            print(f"Skipping file '{filepath}'.")
            return False
        print("Invalid input. Please enter 'y' or 'n'.")


def build_parser() -> argparse.ArgumentParser:
    """Create the argument parser (documented, defaults included)."""
    parser = argparse.ArgumentParser(
        description="Generate SAP R3trans export/import scripts from a CSV file.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Example usage:
  # Run with default settings
  python -m r3tr.cli

  # Specify a different CSV file and output directory
  python -m r3tr.cli -f my_data.csv -o /tmp/sap_output

  # Force overwrite of existing files and use a different client
  python -m r3tr.cli --force -c 100

  # Use a comma as the delimiter
  python -m r3tr.cli -d ","
""",
    )
    parser.add_argument(
        "-f",
        "--csv-file",
        dest="csv_filepath",
        type=Path,
        default=DEFAULT_CSV,
        help="Path to the input CSV file.\n(default: %(default)s)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        dest="output_dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Directory to save the generated script files.\n(default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force overwrite of existing files without asking for confirmation.\n"
        "(default: False)",
    )
    parser.add_argument(
        "-d",
        "--delimiter",
        type=str,
        default=";",
        help='Delimiter used in the CSV file.\n(default: "%(default)s")',
    )
    parser.add_argument(
        "-c",
        "--client",
        type=str,
        default="ALL",
        help="Client identifier for the scripts (e.g., 000, 100, ALL).\n"
        "(default: %(default)s)",
    )
    parser.add_argument(
        "-C",
        "--components",
        type=str,
        default=None,
        help="Optional comma-separated allow-list of Component names to generate "
        "(selection mode). Omit to generate all enabled components.\n"
        "(default: all)",
    )
    parser.add_argument(
        "-e",
        "--exp-file-base",
        type=str,
        default="",
        help='Optional directory for the data file written into the '
        "file = '...' lines, e.g. /usr/sap/trans/data/SYS/ or D:\\export\\\n"
        "(default: empty = file name only)",
    )
    parser.add_argument(
        "--export-components",
        type=str,
        default=None,
        help="Comma-separated list of components that receive an EXPORT file "
        "(overrides the CSV Export flags; components outside this list get "
        "no export file).\n"
        "(default: CSV 'Export' flags)",
    )
    parser.add_argument(
        "--import-components",
        type=str,
        default=None,
        help="Comma-separated list of components that receive an IMPORT file "
        "(overrides the CSV Import flags; components outside this list get "
        "no import file).\n"
        "(default: CSV 'Import' flags)",
    )
    parser.add_argument(
        "--exclude-tables",
        type=str,
        default="",
        help="Comma-separated table names to omit from the exported content "
        "(advanced option; case-insensitive), e.g. --exclude-tables TADIR\n"
        "(default: none excluded)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code (0 = success)."""
    args = build_parser().parse_args(argv)

    try:
        load = load_csv(args.csv_filepath, args.delimiter)
        if load.warnings_text():
            print(f"Warning: {load.warnings_text()}", file=sys.stderr)
        if not load.grouped:
            print("No data to process. Exiting.", file=sys.stderr)
            return 1

        components: "set[str] | None" = None
        if args.components:
            components = {c.strip() for c in args.components.split(",") if c.strip()}
            all_names = set(load.grouped)
            unknown = components - all_names
            if unknown:
                print(
                    "Warning: unknown component(s) ignored: " + ", ".join(sorted(unknown)),
                    file=sys.stderr,
                )

        export_components: "set[str] | None" = None
        if args.export_components:
            export_components = {
                c.strip() for c in args.export_components.split(",") if c.strip()
            }
            unknown = export_components - set(load.grouped)
            if unknown:
                print(
                    "Warning: unknown export component(s) ignored: "
                    + ", ".join(sorted(unknown)),
                    file=sys.stderr,
                )

        import_components: "set[str] | None" = None
        if args.import_components:
            import_components = {
                c.strip() for c in args.import_components.split(",") if c.strip()
            }
            unknown = import_components - set(load.grouped)
            if unknown:
                print(
                    "Warning: unknown import component(s) ignored: "
                    + ", ".join(sorted(unknown)),
                    file=sys.stderr,
                )

        exclude_tables = tuple(
            t.strip() for t in args.exclude_tables.split(",") if t.strip()
        )
        if exclude_tables:
            print(f"Excluding table(s) from exports: {', '.join(exclude_tables)}")

        print(f"Using output directory: '{args.output_dir}'")
        result: GenerationResult = generate_scripts(
            load.grouped,
            args.output_dir,
            client=args.client,
            force_overwrite=args.force,
            confirm_overwrite=None if args.force else _confirm_overwrite,
            components=components,
            export_components=export_components,
            import_components=import_components,
            exp_file_base=args.exp_file_base,
            exclude_tables=exclude_tables,
        )
    except (CSVFileNotFoundError, CSVFormatError, NoDataError, OutputDirError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except R3TRCoreError as exc:
        print(f"\nAn unexpected critical error occurred: {exc}", file=sys.stderr)
        return 1

    # Report - mirrors the per-file messages of the original script.
    for path in result.generated:
        print(f"Successfully generated file: {path}")
    for path in result.refused:
        print(f"Skipped (already exists): {path}")
    for line in result.summary_lines()[:6]:
        print(line)

    if result.errors:
        return 1
    print("\nScript execution finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
