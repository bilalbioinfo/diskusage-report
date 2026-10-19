# diskusage-report

Scans a directory and writes an HTML report of who is using the storage, based
on the owner of each file.

The report shows usage by owner, the top-level folders split by owner, the
largest folders with their main owner, and any paths that couldn't be read.

## Usage

Requires Python 3.6 or newer. Nothing to install.

```bash
python3 disk_report.py /path/to/project
```

This writes `diskusage_<name>_<date>.html` (the report) and
`diskusage_<name>_<date>.json` (the scan data) to the current directory.

To rebuild the report from a saved scan without scanning again:

```bash
python3 disk_report.py --from-json diskusage_<name>_<date>.json
```

## Options

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output` | `./diskusage_<name>_<date>.html` | Where to write the report |
| `-j`, `--workers` | CPU count, max 8 | Number of parallel scanning processes |
| `--depth` | 3 | How many folder levels deep the folder table goes |
| `--cross-filesystems` | off | Also scan other filesystems mounted inside the directory |
| `--from-json FILE` | | Rebuild the report from a saved scan |
| `--days` | 7 | Also report data modified in the last this many days |

## Recently modified data

The report shows how much data was modified in the last `--days` days and which
folders and owners it is in. The top ten folders are also printed in the
terminal.

