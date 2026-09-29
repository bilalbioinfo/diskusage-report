#!/usr/bin/env python3
"""
Author: Bilal Sharif <bilal.bioinfo@gmail.com>

Description: scan a directory tree and write an HTML report of who owns the storage.
Usage: python3 disk_report.py /path/to/project

Writes two files into the current directory
diskusage_<name>_<date>.html: the report
diskusage_<name>_<date>.json: the aggregated scan, so the report can be rebuilt without rescanning: python3 disk_report.py --from-json <file>.json
"""
import argparse
import datetime
import json
import os
import pwd
import socket
import stat
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

# A worker handles this many entries before handing its unvisited
# subdirectories back to the queue, so one huge subtree gets spread over
# all workers instead of pinning one of them.
ENTRIES_PER_TASK = 20000
PROGRESS_EVERY_S = 60
MAX_ERRORS_KEPT = 500


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------

def scan_task(root, rel, root_dev, depth, cross_fs):
    """Scan the directory `rel` (relative to `root`) and as much below it as
    fits in ENTRIES_PER_TASK. Runs in a worker process.

    Returns (usage, hardlinks, errors, leftover_dirs, n_entries, n_dirs):
      usage     {(folder_key, uid): [bytes, files]} where folder_key is the
                containing directory truncated to `depth` levels
      hardlinks [(dev, ino, folder_key, uid, bytes)] for files with nlink > 1,
                de-duplicated by the parent process
      errors    [(rel_path, message)]
    """
    usage = {}
    hardlinks = []
    errors = []
    todo = [rel]
    n_entries = 0
    n_dirs = 0

    while todo:
        cur = todo.pop()
        path = os.path.join(root, cur) if cur else root
        key = "/".join(cur.split("/")[:depth]) if cur else ""
        try:
            it = os.scandir(path)
        except OSError as e:
            errors.append((cur, e.strerror or str(e)))
            continue
        n_dirs += 1
        try:
            with it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        errors.append((cur + "/" + entry.name if cur else entry.name, e.strerror or str(e)))
                        continue
                    is_dir = stat.S_ISDIR(st.st_mode)
                    if is_dir:
                        if not cross_fs and st.st_dev != root_dev:
                            continue
                        todo.append(cur + "/" + entry.name if cur else entry.name)
                    n_entries += 1
                    size = st.st_blocks * 512
                    if not is_dir and st.st_nlink > 1:
                        hardlinks.append((st.st_dev, st.st_ino, key, st.st_uid, size))
                        continue
                    slot = usage.get((key, st.st_uid))
                    if slot is None:
                        usage[(key, st.st_uid)] = [size, 0 if is_dir else 1]
                    else:
                        slot[0] += size
                        if not is_dir:
                            slot[1] += 1
        except OSError as e:
            errors.append((cur, e.strerror or str(e)))
        if n_entries >= ENTRIES_PER_TASK and todo:
            break

    return usage, hardlinks, errors, todo, n_entries, n_dirs


def fmt_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            return ("%.0f %s" if unit == "B" else "%.1f %s") % (n, unit)
        n /= 1024.0


def fmt_duration(s):
    s = int(s)
    return "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


def scan(root, workers, depth, cross_fs):
    root_st = os.stat(root)
    root_dev = root_st.st_dev
    usage = {("", root_st.st_uid): [root_st.st_blocks * 512, 0]}  # (folder_key, uid) -> [bytes, files]
    seen_inodes = set()
    errors = []
    n_errors = 0
    error_top = {}       # top-level folder -> number of errors below it
    n_entries = 0
    n_dirs = 0
    total_bytes = root_st.st_blocks * 512
    t0 = last = time.time()

    with ProcessPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(scan_task, root, "", root_dev, depth, cross_fs)}
        while pending:
            done, pending = wait(pending, timeout=PROGRESS_EVERY_S, return_when=FIRST_COMPLETED)
            for fut in done:
                u, hl, errs, leftover, ne, nd = fut.result()
                for k, (b, f) in u.items():
                    total_bytes += b
                    slot = usage.get(k)
                    if slot is None:
                        usage[k] = [b, f]
                    else:
                        slot[0] += b
                        slot[1] += f
                for dev, ino, key, uid, b in hl:
                    if (dev, ino) in seen_inodes:
                        continue
                    seen_inodes.add((dev, ino))
                    total_bytes += b
                    slot = usage.get((key, uid))
                    if slot is None:
                        usage[(key, uid)] = [b, 1]
                    else:
                        slot[0] += b
                        slot[1] += 1
                for p, msg in errs:
                    n_errors += 1
                    top = p.split("/")[0] if p else "."
                    error_top[top] = error_top.get(top, 0) + 1
                    if len(errors) < MAX_ERRORS_KEPT:
                        errors.append([p, msg])
                n_entries += ne
                n_dirs += nd
                for d in leftover:
                    pending.add(pool.submit(scan_task, root, d, root_dev, depth, cross_fs))
            now = time.time()
            if now - last >= PROGRESS_EVERY_S:
                last = now
                el = now - t0
                sys.stderr.write("[%s] %s entries, %s dirs, %s, %.0f entries/s, %d tasks queued\n" % (
                    fmt_duration(el), format(n_entries, ","), format(n_dirs, ","),
                    fmt_bytes(total_bytes), n_entries / el if el else 0, len(pending)))
                sys.stderr.flush()

    return {
        "usage": usage,
        "errors": errors,
        "n_errors": n_errors,
        "error_top": error_top,
        "n_entries": n_entries,
        "n_dirs": n_dirs,
        "duration_s": round(time.time() - t0, 1),
    }


def owner_info(uid):
    try:
        pw = pwd.getpwuid(uid)
        return pw.pw_name, pw.pw_gecos.split(",")[0].strip()
    except KeyError:
        return "uid %d" % uid, ""


def to_saved(root, result, workers, depth, cross_fs):
    """Aggregate raw scan results into the JSON that gets saved and that the
    report is built from. Folder sizes are cumulative (a folder includes
    everything below it) for every folder up to `depth` levels deep."""
    owners = {}
    folders = {}
    uid_names = {}
    for (key, uid), (b, f) in result["usage"].items():
        if uid not in uid_names:
            uid_names[uid] = owner_info(uid)
        name, full = uid_names[uid]
        o = owners.setdefault(name, {"uid": uid, "full_name": full, "bytes": 0, "files": 0})
        o["bytes"] += b
        o["files"] += f
        parts = key.split("/") if key else []
        for i in range(len(parts) + 1):
            p = "/".join(parts[:i])
            d = folders.setdefault(p, {})
            d[name] = d.get(name, 0) + b

    return {
        "meta": {
            "root": root,
            "host": socket.gethostname(),
            "scanned_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "duration_s": result["duration_s"],
            "workers": workers,
            "depth": depth,
            "cross_filesystems": cross_fs,
            "entries": result["n_entries"],
            "dirs": result["n_dirs"],
            "n_errors": result["n_errors"],
        },
        "owners": owners,
        "folders": folders,
        "error_top": result["error_top"],
        "errors": result["errors"],
    }


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

TOP_FOLDERS = 15        # bars in the top-level folder chart, rest rolled up
COLORED_OWNERS = 8      # owners with their own color; the rest are "other"
LARGEST_FOLDERS = 30    # deepest-level folders in the "largest folders" table
HOTSPOT_SHARE = 0.5     # descend while one subfolder holds >= this share


def build_report_data(saved):
    folders = saved["folders"]
    owners = saved["owners"]
    depth = saved["meta"]["depth"]

    children = {}
    for p in folders:
        if p == "":
            continue
        parent = p.rsplit("/", 1)[0] if "/" in p else ""
        children.setdefault(parent, []).append(p)

    def total(p):
        return sum(folders.get(p, {}).values())

    def hotspot(name):
        """Walk down from the root while a single subfolder holds most of
        this owner's data; return the deepest such folder."""
        node = ""
        node_b = folders[""].get(name, 0)
        while True:
            best, best_b = None, 0
            for c in children.get(node, []):
                b = folders[c].get(name, 0)
                if b > best_b:
                    best, best_b = c, b
            if best is None or best_b < HOTSPOT_SHARE * node_b:
                return node, node_b
            node, node_b = best, best_b

    owner_list = []
    grand = folders.get("", {})
    for name, o in owners.items():
        tops = sorted(((c, folders[c].get(name, 0)) for c in children.get("", [])), key=lambda x: -x[1])
        tops = [{"path": c, "bytes": b} for c, b in tops if b > 0][:6]
        hp, hb = hotspot(name)
        owner_list.append({
            "name": name, "full_name": o["full_name"], "bytes": grand.get(name, 0),
            "files": o["files"], "top": tops,
            "hotspot": hp, "hotspot_bytes": hb,
        })
    owner_list.sort(key=lambda x: -x["bytes"])

    colored = [o["name"] for o in owner_list[:COLORED_OWNERS]]

    def split(p):
        d = folders.get(p, {})
        segs = [{"owner": n, "bytes": d.get(n, 0)} for n in colored]
        rest = sum(b for n, b in d.items() if n not in colored)
        segs.append({"owner": None, "bytes": rest})
        full = sorted(({"owner": n, "bytes": b} for n, b in d.items() if b > 0), key=lambda x: -x["bytes"])
        return segs, full

    top_level = sorted(children.get("", []), key=lambda p: -total(p))
    folder_chart = []
    for p in top_level[:TOP_FOLDERS]:
        segs, full = split(p)
        folder_chart.append({"name": p, "bytes": total(p), "segs": segs, "owners": full,
                             "errors": saved["error_top"].get(p, 0)})
    tail = top_level[TOP_FOLDERS:]
    if tail:
        agg = {}
        for p in tail:
            for n, b in folders[p].items():
                agg[n] = agg.get(n, 0) + b
        segs = [{"owner": n, "bytes": agg.get(n, 0)} for n in colored]
        segs.append({"owner": None, "bytes": sum(b for n, b in agg.items() if n not in colored)})
        full = sorted(({"owner": n, "bytes": b} for n, b in agg.items() if b > 0), key=lambda x: -x["bytes"])
        folder_chart.append({"name": "%d other folders" % len(tail), "bytes": sum(agg.values()),
                             "segs": segs, "owners": full, "errors": 0, "aggregate": True})

    # Largest folders: the biggest folders at the deepest measured level,
    # plus every owner's main location, so each owner shows up somewhere.
    leaves = [p for p in folders if p and p not in children]
    leaves.sort(key=lambda p: -total(p))
    home_of = {}
    for o in owner_list:
        if o["hotspot"] and o["bytes"] > 0:
            home_of.setdefault(o["hotspot"], []).append(
                {"owner": o["name"], "bytes": o["hotspot_bytes"], "owner_total": o["bytes"]})
    paths = set(leaves[:LARGEST_FOLDERS]) | set(home_of)
    largest = []
    for p in sorted(paths, key=lambda p: -total(p)):
        d = folders[p]
        main = max(d.items(), key=lambda kv: kv[1])
        largest.append({"path": p, "bytes": total(p), "main_owner": main[0],
                        "main_bytes": main[1], "n_owners": sum(1 for b in d.values() if b > 0),
                        "home_of": home_of.get(p, [])})
    spread = [o["name"] for o in owner_list if not o["hotspot"] and o["bytes"] > 0]

    root_files = sum(o["files"] for o in owners.values())
    return {
        "meta": dict(saved["meta"], total_bytes=total(""), files=root_files,
                     n_owners=len(owner_list), n_top_folders=len(top_level),
                     error_top=saved["error_top"], errors=saved["errors"][:50],
                     hotspot_share=HOTSPOT_SHARE, depth=depth),
        "colored": colored,
        "owners": owner_list,
        "folders": folder_chart,
        "largest": largest,
        "spread": spread,
    }


def render_html(saved):
    data = build_report_data(saved)
    name = os.path.basename(saved["meta"]["root"].rstrip("/")) or saved["meta"]["root"]
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return (HTML_TEMPLATE
            .replace("__TITLE__", html_escape(name) + " storage by owner")
            .replace("__DATA__", payload))


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --page:           #f9f9f7;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --gridline:       #e1e0d9;
    --border:         rgba(11,11,11,0.10);
    --track:          #eeede8;
    --tooltip-bg:     #201f1d;
    --tooltip-fg:     #ffffff;
    --warn:           #b25e00;
    --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
    --s5: #e87ba4; --s6: #008300; --s7: #4a3aa7; --s8: #e34948;
    --s-other: #b4b2a9;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --page:           #0d0d0d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --gridline:       #2c2c2a;
      --border:         rgba(255,255,255,0.10);
      --track:          #242422;
      --tooltip-bg:     #f2f1ee;
      --tooltip-fg:     #0b0b0b;
      --warn:           #f0a040;
      --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
      --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
      --s-other: #5c5b56;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1:      #1a1a19;
    --page:           #0d0d0d;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted:     #898781;
    --gridline:       #2c2c2a;
    --border:         rgba(255,255,255,0.10);
    --track:          #242422;
    --tooltip-bg:     #f2f1ee;
    --tooltip-fg:     #0b0b0b;
    --warn:           #f0a040;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
    --s-other: #5c5b56;
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    font-size: 14px;
  }
  .wrap { max-width: 1000px; margin: 0 auto; padding: 32px 20px 64px; }
  h1 { font-size: 22px; font-weight: 650; margin: 0 0 4px; text-wrap: balance; }
  .subtitle { color: var(--text-secondary); font-size: 14px; margin: 0 0 28px; line-height: 1.5; }
  code { background: var(--track); border-radius: 4px; padding: 1px 5px; font-size: 12px; overflow-wrap: anywhere; }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 22px 24px 10px;
    margin-bottom: 22px;
  }
  .card h2 { font-size: 15px; font-weight: 650; margin: 0 0 2px; }
  .card .desc { color: var(--text-secondary); font-size: 13px; margin: 0 0 18px; line-height: 1.5; max-width: 72ch; }

  .tiles { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 24px; }
  @media (max-width: 720px) { .tiles { grid-template-columns: repeat(2, 1fr); } }
  .tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
  .tile .label { font-size: 12.5px; color: var(--text-secondary); margin-bottom: 6px; }
  .tile .value { font-size: 26px; font-weight: 650; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }
  .tile .value .unit { font-size: 14px; font-weight: 500; color: var(--text-secondary); margin-left: 3px; }
  .tile .sub { font-size: 12px; color: var(--text-muted); margin-top: 4px; }

  .legend { display: flex; gap: 8px 18px; margin-bottom: 16px; flex-wrap: wrap; }
  .legend-item { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--text-secondary); }
  .legend-swatch { width: 11px; height: 11px; border-radius: 3px; flex: none; }

  .chart { display: flex; flex-direction: column; }
  .bar-row {
    display: grid;
    grid-template-columns: 160px 1fr 96px;
    align-items: center;
    column-gap: 12px;
    height: 30px;
    border-radius: 6px;
    outline: none;
  }
  @media (max-width: 560px) { .bar-row { grid-template-columns: 96px 1fr 76px; } }
  .bar-row:focus-visible { box-shadow: 0 0 0 2px var(--s1); }
  .bar-row:hover .track { filter: brightness(1.05); }
  .bar-label { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }
  .track { position: relative; height: 18px; background: var(--track); border-radius: 4px; overflow: hidden; }
  .fill { height: 100%; display: flex; gap: 2px; }
  .seg { height: 100%; min-width: 0; }
  .fill .seg:last-child { border-radius: 0 4px 4px 0; }
  .bar-value { font-size: 12.5px; color: var(--text-secondary); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .bar-value .err { color: var(--warn); font-weight: 650; }
  .more { font-size: 12.5px; color: var(--text-muted); padding: 8px 0 4px; }

  details.tbl { margin: 10px 0 12px; }
  details.tbl summary { cursor: pointer; font-size: 12.5px; color: var(--text-secondary); padding: 6px 0; user-select: none; }
  details.tbl summary:hover { color: var(--text-primary); }
  .table-wrap { overflow-x: auto; margin-bottom: 14px; }
  table.data { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  table.data th, table.data td {
    text-align: right; padding: 6px 8px; border-bottom: 1px solid var(--gridline);
    font-variant-numeric: tabular-nums; vertical-align: top;
  }
  table.data th.l, table.data td.l { text-align: left; font-variant-numeric: normal; }
  table.data th { color: var(--text-muted); font-weight: 600; white-space: nowrap; }
  table.data td.path { font-family: ui-monospace, "SFMono-Regular", Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; min-width: 16ch; }
  table.data .muted { color: var(--text-muted); }
  .share { display: inline-block; width: 44px; height: 6px; background: var(--track); border-radius: 3px; margin-left: 6px; vertical-align: middle; overflow: hidden; }
  .share i { display: block; height: 100%; background: var(--s1); }

  .notes { font-size: 12.5px; color: var(--text-secondary); line-height: 1.7; }
  .notes { max-width: none; }

  #tooltip {
    position: fixed; pointer-events: none;
    background: var(--tooltip-bg); color: var(--tooltip-fg);
    border-radius: 8px; padding: 9px 12px; font-size: 12px; line-height: 1.6;
    max-width: 300px; box-shadow: 0 6px 20px rgba(0,0,0,0.25);
    opacity: 0; transition: opacity 0.1s ease; z-index: 50;
  }
  #tooltip.show { opacity: 1; }
  #tooltip .tt-title { font-weight: 650; margin-bottom: 4px; overflow-wrap: anywhere; }
  #tooltip .tt-row { display: flex; justify-content: space-between; gap: 14px; }
  #tooltip .tt-row .k { display: flex; align-items: center; gap: 6px; opacity: 0.85; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #tooltip .tt-key { width: 8px; height: 8px; border-radius: 2px; flex: none; }
  #tooltip .tt-more { opacity: 0.65; margin-top: 2px; }
  @media (prefers-reduced-motion: reduce) { #tooltip { transition: none; } }
</style>
</head>
<body>
<div class="wrap">
  <h1 id="title"></h1>
  <p class="subtitle" id="subtitle"></p>

  <div class="tiles">
    <div class="tile"><div class="label">Used on disk</div><div class="value" id="tile-total"></div><div class="sub" id="tile-total-sub"></div></div>
    <div class="tile"><div class="label">Files</div><div class="value" id="tile-files"></div><div class="sub" id="tile-files-sub"></div></div>
    <div class="tile"><div class="label">Owners</div><div class="value" id="tile-owners"></div><div class="sub" id="tile-owners-sub"></div></div>
    <div class="tile"><div class="label">Unreadable paths</div><div class="value" id="tile-errors"></div><div class="sub" id="tile-errors-sub"></div></div>
  </div>

  <div class="card">
    <h2>Usage by owner</h2>
    <p class="desc">Every owner, largest first. Hover or focus a bar to see their largest top-level folders.</p>
    <div class="chart" id="chart-owners"></div>
    <details class="tbl"><summary>Show as table</summary><div class="table-wrap" id="table-owners"></div></details>
  </div>

  <div class="card">
    <h2>Top-level folders by owner</h2>
    <p class="desc" id="desc-folders"></p>
    <div class="legend" id="legend-folders"></div>
    <div class="chart" id="chart-folders"></div>
    <details class="tbl"><summary>Show as table</summary><div class="table-wrap" id="table-folders"></div></details>
  </div>

  <div class="card">
    <h2>Largest folders</h2>
    <p class="desc" id="desc-largest"></p>
    <div class="table-wrap" id="table-largest"></div>
  </div>

  <div class="card notes">
    <h2 style="margin-bottom:6px;">Unreadable paths</h2>
    <p id="note-errors" style="margin:0 0 14px;"></p>
  </div>
</div>
<div id="tooltip" role="status" aria-live="polite"></div>

<script>
const DATA = __DATA__;
const M = DATA.meta;

function fmtBytes(b) {
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  let i = 0;
  while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
  const digits = i === 0 ? 0 : (b >= 100 ? 0 : b >= 10 ? 1 : 2);
  return b.toLocaleString(undefined, { maximumFractionDigits: digits }) + ' ' + units[i];
}
function fmtInt(n) { return n.toLocaleString(); }
function pct(a, b) { return b > 0 ? Math.round(100 * a / b) : 0; }
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
function ownerLabel(o) { return o.full_name ? o.name + ' (' + o.full_name + ')' : o.name; }

const colorOf = {};
DATA.colored.forEach((n, i) => { colorOf[n] = 'var(--s' + (i + 1) + ')'; });
function ownerColor(n) { return n && colorOf[n] ? colorOf[n] : 'var(--s-other)'; }

// ---- tooltip ----
const tooltip = document.getElementById('tooltip');
function showTooltip(anchor, title, rows) {
  tooltip.replaceChildren(el('div', 'tt-title', title));
  rows.slice(0, 8).forEach(r => {
    const row = el('div', 'tt-row');
    const k = el('div', 'k');
    if (r.color) { const key = el('span', 'tt-key'); key.style.background = r.color; k.appendChild(key); }
    k.appendChild(el('span', null, r.k));
    row.appendChild(k);
    row.appendChild(el('div', null, r.v));
    tooltip.appendChild(row);
  });
  if (rows.length > 8) tooltip.appendChild(el('div', 'tt-more', '+ ' + (rows.length - 8) + ' more'));
  tooltip.classList.add('show');
  place(anchor);
}
function place(anchor) {
  const r = anchor.getBoundingClientRect();
  tooltip.style.left = Math.max(8, Math.min(r.left + 160, window.innerWidth - 310)) + 'px';
  tooltip.style.top = Math.max(r.top - tooltip.offsetHeight - 8, 8) + 'px';
}
function hideTooltip() { tooltip.classList.remove('show'); }
function hover(row, fn) {
  row.addEventListener('mouseenter', fn);
  row.addEventListener('focus', fn);
  row.addEventListener('mouseleave', hideTooltip);
  row.addEventListener('blur', hideTooltip);
}

// ---- charts ----
function barRow(label, title, width, segs, valueNode) {
  const row = el('div', 'bar-row');
  row.tabIndex = 0;
  const lab = el('div', 'bar-label', label);
  lab.title = title;
  row.appendChild(lab);
  const track = el('div', 'track');
  const fill = el('div', 'fill');
  fill.style.width = width + '%';
  segs.forEach(s => {
    const seg = el('div', 'seg');
    seg.style.flex = s.bytes + ' 1 0';
    seg.style.background = s.color;
    fill.appendChild(seg);
  });
  track.appendChild(fill);
  row.appendChild(track);
  row.appendChild(valueNode);
  return row;
}

function ownerChart(container, owners, limit) {
  const max = Math.max.apply(null, owners.map(o => o.bytes).concat([1]));
  owners.slice(0, limit).forEach(o => {
    const w = Math.max(100 * o.bytes / max, o.bytes > 0 ? 0.6 : 0);
    const row = barRow(o.name, ownerLabel(o), w, [{ bytes: 1, color: ownerColor(o.name) }],
                       el('div', 'bar-value', fmtBytes(o.bytes)));
    hover(row, () => showTooltip(row, ownerLabel(o), o.top.map(t => ({ k: t.path, v: fmtBytes(t.bytes) }))));
    container.appendChild(row);
  });
  if (owners.length > limit) {
    container.appendChild(el('div', 'more', '+ ' + (owners.length - limit) + ' more owners, all listed in the table below.'));
  }
}

function table(container, cols, rows) {
  const t = el('table', 'data');
  const hr = el('tr');
  cols.forEach(c => { const th = el('th', c.cls, c.h); hr.appendChild(th); });
  const thead = el('thead'); thead.appendChild(hr); t.appendChild(thead);
  const tb = el('tbody');
  rows.forEach(r => {
    const tr = el('tr');
    cols.forEach(c => {
      const td = el('td', c.cls);
      const v = c.v(r);
      if (v instanceof Node) td.appendChild(v); else td.textContent = v;
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
  t.appendChild(tb);
  container.appendChild(t);
}
function shareBar(p) {
  const s = el('span');
  s.appendChild(document.createTextNode(p + '%'));
  const bar = el('span', 'share'); const i = el('i'); i.style.width = p + '%'; bar.appendChild(i);
  s.appendChild(bar);
  return s;
}

// ---- header & tiles ----
const rootName = M.root.replace(/\/+$/, '').split('/').pop() || M.root;
document.getElementById('title').textContent = 'Storage by owner: ' + rootName;
document.getElementById('subtitle').replaceChildren(
  document.createTextNode('Scan of '), el('code', null, M.root),
  document.createTextNode(' on ' + M.host + ', ' + M.scanned_at + '.'));

function tileValue(id, text) {
  const m = text.match(/^([\d.,]+)\s*(.*)$/);
  const node = document.getElementById(id);
  node.textContent = m ? m[1] : text;
  if (m && m[2]) node.appendChild(el('span', 'unit', m[2]));
}
tileValue('tile-total', fmtBytes(M.total_bytes));
document.getElementById('tile-total-sub').textContent = 'in ' + fmtInt(M.n_top_folders) + ' top-level folders';
tileValue('tile-files', fmtInt(M.files));
document.getElementById('tile-files-sub').textContent = fmtInt(M.dirs) + ' folders';
tileValue('tile-owners', fmtInt(M.n_owners));
const top1 = DATA.owners[0];
document.getElementById('tile-owners-sub').textContent = top1 ? 'largest: ' + top1.name + ', ' + pct(top1.bytes, M.total_bytes) + '%' : '';
tileValue('tile-errors', fmtInt(M.n_errors));
document.getElementById('tile-errors-sub').textContent = M.n_errors ? 'totals may be slightly low' : 'everything was readable';

// ---- owners ----
const owners = DATA.owners.filter(o => o.bytes > 0);
ownerChart(document.getElementById('chart-owners'), owners, 40);
table(document.getElementById('table-owners'), [
  { h: 'Owner', cls: 'l', v: o => o.name },
  { h: 'Name', cls: 'l', v: o => o.full_name || '' },
  { h: 'Used', v: o => fmtBytes(o.bytes) },
  { h: 'Share', v: o => pct(o.bytes, M.total_bytes) + '%' },
  { h: 'Files', v: o => fmtInt(o.files) },
], owners);

// ---- top-level folders, stacked by owner ----
document.getElementById('desc-folders').textContent =
  (DATA.folders.some(f => f.aggregate) ? 'The ' + (DATA.folders.length - 1) + ' largest folders directly under the scanned path, plus the rest combined. ' : 'Every folder directly under the scanned path. ') +
  'Each bar is split by owner; the ' + DATA.colored.length + ' largest owners overall have their own color. ' +
  (M.n_errors ? 'A ⚠ after the size means part of that folder could not be read.' : '');
const legend = document.getElementById('legend-folders');
DATA.colored.concat(DATA.owners.length > DATA.colored.length ? [null] : []).forEach(n => {
  const item = el('div', 'legend-item');
  const sw = el('span', 'legend-swatch'); sw.style.background = ownerColor(n);
  item.appendChild(sw);
  item.appendChild(document.createTextNode(n || 'Other owners'));
  legend.appendChild(item);
});
const fmax = Math.max.apply(null, DATA.folders.map(f => f.bytes).concat([1]));
DATA.folders.forEach(f => {
  const segs = f.segs.filter(s => s.bytes > f.bytes * 0.004).map(s => ({ bytes: s.bytes, color: ownerColor(s.owner) }));
  const val = el('div', 'bar-value', fmtBytes(f.bytes));
  if (f.errors) { val.appendChild(document.createTextNode(' ')); const w = el('span', 'err', '⚠'); w.title = f.errors + ' unreadable paths'; val.appendChild(w); }
  const row = barRow(f.name, f.name, Math.max(100 * f.bytes / fmax, f.bytes > 0 ? 0.6 : 0), segs, val);
  hover(row, () => showTooltip(row, f.name + ' · ' + fmtBytes(f.bytes),
    f.owners.map(o => ({ k: o.owner, v: fmtBytes(o.bytes) + ' · ' + pct(o.bytes, f.bytes) + '%', color: ownerColor(o.owner) }))));
  document.getElementById('chart-folders').appendChild(row);
});
table(document.getElementById('table-folders'), [
  { h: 'Folder', cls: 'l path', v: f => f.name },
  { h: 'Used', v: f => fmtBytes(f.bytes) },
  { h: 'Largest owner', cls: 'l', v: f => f.owners.length ? f.owners[0].owner : '' },
  { h: 'Their share', v: f => f.owners.length ? pct(f.owners[0].bytes, f.bytes) + '%' : '' },
  { h: 'Owners', v: f => f.owners.length },
  { h: 'Unreadable', v: f => f.errors ? fmtInt(f.errors) : '' },
], DATA.folders);

// ---- largest folders ----
document.getElementById('desc-largest').textContent =
  'The biggest folders up to ' + M.depth + ' levels below the scanned path, plus each owner\'s main location: ' +
  'the deepest folder holding at least ' + Math.round(100 * M.hotspot_share) + '% of their data. ' +
  'Each size includes everything inside it.' +
  (DATA.spread.length ? ' No single folder holds most of the data of ' + DATA.spread.join(', ') + '.' : '');
table(document.getElementById('table-largest'), [
  { h: 'Folder', cls: 'l path', v: f => f.path },
  { h: 'Used', v: f => fmtBytes(f.bytes) },
  { h: 'Main owner', cls: 'l', v: f => f.main_owner },
  { h: 'Their share', v: f => shareBar(pct(f.main_bytes, f.bytes)) },
  { h: 'Owners', v: f => f.n_owners },
  { h: 'Main location of', cls: 'l', v: f => {
      const s = el('span');
      f.home_of.forEach((h, i) => {
        if (i) s.appendChild(document.createTextNode(', '));
        s.appendChild(document.createTextNode(h.owner + ' '));
        s.appendChild(el('span', 'muted', pct(h.bytes, h.owner_total) + '% of theirs'));
      });
      return s;
  } },
], DATA.largest);

// ---- unreadable paths ----
const noteErr = document.getElementById('note-errors');
if (M.n_errors) {
  const worst = Object.entries(M.error_top).sort((a, b) => b[1] - a[1]).slice(0, 12)
    .map(kv => kv[0] + ' (' + fmtInt(kv[1]) + ')').join(', ');
  noteErr.textContent = fmtInt(M.n_errors) + ' files or folders could not be read, usually because of permissions, so their contents are missing from the totals. By top-level folder: ' + worst + '. The JSON file lists the first paths.';
} else {
  noteErr.textContent = 'None. Every file and folder was readable.';
}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Scan a directory and write an HTML report of disk usage by file owner.")
    ap.add_argument("path", nargs="?", help="directory to scan")
    ap.add_argument("-o", "--output",
                    help="output HTML path (default: ./diskusage_<name>_<date>.html); "
                         "the JSON is written next to it")
    ap.add_argument("-j", "--workers", type=int, default=min(8, os.cpu_count() or 1),
                    help="parallel scanning processes (default: %(default)s)")
    ap.add_argument("--depth", type=int, default=3,
                    help="how many folder levels to measure for the folder tables (default: %(default)s)")
    ap.add_argument("--cross-filesystems", action="store_true",
                    help="also scan other filesystems mounted inside the tree")
    ap.add_argument("--from-json", metavar="FILE",
                    help="rebuild the HTML from a saved JSON scan instead of scanning")
    args = ap.parse_args()

    if args.from_json:
        with open(args.from_json) as fh:
            saved = json.load(fh)
        out_html = args.output or os.path.splitext(args.from_json)[0] + ".html"
        with open(out_html, "w") as fh:
            fh.write(render_html(saved))
        print("Wrote", out_html)
        return

    if not args.path:
        ap.error("give a directory to scan, or --from-json FILE")
    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        ap.error("not a directory: " + root)

    name = os.path.basename(root.rstrip("/")) or "root"
    out_html = args.output or "diskusage_%s_%s.html" % (name, datetime.date.today().isoformat())
    out_json = os.path.splitext(out_html)[0] + ".json"

    sys.stderr.write("Scanning %s with %d workers (progress every %ds)\n" % (root, args.workers, PROGRESS_EVERY_S))
    result = scan(root, args.workers, args.depth, args.cross_filesystems)
    saved = to_saved(root, result, args.workers, args.depth, args.cross_filesystems)

    with open(out_json, "w") as fh:
        json.dump(saved, fh)
    with open(out_html, "w") as fh:
        fh.write(render_html(saved))

    total = sum(saved["folders"].get("", {}).values())
    print("Scanned %s entries in %s: %s, %d owners, %d unreadable paths" % (
        format(result["n_entries"], ","), fmt_duration(result["duration_s"]),
        fmt_bytes(total), len(saved["owners"]), result["n_errors"]))
    print("Wrote", out_html)
    print("Wrote", out_json)


if __name__ == "__main__":
    main()
