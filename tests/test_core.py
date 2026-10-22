"""Unit tests for the framework-agnostic core of the R3TR generator.

Run with ``pytest -q`` from the project root.  The tests use the real
sample data file ``data/export_import_data.csv`` (2325 data rows,
~200 components) so the generation logic is verified against production
input, not toy fixtures.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import pytest

from r3tr.core import (
    CSVFileNotFoundError,
    CSVFormatError,
    NoDataError,
    build_generation_plan,
    conflicting_paths,
    export_filename,
    generate_scripts,
    import_filename,
    load_csv,
    render_export_file,
    render_import_file,
    resolve_exp_file,
)

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_CSV = ROOT / "data" / "export_import_data.csv"


# ---------------------------------------------------------------------------
# load_csv
# ---------------------------------------------------------------------------


def test_load_csv_sample_file_groups_components():
    load = load_csv(SAMPLE_CSV, ";")
    assert load.total_rows > 2000, "sample CSV should contain thousands of rows"
    assert len(load.grouped) > 100, "sample CSV should contain many components"
    # every row that made it into the grouping must be intact
    assert all(row["Component"] and row["Table"] for rows in load.grouped.values() for row in rows)
    # grouping must be lossless
    assert sum(len(v) for v in load.grouped.values()) + len(load.skipped_rows) == load.total_rows


def test_load_csv_missing_file_raises_detailed_error(tmp_path):
    with pytest.raises(CSVFileNotFoundError) as exc:
        load_csv(tmp_path / "nope.csv")
    assert "nope.csv" in str(exc.value)


def test_load_csv_wrong_columns_raises_format_error(tmp_path, capsys):
    path = tmp_path / "wrong.csv"
    path.write_text("Foo;Bar\n1;2\n", encoding="utf-8")
    with pytest.raises(CSVFormatError) as exc:
        load_csv(path, ";")
    assert "Component" in str(exc.value)
    assert "Table" in str(exc.value)


def test_load_csv_empty_file_raises(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(CSVFormatError):
        load_csv(path, ";")


def test_load_csv_comma_delimiter(tmp_path):
    path = tmp_path / "comma.csv"
    path.write_text(
        "Component,Table,Table description,Export,Import\n"
        "ABAP_DAEMONS,ABAP_DAEMON_ST,Startup configuration,1,0\n",
        encoding="utf-8",
    )
    load = load_csv(path, ",")
    assert "ABAP_DAEMONS" in load.grouped
    assert load.grouped["ABAP_DAEMONS"][0]["Table"] == "ABAP_DAEMON_ST"


def test_load_csv_skips_rows_without_component_or_table(tmp_path):
    path = tmp_path / "partial.csv"
    path.write_text(
        "Component;Table;Table description;Export;Import\n"
        ";GHOST_TABLE;missing component;1;0\n"
        "REALCOMP;HAS_TABLE;ok;1;1\n",
        encoding="utf-8",
    )
    load = load_csv(path, ";")
    assert list(load.grouped) == ["REALCOMP"]
    assert len(load.skipped_rows) == 1
    assert "skipped" in load.warnings_text()


# ---------------------------------------------------------------------------
# rendering - format must stay byte-for-byte compatible with the original
# ---------------------------------------------------------------------------


def test_render_export_file_exact_format():
    tables = [
        {"Table": "ABAP_DAEMON_ST", "Table description": "ABAP Daemon framework: Startup configuration"},
        {"Table": "ABAP_DAEMON_RT", "Table description": "ABAP Daemon framework: Runtime entries"},
        {"Table": "AMC_RECEIVER2", "Table description": "Table of Registered AMC Receivers"},
    ]
    expected = (
        "export\n"
        "\n"
        "# ABAP_DAEMONS\n"
        "\n"
        "client = ALL\n"
        "file = 'Export_ABAP_DAEMONS_ALL.exp'\n"
        "buffersync=yes\n"
        "\n"
        "# ABAP Daemon framework: Startup configuration\n"
        "delete from ABAP_DAEMON_ST\n"
        "select * from ABAP_DAEMON_ST\n"
        "\n"
        "# ABAP Daemon framework: Runtime entries\n"
        "delete from ABAP_DAEMON_RT\n"
        "select * from ABAP_DAEMON_RT\n"
        "\n"
        "# Table of Registered AMC Receivers\n"
        "delete from AMC_RECEIVER2\n"
        "select * from AMC_RECEIVER2\n"
        "\n"
    )
    assert render_export_file("ABAP_DAEMONS", "ALL", tables) == expected


def test_render_import_file_exact_format():
    expected = (
        "import\n"
        "\n"
        "# ALECONFIG\n"
        "\n"
        "file = 'Export_ALECONFIG_ALL.exp'\n"
        "\n"
        "client=ALL\n"
    )
    assert render_import_file("ALECONFIG", "ALL") == expected


def test_render_export_file_missing_description_falls_back():
    text = render_export_file("X", "000", [{"Table": "FOO", "Table description": ""}])
    assert "# No description" in text


# ---------------------------------------------------------------------------
# generation against the real sample data
# ---------------------------------------------------------------------------


def _sample_grouped():
    return load_csv(SAMPLE_CSV, ";").grouped


def test_generation_plan_flags():
    plan = {p["component"]: p for p in build_generation_plan(_sample_grouped())}
    # ABAP_DAEMONS: export only (Import=0 on all rows)
    assert plan["ABAP_DAEMONS"]["export_enabled"] is True
    assert plan["ABAP_DAEMONS"]["import_enabled"] is False
    # ALECONFIG: both enabled
    assert plan["ALECONFIG"]["export_enabled"] is True
    assert plan["ALECONFIG"]["import_enabled"] is True
    # SWU3: all rows 0/0 -> nothing must be generated
    assert plan["SWU3"]["export_enabled"] is False
    assert plan["SWU3"]["import_enabled"] is False


def test_generate_files_for_sample_data(tmp_path):
    result = generate_scripts(_sample_grouped(), tmp_path, client="ALL", force_overwrite=True)
    assert result.ok
    names = {p.name for p in result.generated}

    expected_export = "export_ABAP_DAEMONS_ALL.r3tr"
    expected_import = "import_ALECONFIG_ALL.r3tr"
    assert expected_export in names
    assert expected_import in names
    # SWU3 has all flags 0 -> no files at all (exact component, not the
    # SWU3_CUST / SWU3_DATA components that share the name prefix)
    assert "export_SWU3_ALL.r3tr" not in names
    assert "import_SWU3_ALL.r3tr" not in names
    # export file content verified against the known good layout
    content = (tmp_path / expected_export).read_text(encoding="utf-8")
    assert content.startswith("export\n\n# ABAP_DAEMONS\n\nclient = ALL\n")
    assert "select * from ABAP_DAEMON_ST" in content
    import_content = (tmp_path / expected_import).read_text(encoding="utf-8")
    assert import_content.startswith("import\n\n# ALECONFIG\n\n")
    assert "file = 'Export_ALECONFIG_ALL.exp'" in import_content
    assert import_content.rstrip("\n").endswith("client=ALL")


def test_second_run_without_force_refuses_overwrite(tmp_path):
    generate_scripts(_sample_grouped(), tmp_path, client="ALL", force_overwrite=True)
    again = generate_scripts(_sample_grouped(), tmp_path, client="ALL", force_overwrite=False)
    assert again.generated == []
    assert len(again.refused) > 0, "existing files must be reported as refused"


def test_second_run_with_force_overwrites(tmp_path):
    generate_scripts(_sample_grouped(), tmp_path, client="ALL", force_overwrite=True)
    again = generate_scripts(_sample_grouped(), tmp_path, client="ALL", force_overwrite=True)
    assert len(again.generated) > 0
    assert again.refused == []


def test_confirm_callback_allows_single_file(tmp_path):
    generate_scripts(_sample_grouped(), tmp_path, client="ALL", force_overwrite=True)
    seen: list[Path] = []

    def confirm(path):
        seen.append(path)
        return True  # user said "yes" for every file

    again = generate_scripts(
        _sample_grouped(), tmp_path, client="ALL", force_overwrite=False, confirm_overwrite=confirm
    )
    assert len(seen) == len(again.generated)
    assert again.generated, "all files must be regenerated after confirmation"


def test_client_is_written_into_files(tmp_path):
    result = generate_scripts(_sample_grouped(), tmp_path, client="100", force_overwrite=True)
    names = {p.name for p in result.generated}
    assert "export_ABAP_DAEMONS_100.r3tr" in names
    content = (tmp_path / "export_ABAP_DAEMONS_100.r3tr").read_text(encoding="utf-8")
    assert "client = 100" in content
    assert "Export_ABAP_DAEMONS_100.exp" in content


def test_conflicting_paths_reports_existing(tmp_path):
    assert conflicting_paths(_sample_grouped(), tmp_path, "ALL") == []
    generate_scripts(_sample_grouped(), tmp_path, client="ALL", force_overwrite=True)
    found = conflicting_paths(_sample_grouped(), tmp_path, "ALL")
    assert len(found) > 0
    assert all(p.parent == tmp_path for p in found)


def test_generate_empty_groups_raises(tmp_path):
    with pytest.raises(NoDataError):
        generate_scripts({}, tmp_path, client="ALL")


def test_output_dir_created_with_parents(tmp_path):
    target = tmp_path / "nested" / "out"
    generate_scripts(
        {
            "ABAP_DAEMONS": [
                {"Component": "ABAP_DAEMONS", "Table": "ABAP_DAEMON_ST",
                 "Table description": "x", "Export": "1", "Import": "0"},
            ]
        },
        target,
        client="ALL",
        force_overwrite=True,
    )
    assert (target / "export_ABAP_DAEMONS_ALL.r3tr").is_file()


# ---------------------------------------------------------------------------
# New features: full-path exp file, component selection, edit-before-save
# ---------------------------------------------------------------------------


TEST_TABLES = [
    {"Component": "T", "Table": "ABAP_DAEMON_ST", "Table description": "d1",
     "Export": "1", "Import": "1"},
    {"Component": "T", "Table": "AMC_RECEIVER2", "Table description": "d2",
     "Export": "1", "Import": "1"},
]


def test_resolve_exp_file_empty_base_gives_bare_name():
    assert resolve_exp_file("", "ABAP_DAEMONS", "ALL") == "Export_ABAP_DAEMONS_ALL.exp"
    assert resolve_exp_file(None, "ABAP_DAEMONS", "ALL") == "Export_ABAP_DAEMONS_ALL.exp"
    assert resolve_exp_file("   ", "ABAP_DAEMONS", "ALL") == "Export_ABAP_DAEMONS_ALL.exp"


def test_resolve_exp_file_posix_base():
    assert (
        resolve_exp_file("/usr/sap/trans/data/SYS/", "T", "100")
        == "/usr/sap/trans/data/SYS/Export_T_100.exp"
    )
    # no trailing slash is normalised too
    assert (
        resolve_exp_file("/usr/sap/trans/data/SYS", "T", "100")
        == "/usr/sap/trans/data/SYS/Export_T_100.exp"
    )
    # backslashes inside a posix base are converted to "/"
    assert (
        resolve_exp_file("D:\\export", "T", "100")  # contains backslash + slash-mix
        .replace("\\", "/")
        .startswith("D:/export/")
    )


def test_resolve_exp_file_windows_base():
    assert (
        resolve_exp_file("D:\\trans\\data\\SYS\\", "T", "100")
        == "D:\\trans\\data\\SYS\\Export_T_100.exp"
    )
    assert (
        resolve_exp_file("D:/trans/data/SYS", "T", "100")  # forward slashes -> posix
        == "D:/trans/data/SYS/Export_T_100.exp"
    )


def test_render_export_with_exp_file_base():
    text = render_export_file("T", "100", TEST_TABLES, exp_file_base="/base/dir/")
    assert "file = '/base/dir/Export_T_100.exp'\n" in text
    # the rest of the layout is unchanged
    assert text.startswith("export\n\n# T\n\nclient = 100\n")
    assert "delete from ABAP_DAEMON_ST" in text and "select * from ABAP_DAEMON_ST" in text


def test_render_import_with_exp_file_base():
    text = render_import_file("T", "100", exp_file_base="D:\\x\\")
    assert "file = 'D:\\x\\Export_T_100.exp'\n" in text


def test_build_generation_plan_respects_component_filter():
    grouped = _sample_grouped()
    names = list(grouped)
    only = {names[0]}
    plan = build_generation_plan(grouped, "ALL", components=only)
    assert {item["component"] for item in plan} == only
    # without a filter we still get everything
    assert len(build_generation_plan(grouped, "ALL")) == len(names)


def test_generate_scripts_component_filter_writes_only_selected(tmp_path):
    grouped = _sample_grouped()
    names = list(grouped)
    keep = {names[0]}
    drop = names[1]
    res = generate_scripts(grouped, tmp_path, client="ALL",
                           force_overwrite=True, components=keep)
    # only the selected component's files exist
    assert (tmp_path / export_filename(names[0], "ALL")).exists()
    assert not (tmp_path / export_filename(drop, "ALL")).exists()
    assert res.components == 1
    # unknown selection -> NoDataError
    with pytest.raises(NoDataError):
        generate_scripts(grouped, tmp_path, client="ALL",
                         force_overwrite=True, components={"NO_SUCH_COMPONENT"})


def test_generate_scripts_overrides_replace_content(tmp_path):
    grouped = _sample_grouped()
    comp = next(c for c, rows in grouped.items()
                if any((r.get("Export", "0") or "0").strip() == "1" for r in rows))
    ename = export_filename(comp, "ALL")
    custom = "MY EDITED CONTENT\n"
    res = generate_scripts(
        grouped, tmp_path, client="ALL", force_overwrite=True,
        components={comp}, overrides={ename: custom},
    )
    written = (tmp_path / ename).read_text(encoding="utf-8")
    assert written == custom
    assert ename in [p.name for p in res.generated]


def test_overrides_and_exp_base_do_not_collide(tmp_path):
    grouped = _sample_grouped()
    comp = next(c for c, rows in grouped.items()
                if any((r.get("Import", "0") or "0").strip() == "1" for r in rows))
    iname = import_filename(comp, "ALL")
    res = generate_scripts(
        grouped, tmp_path, client="ALL", force_overwrite=True,
        components={comp}, exp_file_base="/opt/exp/",
    )
    content = (tmp_path / iname).read_text(encoding="utf-8")
    assert "file = '/opt/exp/Export_%s_ALL.exp'" % comp in content
    assert iname in [p.name for p in res.generated]


# ---------------------------------------------------------------------------
# v1.2: TADIR exclusion + explicit per-component export/import sets
# ---------------------------------------------------------------------------

TADIR_TABLES = [
    {"Component": "SMW0", "Table": "SM_WORKELEMENT", "Table description": "work elements",
     "Export": "1", "Import": "1"},
    {"Component": "SMW0", "Table": "TADIR", "Table description": "table directory",
     "Export": "1", "Import": "1"},
]


def test_render_export_excludes_tadir():
    text_with = render_export_file("SMW0", "ALL", TADIR_TABLES)
    text_without = render_export_file("SMW0", "ALL", TADIR_TABLES, exclude_tables=("TADIR",))
    assert "delete from TADIR" in text_with
    assert "delete from TADIR" not in text_without
    # the other table stays
    assert "delete from SM_WORKELEMENT" in text_without
    # case-insensitive matching
    text_lower = render_export_file("SMW0", "ALL", TADIR_TABLES, exclude_tables=("tadir",))
    assert "delete from TADIR" not in text_lower


def test_render_export_exclude_unknown_table_is_noop():
    text = render_export_file("SMW0", "ALL", TADIR_TABLES, exclude_tables=("NOPE",))
    assert "delete from TADIR" in text and "delete from SM_WORKELEMENT" in text


def test_generate_scripts_exclude_tables_drops_rows(tmp_path):
    res = generate_scripts(
        {"SMW0": TADIR_TABLES}, tmp_path, client="ALL",
        force_overwrite=True, exclude_tables=("TADIR",),
    )
    content = (tmp_path / "export_SMW0_ALL.r3tr").read_text(encoding="utf-8")
    assert "TADIR" not in content
    assert "delete from SM_WORKELEMENT" in content
    assert res.generated


def test_explicit_export_import_components_override_csv_flags(tmp_path):
    # CSV says SMW0: export+import both 1.
    # 1) ask for export only -> import file must NOT be written
    res = generate_scripts(
        {"SMW0": TADIR_TABLES}, tmp_path / "a", client="ALL",
        force_overwrite=True,
        components={"SMW0"},
        export_components={"SMW0"},
        import_components=set(),
    )
    names = {p.name for p in res.generated}
    assert "export_SMW0_ALL.r3tr" in names
    assert "import_SMW0_ALL.r3tr" not in names

    # 2) ask for import only -> export file must NOT be written
    res2 = generate_scripts(
        {"SMW0": TADIR_TABLES}, tmp_path / "b", client="ALL",
        force_overwrite=True,
        components={"SMW0"},
        export_components=set(),
        import_components={"SMW0"},
    )
    names2 = {p.name for p in res2.generated}
    assert "import_SMW0_ALL.r3tr" in names2
    assert "export_SMW0_ALL.r3tr" not in names2

    # 3) neither -> nothing generated, no error (empty selection handled by UI)
    res3 = generate_scripts(
        {"SMW0": TADIR_TABLES}, tmp_path / "c", client="ALL",
        force_overwrite=True,
        components=set(),
        export_components=set(),
        import_components=set(),
    )
    assert res3.generated == []


def test_plan_with_explicit_sets():
    plan = {
        p["component"]: p
        for p in build_generation_plan(
            {"SMW0": TADIR_TABLES, "OTHER": TADIR_TABLES}, "ALL",
            export_components={"SMW0"}, import_components={"OTHER"},
        )
    }
    assert plan["SMW0"]["export_enabled"] is True
    assert plan["SMW0"]["import_enabled"] is False
    assert plan["OTHER"]["export_enabled"] is False
    assert plan["OTHER"]["import_enabled"] is True


def test_component_from_filename():
    from r3tr.core import component_from_filename

    assert component_from_filename("export_ABAP_DAEMONS_ALL.r3tr", "ALL") == "ABAP_DAEMONS"
    assert component_from_filename("import_MY_COMP_100.r3tr", "100") == "MY_COMP"
    assert component_from_filename("import_A_B_C.r3tr", "ALL") == "A_B_C"  # no client suffix
    assert component_from_filename("random.txt", "ALL") is None
