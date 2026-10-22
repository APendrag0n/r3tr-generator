"""R3TR generator.

A small application that reads a CSV file describing SAP Basis components
and their tables, and generates export/import parameter files for the SAP
``R3trans`` tool.

The package ships two front-ends over the same core logic:

* ``r3tr.app``  - NiceGUI web interface (recommended).
* ``r3tr.cli``  - classic command-line mode (``-f -o -c -d --force`` plus
  the advanced flags ``--export-components`` / ``--import-components`` /
  ``--exclude-tables``).
"""

from .core import (  # noqa: F401
    TADIR,
    CSVFileNotFoundError,
    CSVFormatError,
    GenerationResult,
    NoDataError,
    OutputDirError,
    R3TRCoreError,
    build_generation_plan,
    component_from_filename,
    conflicting_paths,
    export_filename,
    generate_scripts,
    import_filename,
    load_csv,
    render_export_file,
    render_import_file,
    resolve_exp_file,
)

__all__ = [
    "TADIR",
    "CSVFileNotFoundError",
    "CSVFormatError",
    "GenerationResult",
    "NoDataError",
    "OutputDirError",
    "R3TRCoreError",
    "build_generation_plan",
    "component_from_filename",
    "conflicting_paths",
    "export_filename",
    "generate_scripts",
    "import_filename",
    "load_csv",
    "render_export_file",
    "render_import_file",
    "resolve_exp_file",
]

__version__ = "1.3.0"
