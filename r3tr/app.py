"""NiceGUI web interface for the R3TR script generator.

Run with ``python run.py`` (default) or ``python -m r3tr.app`` and open
http://localhost:8080 - everything the CLI can do is available in the
browser:

* load a CSV (upload from disk *or* point at an existing server file),
* per component: tick/tick off **Export** and/or **Import** (defaults are
  loaded from the CSV flags), Select all / Deselect all,
* inspect the **tables and their descriptions** of any component,
* choose client, delimiter, output directory, overwrite policy and -
  optionally - the **full base path for the ``file = '...'`` line**,
* advanced option: **exclude the TADIR table** from the exports of the
  components that contain it (with a warning that it is part of the
  export files),
* generate the ``*.r3tr`` files - the **Generated files** section appears
  after the first run and the hidden-by-default **Run log** opens,
* **preview both files of a component** (export + import, scrollable),
  **edit and re-save** any generated file, download individually or ZIP,
* all right-hand sections are collapsible; only **Components** is open
  on first run;
* dark mode (persisted in browser storage).
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import FileResponse, Response
from nicegui import app as ng_app
from nicegui import ui

from . import TADIR, __version__
from .core import (
    R3TRCoreError,
    CSVLoadResult,
    GenerationResult,
    build_generation_plan,
    component_from_filename,
    conflicting_paths,
    export_filename,
    generate_scripts,
    import_filename,
    load_csv,
    render_export_file,
    render_import_file,
)

# ---------------------------------------------------------------------------
# Paths & state
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPLOADS_DIR = PROJECT_ROOT / "uploads"
SAMPLE_CSV = PROJECT_ROOT / "data" / "export_import_data.csv"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "generated_scripts"


class State:
    """Mutable app state shared by the UI handlers (one process, one user)."""

    def __init__(self) -> None:
        self.csv_path: Optional[Path] = None  #: active CSV (uploaded or given)
        self.load: Optional[CSVLoadResult] = None  #: last parsed data
        self.last_result: Optional[GenerationResult] = None  #: last generation run
        #: Server-side mapping filename -> Path for the last run.
        #: (ui.select option VALUES must stay JSON-serializable strings!)
        self.filename_map: "dict[str, Path]" = {}
        #: Per-component enablement (UI state, initialised from the CSV flags):
        #: component -> {"export": bool, "import": bool}
        self.flags: "dict[str, dict[str, bool]]" = {}
        #: Advanced option: exclude TADIR from the exported content.
        self.tadir_excluded: bool = False
        #: Components that contain a TADIR row (computed on parse).
        self.tadir_components: "list[str]" = []
        #: component currently shown in the "tables" details card.
        self.details_component: Optional[str] = None


state = State()
UPLOADS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# FastAPI download endpoints (files live in user-chosen directories, so a
# small dynamic route is cleaner than static-file mapping)
# ---------------------------------------------------------------------------


def _install_download_routes() -> None:
    """Register /r3tr-dl and /r3tr-dl.zip (NiceGUI exposes the FastAPI app)."""

    @ng_app.get("/r3tr-dl/{filename}")
    async def download_one(filename: str) -> Response:  # noqa: D401
        """Serve a single generated file from the last output directory."""
        if state.last_result is None:
            raise HTTPException(status_code=404, detail="No generation run yet.")
        outdir = state.last_result.output_dir.resolve()
        target = (outdir / filename).resolve()
        # Path-traversal guard: the target must stay inside the output dir.
        if outdir not in target.parents:
            raise HTTPException(status_code=400, detail="Invalid file name.")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"'{filename}' not found.")
        return FileResponse(target, filename=filename, media_type="application/octet-stream")

    @ng_app.get("/r3tr-dl.zip")
    async def download_zip() -> Response:
        """Bundle every generated file of the last run into a ZIP archive."""
        last_result = state.last_result
        if last_result is None or not last_result.generated:
            raise HTTPException(status_code=404, detail="No generated files.")
        outdir = last_result.output_dir.resolve()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            seen: set[Path] = set()
            for path in last_result.generated:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                arcname = resolved.name if outdir in resolved.parents else str(resolved)
                archive.write(resolved, arcname=arcname)
        stamp = last_result.client or "ALL"
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="r3tr_{stamp}.zip"'},
        )


_install_download_routes()


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def _resolve_csv() -> Optional[Path]:
    """The CSV the UI should work with (uploaded file wins, else sample)."""
    if state.csv_path is not None and state.csv_path.is_file():
        return state.csv_path
    if SAMPLE_CSV.is_file():
        return SAMPLE_CSV
    return None


def _real_delim(delim_el: "ui.select") -> str:
    """Translate the delimiter select value (incl. ``<TAB>``) to a char."""
    value = (str(delim_el.value) if delim_el.value else ";").strip()
    return {"<TAB>": "\t"}.get(value.upper(), value or ";")


def _client(client_input_el: "ui.input") -> str:
    value = (client_input_el.value or "ALL").strip()
    return value or "ALL"


def _exp_base(exp_input_el: "ui.input") -> str:
    return (exp_input_el.value or "").strip()


def _exclude_tables() -> tuple[str, ...]:
    """The table names to cut from exports (advanced option state)."""
    return (TADIR,) if state.tadir_excluded else ()


def _excluded_set() -> set[str]:
    return {t.strip().upper() for t in _exclude_tables() if t and t.strip()}


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


@ui.page("/")
def index() -> None:  # noqa: C901 - single-page UI, reads as one flow
    """Render the single-page generator UI."""
    ui.colors(primary="#0a6ed1")

    # -- dark mode (persisted) ---------------------------------------------
    dark_mode_el = ui.dark_mode()  #: single instance, reused by the toggle

    def _apply_dark(value: bool) -> None:
        if value:
            dark_mode_el.enable()
        else:
            dark_mode_el.disable()
        try:
            ng_app.storage.general["dark_mode"] = bool(value)
        except Exception:  # storage unavailable in some embedding modes
            pass

    initial_dark = True
    try:
        initial_dark = bool(ng_app.storage.general.get("dark_mode", True))
    except Exception:
        pass
    dark_value = initial_dark

    # -- header -------------------------------------------------------------
    with ui.header().classes("items-center bg-[#0b1f33] text-white gap-x-3"):
        ui.label("🛠 SAP R3TR generator").classes("text-lg font-bold")
        ui.label(
            "R3Trans export/import parameter files.  ·  v" + __version__
        ).classes("text-sm opacity-80")
        ui.space()
        dark_switch = ui.switch("Dark mode", value=dark_value).props("dense")

    def _set_dark(e) -> None:
        nonlocal dark_value
        dark_value = bool(e.value if e.value is not None else False)
        _apply_dark(dark_value)

    dark_switch.on_value_change(_set_dark)
    _apply_dark(dark_value)

    with ui.column().classes("w-full p-4 gap-3"):
        with ui.row().classes("w-full items-start gap-3 flex-wrap"):
            # ============================================================
            # LEFT column - numbered steps 1-4
            # ============================================================
            with ui.card().classes("w-[340px] gap-3 self-start"):
                ui.label("Data & settings").classes("text-base font-semibold")

                # -- 1 · data source --------------------------------------
                ui.label("1 · Data source").classes("text-base font-semibold")
                with ui.expansion("Upload a CSV file", icon="cloud_upload").classes(
                    "w-full"
                ):
                    csv_upload = ui.upload(
                        auto_upload=True,
                        max_files=1,
                        label="Drop CSV here or click to browse",
                    ).props("button accept='.csv,text/csv'").classes("w-full")

                with ui.expansion("…or use a CSV on this machine",
                                  icon="folder_open").classes("w-full"):
                    path_input = ui.input(
                        "CSV path",
                        value=str(SAMPLE_CSV) if SAMPLE_CSV.is_file() else "",
                        placeholder="/absolute/or/relative/path.csv",
                    ).classes("w-full mb-2")
                    ui.button(
                        "Use this path",
                        icon="description",
                        on_click=lambda: _pick_server_path(path_input),
                    ).props("outline no-caps").classes("w-full")
                ui.button(
                    "Parse data",
                    icon="table_view",
                    on_click=lambda: do_parse(client_input, delim_select, outdir_input),
                ).props("outline no-caps").classes("w-full")

                ui.separator()

                # -- 2 · generation settings --------------------------------
                ui.label("2 · Generation settings").classes("text-base font-semibold")
                delim_select = ui.select(
                    [";", ",", "<TAB>"],
                    value=";",
                    label="CSV delimiter",
                    with_input=False,
                ).classes("w-full")
                client_input = ui.input(
                    "Client (000 / 100 / 300 / ALL)",
                    value="ALL",
                    validation={
                        "Must not be empty": lambda v: bool((v or "").strip())
                    },
                ).props("dense outlined").classes("w-full")
                outdir_input = ui.input(
                    "Output directory",
                    value=str(DEFAULT_OUTPUT_DIR),
                ).props("dense outlined").classes("w-full")
                exp_input = ui.input(
                    "EXP file base path (optional)",
                    value="",
                    placeholder="e.g. /usr/sap/trans/data/SYS/  or  D:\\export\\",
                ).props("dense outlined").classes("w-full")
                ui.label(
                    "When set, the ``file = '...'`` lines carry the full path "
                    "(base path + .exp file name); otherwise the file name only."
                ).classes("text-sm opacity-80 -mt-1")
                force_check = ui.checkbox("Force overwrite existing files")

                ui.separator()

                # -- 3 · advanced options ----------------------------------
                with ui.expansion("3 · Advanced options", icon="tune").classes("w-full"):
                    tadir_check = ui.checkbox(
                        f"Exclude the {TADIR} table from exports"
                    )
                    tadir_note = ui.label("").classes("text-sm")

                ui.separator()

                # -- 4 · generate -------------------------------------------
                ui.button(
                    "4 · Generate R3TR scripts",
                    icon="build",
                    on_click=lambda: do_generate(
                        client_input, delim_select, outdir_input, force_check, exp_input
                    ),
                ).props("no-caps size=lg color=primary").classes("w-full")

            # ============================================================
            # RIGHT column - collapsible sections, all full width
            # ============================================================
            with ui.column().classes("flex-1 gap-3 min-w-[520px]"):
                # -- Components (visible on first run) ----------------------
                with ui.expansion(
                    "Components", icon="checklist"
                ).classes("w-full") as comp_box:
                    with ui.column().classes("w-full gap-2 px-2 pt-1"):
                        parse_meta = ui.label("").classes("text-sm opacity-70")
                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                            ui.label(
                                "Tick Export / Import per component - defaults come from the CSV"
                            ).classes("text-sm opacity-80")
                            sel_meta = ui.label("").classes("text-sm opacity-70")
                            ui.space()
                            _sel_props = "flat dense no-caps"
                            ui.button(
                                "Select all", icon="done_all",
                                on_click=lambda: _set_all(True),
                            ).props(_sel_props)
                            ui.button(
                                "Deselect all", icon="indeterminate_check_box",
                                on_click=lambda: _set_all(False),
                            ).props(_sel_props)
                            ui.button(
                                "Select Export", icon="check_box",
                                on_click=lambda: _set_kind("export", True),
                            ).props(_sel_props)
                            ui.button(
                                "Deselect Export", icon="check_box_outline_blank",
                                on_click=lambda: _set_kind("export", False),
                            ).props(_sel_props)
                            ui.button(
                                "Select Import", icon="check_box",
                                on_click=lambda: _set_kind("import", True),
                            ).props(_sel_props)
                            ui.button(
                                "Deselect Import", icon="check_box_outline_blank",
                                on_click=lambda: _set_kind("import", False),
                            ).props(_sel_props)
                            ui.button(
                                "Default from CSV", icon="settings_backup_restore",
                                on_click=lambda: _reset_defaults(),
                            ).props(_sel_props)
                        comp_container = ui.column().classes(
                            "w-full max-h-[420px] overflow-y-auto gap-0"
                        )
                        empty_hint = ui.label(
                            "Load the CSV (Parse data) to list the components."
                        ).classes("text-sm opacity-70")
                        warn_label = ui.label("").classes("text-sm text-amber-600")
                comp_box.open()

                # -- Tables + descriptions of a component --------------------
                with ui.expansion(
                    "Tables of a component", icon="table_chart"
                ).classes("w-full"):
                    with ui.column().classes("w-full gap-2 px-2 pt-1"):
                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                            details_select = ui.select(
                                {}, label="Component", with_input=False
                            ).props("dense outlined").classes("min-w-60 max-w-96")
                            ui.space()
                            details_meta = ui.label("").classes("text-sm opacity-70")
                        details_table = ui.table(
                            columns=[
                                {"name": "table", "label": "Table", "field": "table",
                                 "sortable": True, "align": "left", "min-width": "140px"},
                                {"name": "description", "label": "Description",
                                 "field": "description", "align": "left"},
                                {"name": "export", "label": "Export flag", "field": "export", "align": "center"},
                                {"name": "import", "label": "Import flag", "field": "import", "align": "center"},
                            ],
                            rows=[],
                            row_key="table",
                            pagination={"rowsPerPage": 50, "options": [50, 100, 250]},
                        ).classes("w-full").props("flat bordered dense")
                        details_tadir_note = ui.label("").classes(
                            "text-sm text-amber-600"
                        )

                # -- Generated files (appears after the first run) -----------
                with ui.expansion(
                    "Generated files", icon="description"
                ).classes("w-full") as gen_box:
                    with ui.column().classes("w-full gap-2 px-2 pt-1"):
                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                            result_meta = ui.label(
                                "No files generated yet."
                            ).classes("text-sm opacity-70")
                            ui.space()
                            zip_btn = ui.button(
                                "Download all (ZIP)",
                                icon="archive",
                                on_click=lambda: ui.navigate.to("/r3tr-dl.zip", new_tab=True),
                            ).props("outline no-caps")
                        result_table = ui.table(
                            columns=[
                                {"name": "file", "label": "File", "field": "file",
                                 "sortable": True, "align": "left", "min-width": "220px"},
                                {"name": "component", "label": "Component",
                                 "field": "component", "align": "left"},
                                {"name": "kind", "label": "Type", "field": "kind", "align": "left"},
                                {"name": "tables", "label": "Tables", "field": "tables", "align": "right"},
                                {"name": "size", "label": "Size", "field": "size", "align": "right"},
                            ],
                            rows=[],
                            row_key="file",
                            pagination=10,
                        ).classes("w-full").props("flat bordered dense")
                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                            file_select = ui.select(
                                {},
                                label="Generated file for edit / download",
                                with_input=False,
                            ).props("dense outlined").classes("min-w-72 max-w-[28rem]")
                            edit_file_btn = ui.button("Edit…", icon="edit")                                 .props("outline no-caps")
                            download_btn = ui.button("Download", icon="download")                                 .props("no-caps")
                            ui.space()

                # -- File content preview (export + import of one component) -
                with ui.expansion(
                    "File content preview", icon="code"
                ).classes("w-full") as preview_box:
                    with ui.column().classes("w-full gap-2 px-2 pt-1"):
                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                            preview_comp_select = ui.select(
                                {}, label="Component", with_input=False
                            ).props("dense outlined").classes("min-w-60 max-w-96")
                            preview_btn = ui.button(
                                "Preview", icon="visibility"
                            ).props("outline no-caps")
                            ui.space()
                            preview_meta = ui.label("").classes("text-sm opacity-70")
                        preview_panes = ui.column().classes("w-full gap-2")

                # -- Edit before save (single component) ---------------------
                with ui.expansion(
                    "Edit before save (single component)", icon="edit"
                ).classes("w-full"):
                    with ui.column().classes("w-full gap-2 px-2 pt-1"):
                        edit_status = ui.label(
                            "Choose a component, load, edit, write."
                        ).classes("text-sm opacity-70")
                        with ui.row().classes("w-full items-center gap-2 flex-wrap"):
                            edit_comp_select = ui.select(
                                {}, label="Component", with_input=False
                            ).props("dense outlined").classes("min-w-56 max-w-96")
                            load_edit_btn = ui.button(
                                "Load content", icon="input",
                                on_click=lambda: _load_edit_content(
                                    edit_comp_select, edit_export_ta, edit_import_ta,
                                    client_input, exp_input, edit_status
                                ),
                            ).props("outline no-caps")
                            write_edit_btn = ui.button(
                                "Write to disk", icon="save",
                                on_click=lambda: _write_edit_content(
                                    edit_comp_select, edit_export_ta, edit_import_ta,
                                    client_input, outdir_input, exp_input, force_check,
                                ),
                            ).props("no-caps")
                            reset_edit_btn = ui.button(
                                "Reset", icon="restart_alt",
                                on_click=lambda: _reset_edit(
                                    edit_export_ta, edit_import_ta, edit_status
                                ),
                            ).props("flat no-caps")
                        with ui.row().classes("w-full items-start gap-4"):
                            with ui.column().classes("flex-1 gap-1 min-w-[280px]"):
                                edit_export_label = ui.label("").classes(
                                    "text-sm font-mono opacity-80"
                                )
                                edit_export_ta = ui.textarea(label="Export file content")                                     .props("filled outlined rows=12 maxrows=20")                                     .classes("w-full font-mono text-sm")
                            with ui.column().classes("flex-1 gap-1 min-w-[280px]"):
                                edit_import_label = ui.label("").classes(
                                    "text-sm font-mono opacity-80"
                                )
                                edit_import_ta = ui.textarea(label="Import file content")                                     .props("filled outlined rows=12 maxrows=20")                                     .classes("w-full font-mono text-sm")

                # -- Run log (hidden by default) -----------------------------
                with ui.expansion("Run log", icon="receipt_long").classes(
                    "w-full"
                ) as log_box:
                    run_log = ui.log(max_lines=5000).classes(
                        "w-full h-64 bg-[#0d1117] text-green-300 text-sm"
                    )

    # ======================================================================
    # shared closures & handlers
    # ======================================================================

    def _pick_server_path(input_el: "ui.input") -> None:
        """Point the tool at a CSV file that already exists on this machine."""
        raw = (input_el.value or "").strip()
        if not raw:
            ui.notify("Type a CSV path first.", type="warning")
            return
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        if not candidate.is_file():
            ui.notify(f"File not found: {candidate}", type="negative")
            return
        state.csv_path = candidate
        ui.notify(f"Using CSV: {candidate}", type="positive")

    def _on_upload(e) -> None:
        """Store the uploaded file in uploads/ and make it the active CSV."""
        try:
            target = UPLOADS_DIR / (e.name or "uploaded.csv")
            with open(target, "wb") as handle:
                handle.write(e.content.read())
            state.csv_path = target
            ui.notify(f"Uploaded and selected: {e.name}", type="positive")
            _auto_parse_all()
        except OSError as exc:
            ui.notify(f"Upload failed: {exc}", type="negative")

    csv_upload.on_upload(_on_upload)

    # ------------------------------------------------------------------
    # per-component flags (Export / Import toggles)
    # ------------------------------------------------------------------
    #: component -> (export checkbox, import checkbox) element registry
    checkboxes: "dict[str, tuple[ui.checkbox, ui.checkbox]]" = {}

    def _update_sel_meta() -> None:
        total = len(state.flags)
        picked = sum(
            1 for f in state.flags.values() if f.get("export") or f.get("import")
        )
        sel_meta.text = f"{picked}/{total} component(s) enabled for generation"

    def _csv_defaults() -> "dict[str, dict[str, bool]]":
        """Per-component export/import defaults derived from the CSV flags."""
        if state.load is None:
            return {}
        result: "dict[str, dict[str, bool]]" = {}
        for comp, rows in state.load.grouped.items():
            result[comp] = {
                "export": any(
                    (r.get("Export", "0") or "0").strip() == "1" for r in rows
                ),
                "import": any(
                    (r.get("Import", "0") or "0").strip() == "1" for r in rows
                ),
            }
        return result

    def _apply_flags_to_ui() -> None:
        """Push ``state.flags`` into the per-row checkboxes (both kinds)."""
        for comp, flags in state.flags.items():
            if comp not in checkboxes:
                continue
            exp_cb, imp_cb = checkboxes[comp]
            exp_cb.set_value(bool(flags.get("export")))
            imp_cb.set_value(bool(flags.get("import")))
        _update_sel_meta()

    def _set_all(value: bool) -> None:
        """Tick (True) or untick (False) both toggles of every component."""
        if not state.flags:
            ui.notify("Load the CSV first (Parse data).", type="warning")
            return
        for flags in state.flags.values():
            flags["export"] = value
            flags["import"] = value
        _apply_flags_to_ui()

    def _set_kind(kind: str, value: bool) -> None:
        """Tick/untick one kind of toggle (``export`` or ``import``) for all."""
        if not state.flags:
            ui.notify("Load the CSV first (Parse data).", type="warning")
            return
        for flags in state.flags.values():
            flags[kind] = value
        _apply_flags_to_ui()

    def _reset_defaults() -> None:
        """Reload the export/import toggles from the CSV flags."""
        if state.load is None:
            ui.notify("Load the CSV first (Parse data).", type="warning")
            return
        state.flags = _csv_defaults()
        _apply_flags_to_ui()
        ui.notify("Toggles reset to the CSV defaults.", type="info")

    def _make_check(comp: str, kind: str) -> "ui.checkbox":
        checked = bool(state.flags.get(comp, {}).get(kind, False))
        label = "Export" if kind == "export" else "Import"
        cb = ui.checkbox(label, value=checked).props("dense")

        def _toggle(e, comp=comp, kind=kind) -> None:
            if comp in state.flags:
                state.flags[comp][kind] = bool(e.value)
            _update_sel_meta()

        cb.on_value_change(_toggle)
        return cb

    def _build_components_list() -> None:
        """(Re)build the component rows from ``state.flags``."""
        comp_container.clear()
        checkboxes.clear()
        empty_hint.set_visibility(not bool(state.flags))
        if not state.flags:
            return
        for comp in sorted(state.flags):
            rows = state.load.grouped.get(comp, []) if state.load else []
            with comp_container:
                with ui.row().classes(
                    "w-full items-center gap-3 px-2 py-1.5 pr-6 border-b "
                    "border-gray-200 dark:border-gray-700"
                ):
                    ui.label(comp).classes("flex-1 text-sm font-medium truncate")
                    ui.label(f"{len(rows)} tbl").classes(
                        "text-xs opacity-70 w-16 text-right"
                    )
                    exp_cb = _make_check(comp, "export")
                    imp_cb = _make_check(comp, "import")
                    checkboxes[comp] = (exp_cb, imp_cb)
                    ui.button(
                        icon="view_list",
                        on_click=lambda c=comp: _show_details(c),
                    ).props("flat dense round size=sm").tooltip(
                        "Tables of this component"
                    )
        _update_sel_meta()

    def _effective_selection() -> "tuple[set[str], set[str]]":
        """(export_components, import_components) from the UI toggles."""
        export_components = {c for c, f in state.flags.items() if f.get("export")}
        import_components = {c for c, f in state.flags.items() if f.get("import")}
        return export_components, import_components

    # ------------------------------------------------------------------
    # tables + descriptions of a component (details card)
    # ------------------------------------------------------------------
    def _show_details(comp: str) -> None:
        state.details_component = comp
        details_select.value = comp

    def _rebuild_details() -> None:
        comp = details_select.value
        if state.load is None or comp not in state.load.grouped:
            details_table.rows = []
            details_table.update()
            details_meta.text = ""
            details_tadir_note.text = ""
            return
        rows = state.load.grouped[comp]
        excl = _excluded_set()
        data = []
        for row in rows:
            table = (row.get("Table") or "").strip()
            desc = (row.get("Table description") or "No description").strip() \
                or "No description"
            exp_flag = (row.get("Export", "0") or "0").strip() == "1"
            imp_flag = (row.get("Import", "0") or "0").strip() == "1"
            exp_text = "✅" if exp_flag else "—"
            if excl and exp_flag and table.upper() in excl:
                exp_text = "⚠ EXCLUDED"
            data.append(
                {
                    "table": table,
                    "description": desc,
                    "export": exp_text,
                    "import": "✅" if imp_flag else "—",
                }
            )
        details_table.rows = data
        details_table.update()
        details_meta.text = f"{comp} · {len(rows)} table(s)"
        if excl:
            has = any((r.get("Table") or "").strip().upper() in excl for r in rows)
            details_tadir_note.text = (
                "Note: TADIR is excluded from the export of this component "
                "(advanced option enabled)." if has else ""
            )
        else:
            details_tadir_note.text = ""

    def _tadir_changed(e) -> None:
        """Advanced option: exclude TADIR from exports + warning message."""
        state.tadir_excluded = bool(e.value)
        _update_tadir_note()
        if e.value and state.tadir_components:
            ui.notify(
                f"⚠ {TADIR} is part of the export files of "
                f"{len(state.tadir_components)} component(s): "
                f"{', '.join(state.tadir_components[:10])}"
                + ("…" if len(state.tadir_components) > 10 else ""),
                type="warning",
                multi_line=True,
            )
            ui.notify(
                f"{TADIR} is now excluded from those exports.",
                type="info",
            )
        elif e.value:
            ui.notify(
                "TADIR exclusion enabled (the table is not present in the "
                "current data, so nothing is removed).",
                type="info",
            )
        _rebuild_details()

    def _update_tadir_note() -> None:
        if not state.tadir_components:
            tadir_note.text = f"{TADIR} is not present in the current data."
            tadir_note.classes(replace="text-xs opacity-80")
            return
        if state.tadir_excluded:
            tadir_note.text = (
                f"{TADIR} will be EXCLUDED from the exports of: "
                f"{', '.join(state.tadir_components)}."
            )
            tadir_note.classes(replace="text-xs text-amber-600 font-medium")
        else:
            tadir_note.text = (
                f"⚠ {TADIR} is part of the export files of: "
                f"{', '.join(state.tadir_components)}. "
                "Enable the option to remove it from the exports."
            )
            tadir_note.classes(replace="text-xs text-amber-600")

    tadir_check.on_value_change(_tadir_changed)

    # ------------------------------------------------------------------
    # parse
    # ------------------------------------------------------------------
    def _refresh_preview() -> None:
        """Rebuild the component list + selectors from ``state.load``.

        Shared by an explicit "Parse data" click and by session restore on
        page (re)load, where the server state survives but the freshly
        rendered elements are empty again.
        """
        if state.load is None:
            return

        # defaults for the toggles come from the CSV flags; keep the user's
        # current toggles where we already saw the component (reload case)
        fresh = {
            comp: {
                "export": any(
                    (r.get("Export", "0") or "0").strip() == "1" for r in rows
                ),
                "import": any(
                    (r.get("Import", "0") or "0").strip() == "1" for r in rows
                ),
            }
            for comp, rows in state.load.grouped.items()
        }
        for comp, flags in fresh.items():
            if comp in state.flags:
                flags["export"] = state.flags[comp].get(
                    "export", flags["export"]
                )
                flags["import"] = state.flags[comp].get(
                    "import", flags["import"]
                )
        state.flags = fresh

        state.tadir_components = sorted(
            comp
            for comp, rows in state.load.grouped.items()
            if any(
                (r.get("Table") or "").strip().upper() == TADIR for r in rows
            )
        )
        _update_tadir_note()
        _build_components_list()

        # details card + edit-before-save selector
        names = sorted(state.load.grouped)
        details_select.options = names
        details_select.update()
        if state.details_component and state.details_component in names:
            details_select.value = state.details_component
        else:
            state.details_component = names[0] if names else None
            details_select.value = state.details_component
        _rebuild_details()

        edit_comp_select.options = names
        edit_comp_select.update()
        if not edit_comp_select.value:
            edit_comp_select.value = state.details_component or (names[0] if names else None)

        # component-based preview selector
        preview_comp_select.options = names
        preview_comp_select.update()
        if not preview_comp_select.value:
            preview_comp_select.value = state.details_component or (names[0] if names else None)

        parse_meta.text = (
            f"{state.csv_path.name if state.csv_path else 'CSV'}: "
            f"{state.load.total_rows} rows, {len(names)} components"
        )
        warn_label.text = state.load.warnings_text() or ""

    def _auto_parse_all() -> None:
        do_parse(client_input, delim_select, outdir_input)

    def do_parse(
        client_input_el: "ui.input", delim_el: "ui.select", outdir_el: "ui.input"
    ) -> None:
        """Parse the active CSV and refresh the component list + details."""
        csv_path = _resolve_csv()
        if csv_path is None:
            ui.notify("No CSV available - upload a file first.", type="negative")
            return
        try:
            load = load_csv(csv_path, _real_delim(delim_el))
        except R3TRCoreError as exc:
            ui.notify(f"CSV problem: {exc}", type="negative", multi_line=True)
            return

        state.load = load
        state.csv_path = csv_path
        _refresh_preview()
        ui.notify(
            f"Parsed {len(state.flags)} components from {csv_path.name}",
            type="positive",
        )

    # ------------------------------------------------------------------
    # generate (all enabled components)
    # ------------------------------------------------------------------
    def do_generate(
        client_input_el: "ui.input",
        delim_el: "ui.select",
        outdir_el: "ui.input",
        force_el: "ui.checkbox",
        exp_el: "ui.input",
    ) -> None:
        """Generate R3TR files for the components with a ticked Export/Import.

        When existing target files are found (and *force* is off) the user
        is asked in a dialog - non-blocking for the event loop.
        """
        if state.load is None:
            do_parse(client_input_el, delim_el, outdir_el)
        if state.load is None or not state.load.grouped:
            ui.notify(
                "Nothing to generate - parse a valid CSV first.", type="negative"
            )
            return

        client = _client(client_input_el)
        exp_base = _exp_base(exp_el)
        export_components, import_components = _effective_selection()
        selected = export_components | import_components
        if not selected:
            ui.notify(
                "No component enabled - tick Export or Import for at least "
                "one component.",
                type="warning",
            )
            return
        outdir = Path((outdir_el.value or str(DEFAULT_OUTPUT_DIR))).expanduser()
        force = bool(force_el.value)
        grouped = state.load.grouped  # captured for the closures below
        excl = _exclude_tables()

        def _run(effective_force: bool) -> None:
            import traceback as _tb

            try:
                run_log.clear()
                run_log.push(
                    "Enabled            : "
                    f"{len(selected)} component(s) "
                    f"({len(export_components)} export, "
                    f"{len(import_components)} import)"
                )
                run_log.push(f"Output directory : {outdir}")
                run_log.push(f"Client           : {client}")
                run_log.push(
                    "EXP file base    : "
                    f"{exp_base if exp_base else '(none — file name only)'}"
                )
                if excl:
                    run_log.push(
                        f"Excluded tables  : {', '.join(excl)} "
                        f"(present in: {', '.join(state.tadir_components) or 'n/a'})"
                    )
                run_log.push(
                    "Overwrite policy : "
                    f"{'FORCE' if effective_force else 'ask / skip'}"
                )
                result = generate_scripts(
                    grouped,
                    outdir,
                    client=client,
                    force_overwrite=effective_force,
                    components=selected,
                    export_components=export_components,
                    import_components=import_components,
                    exp_file_base=exp_base,
                    exclude_tables=excl,
                )
                state.last_result = result

                for line in result.summary_lines():
                    run_log.push(line)
                refresh_results(client_input_el)
                log_box.open()
                gen_box.open()

                if result.generated and not result.errors:
                    ui.notify(
                        f"Generated {len(result.generated)} file(s) in {outdir}",
                        type="positive",
                    )
                elif result.errors:
                    ui.notify("Finished with errors - see log.", type="negative")
                else:
                    ui.notify(
                        "No files were generated (all disabled or skipped).",
                        type="warning",
                    )
            except Exception as exc:  # noqa: BLE001 - surface ANY failure in the log
                try:
                    run_log.push(f"ERROR: {exc!r}")
                except Exception:
                    pass
                _tb.print_exc()
                try:
                    ui.notify(
                        f"Generation failed: {exc!r}", type="negative", multi_line=True
                    )
                except Exception:
                    pass

        # --- overwrite policy (mirrors the original confirm dialog) -------
        # NOTE: the continuations MUST be called directly from the button
        # handlers.  A future/callback continuation runs in a bare asyncio
        # task whose slot stack is empty, so any ui.notify() inside it
        # raises "current slot cannot be determined".
        if not force:
            conflicts = conflicting_paths(
                grouped,
                outdir,
                client,
                components=selected,
                export_components=export_components,
                import_components=import_components,
            )
            if conflicts:
                with ui.dialog() as ask_dialog, ui.card().classes(
                    "min-w-[440px] gap-2"
                ):
                    ui.label(
                        f"{len(conflicts)} target file(s) already exist. Overwrite?"
                    ).classes("text-base font-semibold")
                    for p in conflicts[:8]:
                        ui.label(f"· {p.name}").classes("text-xs font-mono pl-4")
                    if len(conflicts) > 8:
                        ui.label(
                            f"· and {len(conflicts) - 8} more…"
                        ).classes("text-xs pl-4")
                    with ui.row().classes("w-full justify-end gap-2 mt-2"):
                        ui.button("Cancel", on_click=lambda: ask_dialog.close())
                        ui.button(
                            "Skip existing",
                            on_click=lambda: _answer(ask_dialog, "skip"),
                        ).props("outline no-caps")
                        ui.button(
                            "Overwrite all",
                            on_click=lambda: _answer(ask_dialog, "overwrite"),
                        ).props("color=warning no-caps")

                def _answer(dlg, choice: str) -> None:
                    dlg.close()
                    if choice == "overwrite":
                        _run(True)
                    elif choice == "skip":
                        _run(False)
                ask_dialog.open()
                return
        _run(force)

    # ------------------------------------------------------------------
    # edit-before-save (single component)
    # ------------------------------------------------------------------
    def _flags_for(component: str) -> "tuple[bool, bool]":
        """Effective (export, import) enablement of one component."""
        if component in state.flags:
            f = state.flags[component]
            return bool(f.get("export")), bool(f.get("import"))
        # fall back to the CSV flags (defensive - parse should have filled it)
        if state.load is None:
            return False, False
        rows = state.load.grouped.get(component, [])
        do_export = any((r.get("Export", "0") or "0").strip() == "1" for r in rows)
        do_import = any((r.get("Import", "0") or "0").strip() == "1" for r in rows)
        return do_export, do_import

    def _load_edit_content(
        comp_el: "ui.select",
        export_ta: "ui.textarea",
        import_ta: "ui.textarea",
        client_el: "ui.input",
        exp_el: "ui.input",
        status_el: "ui.label",
    ) -> None:
        """Render the (un-edited) content of one component into the editor."""
        component = comp_el.value
        if state.load is None or component not in state.load.grouped:
            ui.notify("Parse data first and pick a known component.", type="warning")
            return
        client = _client(client_el)
        exp_base = _exp_base(exp_el)
        do_export, do_import = _flags_for(component)
        excl = _exclude_tables()

        rows = state.load.grouped[component]
        export_text = (
            render_export_file(component, client, rows, exp_base, excl)
            if do_export else ""
        )
        import_text = (
            render_import_file(component, client, exp_base) if do_import else ""
        )

        export_ta.value = export_text
        export_ta.disable() if not do_export else export_ta.enable()
        import_ta.value = import_text
        import_ta.disable() if not do_import else import_ta.enable()

        extra = " (TADIR excluded)" if do_export and excl and TADIR in excl else ""
        edit_export_label.text = (
            f"→ {export_filename(component, client)}"
            if do_export else "Export disabled (tick Export in the list)"
        ) + extra
        edit_import_label.text = (
            f"→ {import_filename(component, client)}"
            if do_import else "Import disabled (tick Import in the list)"
        )
        status_el.text = (
            f"Loaded {component} (client {client}) — edit freely, then "
            "'Write to disk'."
        )
        ui.notify(
            f"Loaded {component} into the editor"
            + (" (export only)" if do_export and not do_import
               else " (import only)" if do_import and not do_export else ""),
            type="info",
        )

    def _reset_edit(
        export_ta: "ui.textarea", import_ta: "ui.textarea", status_el: "ui.label"
    ) -> None:
        export_ta.value = ""
        import_ta.value = ""
        export_ta.enable()
        import_ta.enable()
        edit_export_label.text = ""
        edit_import_label.text = ""
        status_el.text = "Editor cleared."

    def _write_edit_content(
        comp_el: "ui.select",
        export_ta: "ui.textarea",
        import_ta: "ui.textarea",
        client_el: "ui.input",
        outdir_el: "ui.input",
        exp_el: "ui.input",
        force_el: "ui.checkbox",
    ) -> None:
        """Write the edited content of one component to disk (overwrite policy
        respected; the edited text replaces the rendered text verbatim)."""
        component = comp_el.value
        if state.load is None or component not in state.load.grouped:
            ui.notify("Nothing loaded for this component.", type="warning")
            return
        client = _client(client_el)
        exp_base = _exp_base(exp_el)
        outdir = Path((outdir_el.value or str(DEFAULT_OUTPUT_DIR))).expanduser()
        grouped = state.load.grouped if state.load is not None else {}
        do_export, do_import = _flags_for(component)

        overrides: "dict[str, str]" = {}
        if do_export:
            overrides[export_filename(component, client)] = export_ta.value or ""
        if do_import:
            overrides[import_filename(component, client)] = import_ta.value or ""
        if not overrides:
            ui.notify(
                "Both Export and Import are off for this component - nothing "
                "to write (tick the toggles in the Components card).",
                type="warning",
            )
            return

        def _commit(effective_force: bool) -> None:
            try:
                run_log.clear()
                run_log.push(f"EDITED WRITE : {component} (client {client})")
                result = generate_scripts(
                    grouped,
                    outdir,
                    client=client,
                    force_overwrite=effective_force,
                    components={component},
                    export_components={component} if do_export else set(),
                    import_components={component} if do_import else set(),
                    exp_file_base=exp_base,
                    overrides=overrides,
                    exclude_tables=_exclude_tables(),
                )
                _merge_into_last_result(result)
                for line in result.summary_lines()[:6]:
                    run_log.push(line)
                refresh_results(client_el)
                log_box.open()
                if result.generated:
                    ui.notify(
                        f"Written {len(result.generated)} file(s) for {component} "
                        f"to {outdir}",
                        type="positive",
                    )
                elif result.refused:
                    ui.notify(
                        "Not written - file exists and overwrite was skipped.",
                        type="warning",
                    )
            except Exception as exc:  # noqa: BLE001
                try:
                    run_log.push(f"ERROR: {exc!r}")
                except Exception:
                    pass
                ui.notify(f"Write failed: {exc!r}", type="negative", multi_line=True)

        if not force_el.value:
            existing = [outdir / name for name in overrides if (outdir / name).exists()]
            if existing:
                with ui.dialog() as ask_dialog, ui.card().classes(
                    "min-w-[420px] gap-2"
                ):
                    ui.label(
                        f"{len(existing)} existing file(s) would be overwritten "
                        f"({', '.join(p.name for p in existing[:4])}…). Overwrite?"
                    ).classes("text-base font-semibold")
                    with ui.row().classes("w-full justify-end gap-2 mt-2"):
                        ui.button("Cancel", on_click=lambda: ask_dialog.close())
                        ui.button(
                            "Skip", on_click=lambda: _answer(ask_dialog, "skip")
                        ).props("outline no-caps")
                        ui.button(
                            "Overwrite",
                            on_click=lambda: _answer(ask_dialog, "overwrite"),
                        ).props("color=warning no-caps")

                def _answer(dlg, choice: str) -> None:
                    dlg.close()
                    if choice == "overwrite":
                        _commit(True)
                    elif choice == "skip":
                        _commit(False)
                    # "cancel" -> do nothing

                ask_dialog.open()
                return
        _commit(True if force_el.value else False)

    def _merge_into_last_result(result: GenerationResult) -> None:
        """Fold a single-file (edited) run into the remembered last result so
        the results table / ZIP / downloads keep showing the full run."""
        prev = state.last_result
        if prev is None or prev.output_dir != result.output_dir:
            state.last_result = result
            return
        for path in result.generated:
            if path not in prev.generated:
                prev.generated.append(path)
        for path in result.refused:
            if path not in prev.refused:
                prev.refused.append(path)
        prev.errors.extend(result.errors)
        state.last_result = prev

    # ------------------------------------------------------------------
    # results panel
    # ------------------------------------------------------------------
    def refresh_results(client_input_el: "ui.input") -> None:
        """Rebuild the results table and the file pickers after a run."""
        result = state.last_result
        if result is None:
            return

        client = _client(client_input_el)
        plan_by_component = {}
        if state.load is not None:
            plan_by_component = {
                item["component"]: item
                for item in build_generation_plan(state.load.grouped, client)
            }

        data: "list[dict]" = []
        for path in result.generated:
            name = path.name
            comp = component_from_filename(name, client) or name
            try:
                size = f"{path.stat().st_size / 1024:.1f} KB"
            except OSError:
                size = "?"
            data.append(
                {
                    "file": name,
                    "component": comp,
                    "kind": "Import" if name.startswith("import_") else "Export",
                    "tables": plan_by_component.get(comp, {}).get("tables", 0),
                    "size": size,
                    "path": path,
                }
            )

        result_table.rows = [
            {k: v for k, v in item.items() if k != "path"} for item in data
        ]
        result_table.update()

        # Keep the select in sync.  Option VALUES are plain filename strings
        # (nicegui syncs options to the browser as JSON, so Path objects are
        # not allowed here) - the Path lookup lives in state.filename_map.
        state.filename_map = {item["path"].name: item["path"] for item in data}
        file_select.options = sorted(name for name in state.filename_map)
        file_select.update()
        if state.filename_map and file_select.value not in state.filename_map:
            file_select.value = sorted(state.filename_map)[0]

        suffix = (
            f" - {len(result.refused)} skipped (already existed)"
            if result.refused else ""
        )
        result_meta.text = (
            f"{len(result.generated)} file(s) in {result.output_dir}{suffix}"
        )

    def show_preview(component: str) -> None:
        """Render the export + import file of one component (both panes).

        Each pane uses ``ui.code`` with a *fixed* height (``h-72``) because
        NiceGUI's markdown renderer inside it uses ``height: 100%`` and
        ``overflow: auto`` - a ``max-height`` parent would make that inner
        element fall back to ``auto`` and the code would overflow instead of
        scrolling (fixed for long files).
        """
        comp = (component or "").strip()
        if state.load is None or comp not in state.load.grouped:
            ui.notify("Parse data first and pick a known component.", type="warning")
            return
        client = _client(client_input)
        exp_base = _exp_base(exp_input)
        do_export, do_import = _flags_for(comp)
        excl = _exclude_tables()
        rows = state.load.grouped[comp]
        exp_text = (
            render_export_file(comp, client, rows, exp_base, excl) if do_export else None
        )
        imp_text = render_import_file(comp, client, exp_base) if do_import else None

        preview_panes.clear()
        with preview_panes:
            for name, kind, text, enabled in (
                (export_filename(comp, client), "Export", exp_text, do_export),
                (import_filename(comp, client), "Import", imp_text, do_import),
            ):
                if enabled:
                    ui.label(
                        f"→ {name} · {len(text.splitlines())} lines · "
                        f"{len(text) / 1024:.1f} KB (scrollable)"
                    ).classes("text-sm font-mono opacity-80")
                    ui.code(text or "", language="none").classes("w-full h-72")
                else:
                    ui.label(f"→ {name}").classes("text-sm font-mono opacity-80")
                    ui.label(
                        f"{kind} is disabled for {comp} "
                        f"(tick {kind} in the Components list)."
                    ).classes("text-sm text-amber-600")
        preview_meta.text = (
            f"client {client}"
            + (" · TADIR excluded" if do_export and excl and TADIR in excl else "")
        )
        preview_box.open()
        ui.notify(f"Previewing {comp}", type="info")

    def _preview_component() -> None:
        show_preview(preview_comp_select.value or state.details_component or "")

    def selected_path() -> Optional[Path]:
        """The Path behind the current file-select entry (if any)."""
        name = file_select.value
        return state.filename_map.get(name) if name else None

    def _edit_selected() -> None:
        """Open the content of the selected file in an editor dialog."""
        path = selected_path()
        if path is None:
            ui.notify("Pick a file from the table first.", type="warning")
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            ui.notify(f"Cannot read {path.name}: {exc}", type="negative")
            return

        with ui.dialog() as edit_dlg, ui.card().classes(
            "w-[720px] max-w-[94vw] gap-3"
        ):
            ui.label(f"Editing {path.name}").classes("text-base font-semibold")
            ui.label(
                "Changes are applied to the file on disk when you press "
                "'Save to disk'. 'Discard' keeps the file as it is."
            ).classes("text-xs opacity-70")
            edit_ta = ui.textarea(value=text, label="Content").props(
                "filled outlined rows=18 maxrows=24"
            ).classes("w-full font-mono text-xs")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Discard", on_click=lambda: edit_dlg.close())
                ui.button(
                    "Save to disk",
                    icon="save",
                    on_click=lambda: _do_save_file(path, edit_ta, edit_dlg),
                ).props("no-caps")

        def _do_save_file(target: Path, ta: "ui.textarea", dlg) -> None:
            content = ta.value or ""
            try:
                target.write_text(content, encoding="utf-8")
                run_log.push(f"EDIT SAVED : {target.name} ({len(content)} chars)")
                log_box.open()
                gen_box.open()
                ui.notify(f"Saved {target.name}", type="positive")
                comp = component_from_filename(
                    target.name, _client(client_input)
                )
                if comp:
                    show_preview(comp)
                refresh_results(client_input)
                dlg.close()
            except OSError as exc:
                ui.notify(f"Save failed: {exc}", type="negative")

        edit_dlg.open()

    def _download_selected() -> None:
        if not file_select.value:
            ui.notify("Pick a file from the table first.", type="warning")
            return
        ui.navigate.to(f"/r3tr-dl/{file_select.value}", new_tab=True)

    preview_btn.on_click(_preview_component)
    edit_file_btn.on_click(_edit_selected)
    download_btn.on_click(_download_selected)
    details_select.on_value_change(lambda e: _rebuild_details())

    # ------------------------------------------------------------------
    # session restore: if a previous page in this browser session already
    # parsed data / generated files, repopulate the freshly rendered UI
    # (server-side ``state`` survives a reload; the elements do not).
    # ------------------------------------------------------------------
    if state.load is not None:
        _refresh_preview()
    if state.last_result is not None:
        refresh_results(client_input)
        run_log.push(
            f"Session restored: {len(state.last_result.generated)} "
            f"file(s) from last run."
        )
        log_box.open()
        gen_box.open()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Start the NiceGUI web server (blocks until shutdown)."""
    ui.run(
        host=host,
        port=port,
        title="R3TR generator",
        favicon="🛠",
        show=False,  # do not try to open a browser on the server host
        reload=False,
    )


if __name__ in {"__main__", "__mp_main__"}:
    run(
        host=os.environ.get("R3TR_UI_HOST", "127.0.0.1"),
        port=int(os.environ.get("R3TR_UI_PORT", "8080")),
    )
