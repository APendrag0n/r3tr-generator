"""Core logic of the R3TR script generator (UI-agnostic).

This module contains **all** of the generation logic and is deliberately
free of any UI dependency.  It is used by:

* the NiceGUI web application (``r3tr.app``),
* the classic CLI front-end (``r3tr.cli``),
* the unit tests (``tests/test_core.py``).

The logic is a port of the original standalone R3trans generator script
with several intentional upgrades:

* errors are raised as well-described exceptions (the original printed to
  stderr and continued),
* every run returns a :class:`GenerationResult` report instead of relying
  on plain ``print`` output,
* export/import enablement per component can be supplied explicitly (UI
  checkboxes) instead of being read only from the CSV flags, and
* specific tables (e.g. ``TADIR``) can be excluded from the generated
  export content.

CSV layout (semicolon separated by default)::

    Component;Table;Table description;Export;Import
    ABAP_DAEMONS;ABAP_DAEMON_ST;ABAP Daemon framework: Startup configuration;1;0
    ...

``Export`` / ``Import`` are flags (``1`` = generate that file type).
A file is generated for a component when **any** of its rows carries the
flag ``1``.  Components whose rows are all ``0`` produce no files at all
(see component ``SWU3`` in the sample data).

Generated file names::

    <output_dir>/export_<COMPONENT>_<CLIENT>.r3tr
    <output_dir>/import_<COMPONENT>_<CLIENT>.r3tr
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Mapping, Optional, Sequence, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Columns that every data row must provide (values may not be empty).
REQUIRED_COLUMNS: Sequence[str] = ("Component", "Table")

#: Column holding the human readable table description (optional per row).
DESC_COLUMN = "Table description"

#: Flag columns; a value of ``1`` enables generation of that file type.
FLAG_COLUMNS: Mapping[str, str] = {"Export": "export", "Import": "import"}

#: Default delimiter for the data files (see original script).
DEFAULT_DELIMITER = ";"

#: Default client identifier.
DEFAULT_CLIENT = "ALL"

#: Well-known table names that a user may want to exclude from exports
#: (advanced option).  TADIR is the SAP dictionary table holding table
#: metadata; it normally rides along in component exports and is the most
#: common candidate for removal.
TADIR = "TADIR"

#: Default set of tables excluded from export content (empty = nothing).
DEFAULT_EXCLUDE_TABLES: "frozenset[str]" = frozenset()

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class R3TRCoreError(RuntimeError):
    """Base class for every error raised by this module."""


class CSVFileNotFoundError(R3TRCoreError):
    """Raised when the requested CSV file does not exist or is unreadable."""


class CSVFormatError(R3TRCoreError):
    """Raised when the CSV file is present but cannot be parsed.

    The message always describes the concrete problem (column names,
    encoding, row content) so the user can fix the source file.
    """


class NoDataError(R3TRCoreError):
    """Raised when the CSV contains no processable rows at all."""


class OutputDirError(R3TRCoreError):
    """Raised when the output directory cannot be created or written."""


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class CSVLoadResult:
    """Outcome of one :func:`load_csv` call."""

    csv_filepath: Path
    delimiter: str
    grouped: "dict[str, list[dict]]"
    total_rows: int = 0
    skipped_rows: List[dict] = field(default_factory=list)

    def warnings_text(self) -> str:
        """Human readable summary of skipped/invalid rows (may be empty)."""
        if not self.skipped_rows:
            return ""
        sample = "; ".join(
            f"row: {row.get('Component') or '?'} / {row.get('Table') or '(no table)'}"
            for row in self.skipped_rows[:5]
        )
        more = f" (+{len(self.skipped_rows) - 5} more)" if len(self.skipped_rows) > 5 else ""
        return f"{len(self.skipped_rows)} row(s) skipped: {sample}{more}"


@dataclass
class GenerationResult:
    """Outcome of one :func:`generate_scripts` run."""

    output_dir: Path
    client: str = DEFAULT_CLIENT
    components: int = 0
    tables: int = 0
    generated: List[Path] = field(default_factory=list)
    #: Files not generated because both flags were ``0`` for the component.
    disabled_by_flags: List[Path] = field(default_factory=list)
    #: Existing files that were **not** overwritten (user/CLI said no or no
    #: confirmation callback was available with force off).
    refused: List[Path] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """``True`` when at least one file was produced and nothing failed."""
        return not self.errors

    def summary_lines(self) -> List[str]:
        """Plain-text summary (used by the CLI and the web log)."""
        lines = [
            f"Components processed : {self.components}",
            f"Tables listed        : {self.tables}",
            f"Files generated      : {len(self.generated)}",
            f"Disabled by flags    : {len(self.disabled_by_flags)}",
            f"Refused (existing)   : {len(self.refused)}",
            f"Errors               : {len(self.errors)}",
        ]
        for name in ("generated", "refused", "disabled_by_flags", "errors"):
            items: List[Union[Path, str]] = getattr(self, name)
            for item in items:
                lines.append(f"  [{name}] {item}")
        return lines


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


def load_csv(csv_filepath: Union[str, Path], delimiter: str = DEFAULT_DELIMITER) -> CSVLoadResult:
    """Read the CSV file and group its rows by the ``Component`` column.

    Args:
        csv_filepath: Path to the input CSV file.
        delimiter: One-character delimiter used in the file (default ``;``).

    Returns:
        A :class:`CSVLoadResult` holding the grouped rows and any rows that
        had to be skipped (missing ``Component``/``Table`` values).

    Raises:
        CSVFileNotFoundError: If the file does not exist or is not a file.
        CSVFormatError: If the file cannot be decoded/parsed or the header
            lacks the required columns.
    """
    path = Path(csv_filepath).expanduser()
    if not path.exists():
        raise CSVFileNotFoundError(
            f"Input CSV file not found at '{path}'. "
            "Please check the path and try again."
        )
    if not path.is_file():
        raise CSVFileNotFoundError(f"'{path}' exists but is not a regular file.")
    if not delimiter:
        raise CSVFormatError("CSV delimiter must be a single, non-empty character.")

    # Group rows by component, keeping original order per component.
    grouped: "defaultdict[str, list[dict]]" = defaultdict(list)
    skipped: List[dict] = []
    total = 0

    try:
        with open(path, mode="r", encoding="utf-8", newline="") as infile:
            reader = csv.DictReader(infile, delimiter=delimiter)
            if reader.fieldnames is None:
                raise CSVFormatError(
                    f"'{path}' is empty - no header row found. Expected header: "
                    "'Component;Table;Table description;Export;Import'."
                )
            header = [name.strip() for name in reader.fieldnames if name]
            missing = [col for col in REQUIRED_COLUMNS if col not in header]
            if missing:
                raise CSVFormatError(
                    f"CSV header is missing required column(s): {', '.join(missing)}. "
                    f"Found columns: {', '.join(header)}."
                )
            for row in reader:
                total += 1
                # DictReader maps the (possibly stripped) header onto the row;
                # normalise the keys so lookups do not depend on whitespace.
                clean = {
                    (k.strip() if k else k): (v or "").strip()
                    for k, v in row.items()
                    if k is not None
                }
                component = clean.get("Component", "")
                table = clean.get("Table", "")
                if not component or not table:
                    skipped.append(clean)
                    continue
                grouped[component].append(clean)
    except UnicodeDecodeError as exc:
        raise CSVFormatError(
            f"Cannot decode '{path}' as UTF-8: {exc}. "
            "Save the file as UTF-8 text and retry."
        ) from exc
    except csv.Error as exc:
        raise CSVFormatError(f"CSV parsing failed in '{path}': {exc}") from exc
    except OSError as exc:
        raise CSVFileNotFoundError(f"Cannot read '{path}': {exc}") from exc

    return CSVLoadResult(
        csv_filepath=path,
        delimiter=delimiter,
        grouped=dict(grouped),
        total_rows=total,
        skipped_rows=skipped,
    )


# ---------------------------------------------------------------------------
# Rendering (pure string builders - easy to unit test)
# ---------------------------------------------------------------------------


def resolve_exp_file(exp_file_base: "str | None", component: str, client: str) -> str:
    """Build the ``file = '...'`` value: optional base path + ``.exp`` name.

    The base path keeps its own flavour of separators:

    * ``""``                          -> ``Export_<COMP>_<CLNT>.exp``
    * ``/usr/sap/trans/data/SYS/``    -> ``/usr/sap/trans/data/SYS/Export_...``
    * ``D:\\export\\`` (or ``D:/export``) -> ``D:\\export\\Export_...``

    Mixing separators inside one base is normalised to the dominant one so
    that R3trans always sees a consistent path.
    """
    name = f"Export_{component}_{client}.exp"
    base = (exp_file_base or "").strip()
    if not base:
        return name
    if "\\" in base and "/" not in base:
        # Windows-style path: keep backslashes, drop any slashes.
        base = base.replace("/", "\\").rstrip("\\")
        return base + "\\" + name
    # POSIX-style (default): keep forward slashes.
    base = base.replace("\\", "/").rstrip("/")
    return base + "/" + name


def render_export_file(
    component: str,
    client: str,
    tables: Iterable[Mapping[str, str]],
    exp_file_base: str = "",
    exclude_tables: Iterable[str] = (),
) -> str:
    """Build the content of an R3trans **export** parameter file.

    Args:
        component: SAP component name (``Component`` column).
        client: Client identifier (``000``, ``100``, ``ALL``, ...).
        tables: Rows belonging to the component; each row must provide
            ``Table`` and may provide ``Table description``.
        exp_file_base: Optional directory for the ``.exp`` data file, e.g.
            ``/usr/sap/trans/data/SYS/`` or ``D:\\\\export\\\\``.  When given,
            the ``file = '...'`` line carries the **full path**; when empty
            (default) the file name only is written.
        exclude_tables: Optional collection of table names (matched
            case-insensitively) that must be **omitted** from the export
            content, e.g. ``("TADIR",)``.  All other tables are written as
            usual.

    Returns:
        The complete file content, ready to be written to disk.
    """
    excluded = {t.strip().upper() for t in exclude_tables if t and t.strip()}
    text = (
        "export\n"
        "\n"
        f"# {component}\n"
        "\n"
        f"client = {client}\n"
        f"file = '{resolve_exp_file(exp_file_base, component, client)}'\n"
        "buffersync=yes\n"
        "\n"
    )
    for info in tables:
        table_name = (info.get("Table") or "").strip()
        if not table_name:
            continue  # rows without a table name carry nothing to export
        if table_name.upper() in excluded:
            continue  # advanced option: drop this table from the export
        description = (info.get(DESC_COLUMN) or "No description").strip() or "No description"
        text += (
            f"# {description}\n"
            f"delete from {table_name}\n"
            f"select * from {table_name}\n"
            "\n"
        )
    return text


def render_import_file(component: str, client: str, exp_file_base: str = "") -> str:
    """Build the content of an R3trans **import** parameter file.

    Args:
        component: SAP component name (``Component`` column).
        client: Client identifier (``000``, ``100``, ``300``, ``ALL``, ...).
        exp_file_base: Optional directory for the ``.exp`` data file - see
            :func:`render_export_file`.

    Returns:
        The complete file content, ready to be written to disk.
    """
    return (
        "import\n"
        "\n"
        f"# {component}\n"
        "\n"
        f"file = '{resolve_exp_file(exp_file_base, component, client)}'\n"
        "\n"
        f"client={client}\n"
    )


# ---------------------------------------------------------------------------
# Plan / generation
# ---------------------------------------------------------------------------


def export_filename(component: str, client: str) -> str:
    """File name of the generated export script for a component."""
    return f"export_{component}_{client}.r3tr"


def import_filename(component: str, client: str) -> str:
    """File name of the generated import script for a component."""
    return f"import_{component}_{client}.r3tr"


def component_from_filename(name: str, client: str) -> Optional[str]:
    """Recover the component name from a generated ``*.r3tr`` file name.

    ``export_<COMP>_<CLIENT>.r3tr`` / ``import_<COMP>_<CLIENT>.r3tr`` ->
    ``<COMP>`` (the client suffix is stripped even when it contains
    underscores).  Returns ``None`` when the name does not match.
    """
    for prefix in ("export_", "import_"):
        if not name.startswith(prefix):
            continue
        mid = name[len(prefix):]
        for suffix in (f"_{client}.r3tr", ".r3tr"):
            if mid.endswith(suffix):
                comp = mid[: -len(suffix)]
                return comp or None
    return None


def build_generation_plan(
    grouped: Mapping[str, Sequence[Mapping[str, str]]],
    client: str = DEFAULT_CLIENT,
    components: Optional["set[str]"] = None,
    export_components: Optional["set[str]"] = None,
    import_components: Optional["set[str]"] = None,
    exclude_tables: Iterable[str] = (),
) -> "list[dict]":
    """Compute which files **would** be generated for the given data.

    Returns a list of dicts (one per component)::

        {"component": str, "tables": int,
         "export_enabled": bool, "import_enabled": bool,
         "export_filename": str, "import_filename": str}

    Args:
        grouped: Rows grouped by component (see :func:`load_csv`).
        client: Client identifier.
        components: Optional allow-list of component names (the "selection
            mode" of the UI).  When given, only those components end up in
            the plan; anything else is treated as *deselected* and ignored
            by both the plan and :func:`generate_scripts`.
        export_components: Optional explicit set of components that should
            get an **export** file, overriding the CSV ``Export`` flags.
            When ``None`` the flags are used (CLI / default behaviour).
        import_components: Same idea for the **import** files (CSV
            ``Import`` flags).
        exclude_tables: Table names (case-insensitive) to omit from the
            export content; only affects the reported ``export_tables``
            count, not the file selection.

    The plan is used by the UI for its pre-flight conflict check and by
    :func:`generate_scripts` itself.
    """
    excluded = {t.strip().upper() for t in exclude_tables if t and t.strip()}
    plan: List[dict] = []
    for component, tables in grouped.items():
        if components is not None and component not in components:
            continue
        if export_components is not None:
            do_export = component in export_components
        else:
            do_export = any((r.get("Export", "0") or "0").strip() == "1" for r in tables)
        if import_components is not None:
            do_import = component in import_components
        else:
            do_import = any((r.get("Import", "0") or "0").strip() == "1" for r in tables)

        listed = len(tables)
        if excluded:
            exported = sum(
                1 for r in tables
                if (r.get("Table") or "").strip() and (r.get("Table") or "").strip().upper() not in excluded
            )
        else:
            exported = listed

        plan.append(
            {
                "component": component,
                "tables": listed,
                "export_tables": exported if do_export else 0,
                "export_enabled": do_export,
                "import_enabled": do_import,
                "export_filename": export_filename(component, client),
                "import_filename": import_filename(component, client),
            }
        )
    return plan


def conflicting_paths(
    grouped: Mapping[str, Sequence[Mapping[str, str]]],
    output_dir: Union[str, Path],
    client: str = DEFAULT_CLIENT,
    components: Optional["set[str]"] = None,
    export_components: Optional["set[str]"] = None,
    import_components: Optional["set[str]"] = None,
) -> "list[Path]":
    """Return the target files that already exist (for overwrite prompts)."""
    outdir = Path(output_dir).expanduser()
    found: List[Path] = []
    for item in build_generation_plan(
        grouped, client, components=components,
        export_components=export_components, import_components=import_components,
    ):
        for key in ("export_filename", "import_filename"):
            candidate = outdir / item[key]
            if not item[key].endswith("*") and candidate.exists():
                found.append(candidate)
    return found


def generate_scripts(
    grouped: Mapping[str, Sequence[Mapping[str, str]]],
    output_dir: Union[str, Path],
    client: str = DEFAULT_CLIENT,
    force_overwrite: bool = False,
    confirm_overwrite: Optional[Callable[[Path], bool]] = None,
    components: Optional["set[str]"] = None,
    export_components: Optional["set[str]"] = None,
    import_components: Optional["set[str]"] = None,
    exp_file_base: str = "",
    overrides: Optional[Mapping[str, str]] = None,
    exclude_tables: Iterable[str] = (),
) -> GenerationResult:
    """Generate the R3trans export/import files for all components.

    * a component gets an **export** file when its export is enabled and an
      **import** file when its import is enabled — enabled by the CSV flags
      by default, or explicitly via ``export_components`` /
      ``import_components`` (UI checkbox state);
    * existing target files are overwritten only if ``force_overwrite`` is
      true **or** the ``confirm_overwrite`` callback answers ``True`` for
      that specific file;
    * files are written with UTF-8 encoding.

    Args:
        grouped: Rows grouped by component (see :func:`load_csv`).
        output_dir: Directory for the generated ``*.r3tr`` files; created
            with parents if missing.
        client: Client identifier written into the files (default ``ALL``).
        force_overwrite: Overwrite existing files without asking.
        confirm_overwrite: Optional callable ``path -> bool`` used to ask the
            user per file (CLI interactive prompt / web dialog).  When it is
            ``None`` and the file already exists, the file is **skipped**
            (recorded in :attr:`GenerationResult.refused`).
        components: Optional allow-list of component names - the UI's
            "selection" mode.  When given, all other components are left
            completely untouched (not even counted).
        export_components: Optional explicit set of components that receive
            an **export** file (overrides the CSV ``Export`` flags).  When
            ``None`` the CSV flags decide.
        import_components: Same for the **import** files (CSV ``Import``
            flags).
        exp_file_base: Optional directory for the ``.exp`` data file inside
            the ``file = '...'`` lines (see :func:`resolve_exp_file`).
        overrides: Optional mapping ``filename -> exact content`` used to
            replace the rendered text of selected files (the UI's "edit
            before save" mode).  The key is the file *name* (e.g.
            ``export_X_ALL.r3tr``); the value is written verbatim.
        exclude_tables: Optional collection of table names (case-insensitive)
            to omit from the exported content, e.g. ``("TADIR",)`` — the
            "advanced" exclusion option.

    Returns:
        A :class:`GenerationResult` describing everything that happened.

    Raises:
        NoDataError: If ``grouped`` is empty (or nothing left after the
            component selection).
        OutputDirError: If the output directory cannot be created.
        R3TRCoreError (subclass): If an individual file cannot be written
            - the error text is also collected in ``result.errors`` and the
            run continues with the remaining components.
    """
    outdir = Path(output_dir).expanduser()
    result = GenerationResult(output_dir=outdir, client=client)
    overrides = dict(overrides or {})
    selection = {c.strip() for c in components} if components else None

    selected_grouped = {
        component: tables
        for component, tables in grouped.items()
        if selection is None or component in selection
    }
    if not selected_grouped:
        raise NoDataError(
            "No data to process: the CSV contained no valid rows"
            + ("" if selection is None else " (or none of the selected components were found)")
        )

    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OutputDirError(
            f"Could not create output directory '{outdir}': {exc}"
        ) from exc
    if not outdir.is_dir():
        raise OutputDirError(f"'{outdir}' is not a directory.")

    def _write(path: Path, content: str) -> None:
        """Write one file honouring the overwrite policy; record the outcome."""
        if path.exists() and not force_overwrite:
            if confirm_overwrite is not None:
                allowed = confirm_overwrite(path)
            else:
                allowed = False
            if not allowed:
                result.refused.append(path)
                return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
        except OSError as exc:
            message = f"Could not write to file '{path}'. Reason: {exc}"
            result.errors.append(message)
            raise R3TRCoreError(message) from exc
        result.generated.append(path)

    for component, tables in selected_grouped.items():
        result.components += 1
        result.tables += len(tables)

        if export_components is not None:
            do_export = component in export_components
        else:
            do_export = any((r.get("Export", "0") or "0").strip() == "1" for r in tables)
        if import_components is not None:
            do_import = component in import_components
        else:
            do_import = any((r.get("Import", "0") or "0").strip() == "1" for r in tables)

        # --- export file -------------------------------------------------
        if do_export:
            export_name = export_filename(component, client)
            export_content = overrides.get(export_name)
            if export_content is None:
                export_content = render_export_file(
                    component, client, tables, exp_file_base, exclude_tables
                )
            _write(outdir / export_name, export_content)
        else:
            result.disabled_by_flags.append(
                outdir / export_filename(component, client)
            )

        # --- import file -------------------------------------------------
        if do_import:
            import_name = import_filename(component, client)
            import_content = overrides.get(import_name)
            if import_content is None:
                import_content = render_import_file(component, client, exp_file_base)
            _write(outdir / import_name, import_content)
        else:
            result.disabled_by_flags.append(
                outdir / import_filename(component, client)
            )

    return result
