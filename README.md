# R3TR generator (NiceGUI)

Generates SAP **R3trans** export/import parameter files from a CSV that
lists SAP components, their tables and the `Export` / `Import` flags —
rebuilt as a proper Python project with a **NiceGUI web interface** plus
the classic CLI.

> Generator for `.r3tr` files (R3trans export/import) based on a CSV file
> (components, tables, export/import flags). Includes a web interface (NiceGUI)
> as well as a classic CLI. To run: execute `python run.py` and open
> http://localhost:8080.

## Features

- 🌐 **Web UI (NiceGUI)** – the main interface, organised as a guided
  flow (`1 · Data source` → `2 · Generation settings` →
  `3 · Advanced options` → `4 · Generate R3TR scripts`):
  - upload a CSV (drag & drop) or point to an existing server file,
  - **per component**: tick / untick **Export** and/or **Import**
    (defaults are loaded from the CSV flags), with *Select all /
    Deselect all*, per-type *Select / Deselect Export* and
    *Select / Deselect Import*, plus **Default from CSV** (reload the
    toggles from the CSV flags),
  - **tables + descriptions** of any component (flag columns included),
  - settings: delimiter, client (`000` / `100` / `ALL` …), output
    directory, force-overwrite,
  - **advanced option**: exclude the **TADIR** table from the exports of
    the components that contain it — with a warning that TADIR is part
    of those export files,
  - every right-hand section (**Components, Tables of a component,
    Generated files, File content preview, Edit before save, Run log**)
    is collapsible; on first run only **Components** is open —
    **Generated files** appears (opens) after the first run and the
    hidden-by-default **Run log** expands automatically with the run,
  - **File content preview**: shows the **export and import file of the
    selected component** (both scrollable panes, same layout as the
    edit-before-save card),
  - overwrite confirmation dialog when target files already exist
    (*Overwrite all / Skip existing / Cancel*),
  - **edit any generated file** (editor dialog: *Save to disk* /
    *Discard*),
  - optional **full path for the `file = '...'` line** —
    *EXP file base path* (e.g. `/usr/sap/trans/data/SYS/`) is prepended
    to the `.exp` name inside the generated files,
  - **dark mode** with a header switch (persisted per browser),
  - consistent typography across the page (16 px headings/controls,
    14 px secondary text, 14 px code).
- ⌨️ **CLI mode** – original flags (`-f -o -c -d --force -C -e`) plus:
  `--export-components "A,B"`, `--import-components "A,B"` (explicit
  per-file-type selection, overriding the CSV flags) and
  `--exclude-tables TADIR` (advanced exclusion),
- 🔒 **Core library** (`r3tr/core.py`) – UI-agnostic, raising
  well-described exceptions instead of printing to stderr, returning a
  structured `GenerationResult`,
- 🧪 **Tests** – 33 unit tests covering CSV loading, byte-exact file
  formatting, flag handling, overwrite policies, client handling, the
  component filter, explicit export/import sets, TADIR exclusion, the
  `overrides` (edit-before-save) path and the `file = '...'` full-path
  handling (run against the real 2325-row sample data).

## Project layout

```
r3tr-generator/
├── run.py                     # entry point (web UI / --cli)
├── r3tr/
│   ├── core.py                # generation logic (shared by UI + CLI + tests)
│   ├── cli.py                 # classic CLI
│   └── app.py                 # NiceGUI web interface + download endpoints
├── tests/test_core.py         # pytest suite
├── data/export_import_data.csv   # sample data (2325 rows)
├── requirements.txt
├── Dockerfile                 # optional container deployment
└── generated_scripts/         # default output directory (created at runtime)
```

## Setup

```bash
cd r3tr-generator
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest        # only for running the test suite
```

## Usage – Web UI (recommended)

```bash
python run.py              # -> http://localhost:8080
python run.py --port 9000 --host 0.0.0.0
```

1. **Data source** – upload the CSV (or use the bundled
   `data/export_import_data.csv` – it is preselected),
2. **Parse data** – the *Components* card lists every component with its
   table count and **Export / Import toggles** (defaults from the CSV);
   tick what you need, *Select all* / *Deselect all* when in a hurry,
3. **Tables of a component** – pick any component to see its tables and
   descriptions (the TADIR row is marked `⚠ EXCLUDED` when the advanced
   option is on),
4. **Generation settings** – delimiter (default `;`), client (default
   `ALL`), output directory (default `./generated_scripts`),
   *EXP file base path* (optional full path for `file = '...'`), and the
   *force overwrite* checkbox,
5. **Advanced options** – *Exclude the TADIR table from exports*; the
   inline note lists the affected components and warns that TADIR is
   part of the export files,
6. **Generate R3TR scripts** – existing target files trigger the
   *Overwrite all / Skip existing / Cancel* dialog, the run goes to the
   (hidden by default) **Run log**,
7. **Generated files** – preview / edit / download any file, or grab the
   whole batch as `r3tr_<client>.zip`.

## Usage – CLI

```bash
# defaults: sample CSV from the project, output to ./generated_scripts
python run.py --cli
# or
python -m r3tr.cli

# custom CSV / output dir / client, force overwrite
python run.py --cli -f my_data.csv -o /tmp/sap_output -c 100 --force

# comma separator
python run.py --cli -d ","

# only two components, full path for the .exp file inside the generated
# files (file = '/usr/sap/trans/data/SYS/Export_ABAP_DAEMONS_100.exp')
python run.py --cli -C "ABAP_DAEMONS,ALECONFIG" \
  -e "/usr/sap/trans/data/SYS/" -o /tmp/sap_output -c 100 --force

# explicit per-file-type selection (overrides the CSV flags)
python run.py --cli \
  --export-components "ABAP_DAEMONS,ALECONFIG" \
  --import-components "ALECONFIG"

# advanced: cut TADIR out of the exported table lists
python run.py --cli --exclude-tables TADIR --force
```

| Flag | Meaning | Default |
|---|---|---|
| `-f, --csv-file` | input CSV file | `data/export_import_data.csv` |
| `-o, --output-dir` | where to write `*.r3tr` files | `generated_scripts` |
| `-c, --client` | client identifier written into the files | `ALL` |
| `-d, --delimiter` | CSV delimiter | `;` |
| `--force` | overwrite existing files without asking | off (ask per file) |
| `-C, --components` | comma-separated list of components to include | all enabled |
| `-e, --exp-file-base` | base path prepended to the `file = '...'` name | empty (file name only) |
| `--export-components` | components that receive an **export** file (overrides CSV flags) | CSV `Export` flags |
| `--import-components` | components that receive an **import** file (overrides CSV flags) | CSV `Import` flags |
| `--exclude-tables` | table names (case-insensitive) omitted from exported content, e.g. `TADIR` | none |

## Generated file format

`export_<COMPONENT>_<CLIENT>.r3tr`:

```
export

# ABAP_DAEMONS

client = ALL
file = 'Export_ABAP_DAEMONS_ALL.exp'
buffersync=yes

# ABAP Daemon framework: Startup configuration
delete from ABAP_DAEMON_ST
select * from ABAP_DAEMON_ST
```

`import_<COMPONENT>_<CLIENT>.r3tr`:

```
import

# ALECONFIG

file = 'Export_ALECONFIG_ALL.exp'

client=ALL
```

Rules:

- a component gets an **export** file when its Export toggle is on (CSV
  `Export` flag by default, or an explicit `--export-components` entry)
  and an **import** file when its Import toggle is on,
- components with both toggles off (e.g. `SWU3` in the sample data)
  produce nothing,
- rows missing `Component` or `Table` are skipped and reported,
- existing target files are only overwritten after confirmation
  (`--force` / "Overwrite all"),
- with `--exclude-tables` enabled (or the advanced UI option), the listed
  tables — e.g. `TADIR` — are omitted from the export content.

## Tests

```bash
pytest -q
```

## Deployment

### Local / LAN

Run on any machine with Python ≥ 3.10; to share the UI on your LAN use
`python run.py --host 0.0.0.0` and open `http://<host>:8080`.

### Docker

```bash
docker build -t r3tr-gen .
docker run --rm -p 8080:8080 \
  -v $(pwd)/generated_scripts:/app/generated_scripts \
  r3tr-gen
```

### Scheduling / batch

The CLI can be called unattended (`--force`) from job schedulers; the web
UI is meant for interactive use.

## Background (what the data means)

The CSV describes which SAP Basis component tables must be dumped/reloaded
via R3trans. Component ↔ table mapping in a running system lives in DDIC
dictionary tables – see `docs/TADIR_info.txt` (`TADIR`, `TDDAT`,
`TABUXX`) for the metadata source.
