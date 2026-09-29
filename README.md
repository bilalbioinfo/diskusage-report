# diskusage-report

Scan a directory tree and get a single HTML report of who is using the storage,
based on the **owner of every file** (not on folder names).

```bash
python3 disk_report.py /path/to/project
```

That writes two files to the current directory:

| File | What it is |
|---|---|
| `diskusage_<name>_<date>.html` | The report. Self-contained, open it in any browser. |
| `diskusage_<name>_<date>.json` | The aggregated scan. Rebuild the report from it without rescanning. |

Requirements: Python 3.6 or newer. Standard library only, nothing to install.

## What the report shows

- **Totals**: space used on disk, number of files and folders, number of owners, unreadable paths.
- **Usage by owner**: every owner on one chart, largest first.
- **Top-level folders by owner**: the 15 biggest folders directly under the scanned path. Each bar is split by owner.
- **Largest folders**: the 30 biggest folders at the deepest measured level, with each one's main owner. The table also includes each owner's main location, which is the deepest folder holding at least half of their data.
- **Unreadable paths**: how many paths couldn't be read, and where.

Every chart has a table view, and hovering a bar shows the breakdown.

## Options

```
python3 disk_report.py PATH [-o report.html] [-j WORKERS] [--depth N] [--cross-filesystems]
python3 disk_report.py --from-json scan.json [-o report.html]
```

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--output` | `./diskusage_<name>_<date>.html` | Where to write the HTML. The JSON goes next to it. |
| `-j`, `--workers` | CPU count, max 8 | Number of processes scanning in parallel. |
| `--depth` | 3 | How many folder levels to measure for the folder tables. |
| `--cross-filesystems` | off | Also scan other filesystems mounted inside the tree. |
| `--from-json FILE` | | Rebuild the HTML from a saved scan. |

## How long it takes

On a parallel filesystem (Lustre, GPFS), scanning speed depends mostly on how
many file lookups happen at once, so the script runs several worker processes.
As a reference, 8 workers on a Dardel login node scanned about 5,500 files per
second, so 25 million files take roughly 75 minutes. More workers on a compute
node usually go faster, until the filesystem's metadata server becomes the limit.

Progress is printed to stderr every minute:

```
[0:02:01] 647,575 entries, 50,454 dirs, 13.6 TiB, 5314 entries/s, 157 tasks queued
```

### Running it as a Slurm job

Long scans are better run as a batch job than on a login node. Adjust the
account and partition to your cluster:

```bash
#!/bin/bash
#SBATCH -A <your-project-account>
#SBATCH -p shared
#SBATCH -c 16
#SBATCH -t 06:00:00
#SBATCH -J diskusage

python3 disk_report.py /path/to/project -j "$SLURM_CPUS_PER_TASK" -o /path/to/reports/diskusage.html
```

## How sizes and owners are measured

- **Owner** is the file's uid, turned into a username (and full name, if set)
  with the host's user database. Run the report on a node that knows the
  cluster's users, or owners show up as `uid 1234`.
- **Size** is space used on disk (`st_blocks × 512`), the same number `du` and
  ncdu report. Hard-linked files are counted once, and symlinks are not followed.
- Other filesystems mounted inside the tree are skipped unless you pass
  `--cross-filesystems`, like `du -x`.
- Paths the scanning user can't read are counted and listed, and the report
  says where they are. Their contents are missing from the totals. For
  complete numbers, run it as a user who can read everything.
