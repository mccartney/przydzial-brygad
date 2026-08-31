#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grzegorz Olędzki
"""Render a one-page table of "which depot runs which brigade" for every WTP bus line.

Reads the daily-rebuilt mkuran Warsaw GTFS (https://mkuran.pl/gtfs/warsaw.zip) and, for
the furthest-future schedule day the feed carries, works out the operating depot of each
(line, brigade) pair. The depot is not a field in GTFS — it is recovered from the
non-revenue pull-out/pull-in trips (`exceptional=1`, variants TD-*/TZ-*), which start or
end at depot stops like "R-1 Zajezdnia Woronicza", and then propagated along `block_id`
so a brigade without its own depot run inherits it from the rest of the vehicle's day.

Only MZA models those runs, so brigades of the contracted operators (Mobilis, PKS
Grodzisk, ReloBus) come out as "nieznany" — see README.md.

Stdlib only. One streaming pass over stop_times.txt, so it fits in CI memory.
"""

import argparse
import csv
import datetime
import html
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

FEED_URL = "https://mkuran.pl/gtfs/warsaw.zip"
FEED_FILE = Path("warsaw.zip")
DATA = Path("brygady.json")
OUT = Path("przydzial.html")

BUS_ROUTE_TYPE = "3"
# Suburban "L" lines are run by commune operators the feed says nothing about.
SUBURBAN_LINE = re.compile(r"^L-?\d+$")
# Surface-network services are dated, e.g. "2026-09-08:PcS"; metro ones ("PcM") are not.
DATED_SERVICE = re.compile(r"^(\d{4}-\d{2}-\d{2}):(PcS|PtS|SbS|NdS)$")
MZA_DEPOT = re.compile(r"^(R-\d+) Zajezdnia (.+)$")
# The one depot-like terminal that does not follow the "R-n Zajezdnia X" naming.
EXTRA_DEPOTS = {"Wydział Włościańska": "Włościańska"}
# Włościańska is an outstation, not a home depot: a bus pulls out of R-4 (or R-6) in the
# morning and parks there overnight, or the reverse. It never turns up as a brigade's only
# depot, so it says where the bus sleeps, not who runs it — hide it whenever a real
# depot is also on the block.
OUTSTATIONS = {"Włościańska"}

# The two columns of the table, each merging the ZTM day-types it covers.
DAY_GROUPS = [
    ("powszedni", "Dzień powszedni", ("PcS", "PtS")),
    ("swiateczny", "Sobota / niedziela i święta", ("SbS", "NdS")),
]
DAY_NAME = {"PcS": "pon.–czw.", "PtS": "piątek", "SbS": "sobota", "NdS": "niedziela"}

UNKNOWN = "nieznany"
DEPOT_ORDER = ["R-1", "R-2", "R-3", "R-4", "R-5", "R-6", "Włościańska", UNKNOWN]
DEPOT_COLOR = {
    "R-1": "#cfe0ff",
    "R-2": "#cdeed6",
    "R-3": "#ffdfb5",
    "R-4": "#f8d1e4",
    "R-5": "#c9ece9",
    "R-6": "#ddd2f4",
    "Włościańska": "#fdefac",
    UNKNOWN: "#e6e6e6",
}
DEPOT_FULL = {}  # short label -> "R-1 Woronicza", filled while parsing

# Guards against publishing a page built from a truncated or malformed feed.
MIN_LINES = 200
MIN_COVERAGE = 0.60


class Feed:
    """Reads GTFS tables straight out of the .zip — nothing is unpacked to disk."""

    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)

    def _open(self, table):
        return io.TextIOWrapper(self.zip.open(table + ".txt"), encoding="utf-8-sig", newline="")

    def rows(self, table):
        with self._open(table) as fh:
            yield from csv.DictReader(fh)

    def lines(self, table):
        """Raw text lines — lets us prefilter the huge tables before paying for CSV parsing."""
        with self._open(table) as fh:
            yield from fh

    def version(self):
        for row in self.rows("feed_info"):
            return row["feed_version"]
        return None


def download(path):
    if path.exists():
        print(f"using existing {path}", file=sys.stderr)
        return
    print(f"downloading {FEED_URL}", file=sys.stderr)
    with urllib.request.urlopen(FEED_URL, timeout=300) as resp, open(path, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    print(f"  {path.stat().st_size / 1e6:.0f} MB", file=sys.stderr)


def pick_services(feed):
    """Latest dated service per day-type — the furthest-future edition the feed carries.

    `calendar_dates.txt` repeats each service weekly to the end of the month, so the
    highest date *prefix* (not the highest date it runs on) is the newest schedule.
    """
    latest = {}
    for row in feed.rows("calendar_dates"):
        m = DATED_SERVICE.match(row["service_id"])
        if not m:
            continue
        date, code = m.groups()
        if code not in latest or date > latest[code][0]:
            latest[code] = (date, row["service_id"])
    return latest


def depot_label(stop_name):
    """'R-1 Zajezdnia Woronicza' -> 'R-1'; remembers the full name for the legend."""
    m = MZA_DEPOT.match(stop_name)
    if m:
        short, place = m.groups()
        DEPOT_FULL.setdefault(short, f"{short} {place}")
        return short
    short = EXTRA_DEPOTS.get(stop_name)
    if short:
        DEPOT_FULL.setdefault(short, stop_name)
    return short


def read_trips(feed, services):
    """One pass over trips.txt. Returns what every later step needs, keyed by day-type code.

    services maps service_id -> day-type code.
    """
    bus_lines = {}
    for r in feed.rows("routes"):
        if r["route_type"] == BUS_ROUTE_TYPE and not SUBURBAN_LINE.match(r["route_short_name"]):
            bus_lines[r["route_id"]] = r["route_short_name"]

    pairs = {}          # (code, line, brigade) -> set of block_ids
    block_depots = {}   # block_id -> set of depot labels
    tech = {}           # trip_id of a non-revenue trip -> block_id
    for t in feed.rows("trips"):
        code = services.get(t["service_id"])
        line = bus_lines.get(t["route_id"])
        if code is None or line is None:
            continue
        block = t["block_id"]
        pairs.setdefault((code, line, t["block_short_name"]), set()).add(block)
        if t["exceptional"] == "1":
            tech[t["trip_id"]] = block
            # A pull-in's headsign is already the depot; a pull-out's is not, so we still
            # need the stop_times pass below to catch where it started from.
            label = depot_label(t["trip_headsign"])
            if label:
                block_depots.setdefault(block, set()).add(label)
    return pairs, block_depots, tech


def read_technical_terminals(feed, tech, dates):
    """First and last stop of every non-revenue trip, from a single stop_times pass."""
    prefixes = tuple(f"{d}:" for d in dates)
    header = None
    ends = {}  # trip_id -> {"first": (seq, stop_id), "last": (seq, stop_id)}
    for raw in feed.lines("stop_times"):
        if header is None:
            header = next(csv.reader([raw]))
            i_trip, i_seq, i_stop = (header.index(c) for c in ("trip_id", "stop_sequence", "stop_id"))
            continue
        if not raw.startswith(prefixes):
            continue
        row = next(csv.reader([raw]))
        trip = row[i_trip]
        if trip not in tech:
            continue
        seq, stop = int(row[i_seq]), row[i_stop]
        e = ends.setdefault(trip, {"first": (seq, stop), "last": (seq, stop)})
        if seq < e["first"][0]:
            e["first"] = (seq, stop)
        if seq > e["last"][0]:
            e["last"] = (seq, stop)
    return ends


def resolve_depots(feed, pairs, block_depots, tech, ends):
    """Fold the technical-trip terminals into block_depots, then read depots off the blocks."""
    wanted = {stop for e in ends.values() for _seq, stop in (e["first"], e["last"])}
    names = {}
    for s in feed.rows("stops"):
        if s["stop_id"] in wanted:
            names[s["stop_id"]] = s["stop_name"]

    for trip, e in ends.items():
        for _seq, stop in (e["first"], e["last"]):
            label = depot_label(names.get(stop, ""))
            if label:
                block_depots.setdefault(tech[trip], set()).add(label)

    # A brigade inherits every depot seen anywhere in its vehicle's whole-day chain.
    return {key: set().union(*(block_depots.get(b, set()) for b in blocks)) if blocks else set()
            for key, blocks in pairs.items()}


def group_rows(pair_depots, services_by_code):
    """Collapse the day-types into the table's two columns.

    Returns {line: {group_key: {depot_label: [(brigade, only_in_codes_or_None), ...]}}}.
    """
    table = {}
    for group_key, _title, codes in DAY_GROUPS:
        live = [c for c in codes if c in services_by_code]
        for code in live:
            for (c, line, brigade), depots in pair_depots.items():
                if c != code:
                    continue
                slot = table.setdefault(line, {}).setdefault(group_key, {})
                entry = slot.setdefault(brigade, {"depots": set(), "codes": set()})
                entry["depots"] |= depots
                entry["codes"].add(code)
        # A brigade that skips part of the group (e.g. Friday only) gets flagged.
        for line, groups in table.items():
            for brigade, entry in groups.get(group_key, {}).items():
                entry["only"] = None if entry["codes"] == set(live) else sorted(entry["codes"])

    out = {}
    for line, groups in table.items():
        for group_key, brigades in groups.items():
            for brigade, entry in brigades.items():
                depots = entry["depots"] - OUTSTATIONS or entry["depots"]
                label = " / ".join(sorted(depots, key=depot_sort)) or UNKNOWN
                out.setdefault(line, {}).setdefault(group_key, {}).setdefault(label, []).append(
                    (brigade, entry["only"])
                )
    for groups in out.values():
        for depots in groups.values():
            for brigades in depots.values():
                brigades.sort(key=lambda e: brigade_sort(e[0]) + (e[0],))
    return out


def depot_sort(label):
    parts = [DEPOT_ORDER.index(p) if p in DEPOT_ORDER else len(DEPOT_ORDER) for p in label.split(" / ")]
    return (parts, label)


def brigade_sort(brigade):
    return (0, int(brigade), len(brigade)) if brigade.isdigit() else (1, 0, 0)


def line_sort(line):
    if line.isdigit():
        return (0, "", int(line), line)
    m = re.match(r"^([A-Za-z]+)-?(\d+)$", line)
    if m:
        return (1, m.group(1), int(m.group(2)), line)
    return (2, line, 0, line)


def marker(only):
    """Superscript flag for a brigade that runs on only part of the merged day group."""
    names = ", ".join(DAY_NAME[c] for c in only)
    short = "".join(c[:2].lower() for c in only)
    return f'<sup class="mk" title="tylko {html.escape(names)}">{html.escape(short)}</sup>'


def same_numbering(a, b):
    """Do two brigade numbers belong to one run? '9'+'10' yes, '9'+'010' no, '09'+'010' no.

    ZTM uses the leading zero to tell two brigade series apart, so a run may never cross
    from the unpadded series into the padded one (or between padding widths).
    """
    if not (a.isdigit() and b.isdigit()):
        return False
    if a.startswith("0") != b.startswith("0"):
        return False
    return len(a) == len(b) if a.startswith("0") else True


def format_brigades(entries):
    """Sorted brigade list with consecutive runs compressed: '2-4, 6-7, 9, 017-020'."""
    entries = sorted(entries, key=lambda e: brigade_sort(e[0]) + (e[0],))
    out, run = [], []

    def flush():
        if not run:
            return
        only = run[0][1]
        text = (f"{html.escape(run[0][0])}-{html.escape(run[-1][0])}" if len(run) >= 2
                else html.escape(run[0][0]))
        out.append(text + (marker(only) if only else ""))
        run.clear()

    for brigade, only in entries:
        if run:
            prev, prev_only = run[-1]
            # Same day-group flag and an unbroken step of 1 within the same series.
            if only == prev_only and same_numbering(prev, brigade) and int(brigade) - int(prev) == 1:
                run.append((brigade, only))
                continue
        flush()
        run.append((brigade, only))
    flush()
    return ", ".join(out)


def build_html(payload):
    lines = payload["lines"]
    services = payload["services"]
    full_name = payload["depots"]

    counts = {}
    for groups in lines.values():
        for depots in groups.values():
            for label, brigades in depots.items():
                for part in label.split(" / "):
                    counts[part] = counts.get(part, 0) + len(brigades)

    head = "".join(f"<th>{html.escape(title)}</th>" for _k, title, _c in DAY_GROUPS)

    body = []
    for line in sorted(lines, key=line_sort):
        cells = [f'<th class="line">{html.escape(line)}</th>']
        for group_key, _title, _codes in DAY_GROUPS:
            depots = lines[line].get(group_key)
            if not depots:
                cells.append('<td class="none">—</td>')
                continue
            blocks = []
            for label in sorted(depots, key=depot_sort):
                # A brigade split across two depots keeps the first one's colour, so it
                # never reads as grey "nieznany".
                color = DEPOT_COLOR.get(label.split(" / ")[0], DEPOT_COLOR[UNKNOWN])
                tip = html.escape(full_name.get(label, label))
                blocks.append(
                    f'<div class="g"><span class="d" style="background:{color}" title="{tip}">'
                    f'{html.escape(label)}</span>{format_brigades(depots[label])}</div>'
                )
            cells.append("<td>" + "".join(blocks) + "</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")

    legend = []
    for label in DEPOT_ORDER:
        if label not in counts:
            continue
        full = full_name.get(label, label)
        legend.append(
            f'<span class="leg"><span class="sw" style="background:{DEPOT_COLOR[label]}"></span>'
            f'{html.escape(full)} <span class="cnt">{counts[label]}</span></span>'
        )

    day_note = " · ".join(
        f"{DAY_NAME[c]} {services[c]['date']}" for _k, _t, codes in DAY_GROUPS for c in codes if c in services
    )

    return f"""<!DOCTYPE html>
<html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Przydział brygad — zakłady i przewoźnicy WTP</title>
<style>
  :root {{ font-family: -apple-system, system-ui, sans-serif; }}
  body {{ margin: 24px; color: #1b1b1b; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 3px; }}
  .sub a {{ color: #06c; }}
  .tools {{ margin: 14px 0 0; }}
  #q {{ font: inherit; font-size: 13px; padding: 5px 9px; width: 220px;
    border: 1px solid #ccc; border-radius: 5px; }}
  .scroll {{ overflow-x: auto; border: 1px solid #ddd; border-radius: 6px; margin-top: 10px; }}
  table {{ border-collapse: collapse; font-size: 13px; width: 100%; }}
  th, td {{ text-align: left; vertical-align: top; padding: 5px 8px;
    border-bottom: 1px solid #eee; }}
  thead th {{ position: sticky; top: 0; background: #fafafa; z-index: 3; color: #555;
    font-weight: 600; border-bottom: 1px solid #ddd; }}
  thead th:first-child {{ left: 0; z-index: 4; background: #f0f0f0; }}
  th.line {{ position: sticky; left: 0; background: #f4f4f4; z-index: 2; width: 1%;
    white-space: nowrap; font-weight: 600; font-variant-numeric: tabular-nums; }}
  td {{ background: #fff; }}
  td.none {{ color: #bbb; }}
  .g {{ margin: 1px 0; line-height: 1.5; }}
  .d {{ display: inline-block; min-width: 3.4em; margin-right: 6px; padding: 0 6px;
    border-radius: 4px; font-size: 11px; font-weight: 600; text-align: center;
    border: 1px solid rgba(0,0,0,0.10); }}
  .mk {{ color: #999; font-size: 9px; margin-left: 1px; }}
  .legend {{ margin: 16px 0 4px; display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; }}
  .leg {{ display: inline-flex; align-items: center; gap: 5px; }}
  .sw {{ width: 13px; height: 13px; border-radius: 3px; border: 1px solid rgba(0,0,0,0.12);
    display: inline-block; }}
  .cnt {{ color: #999; }}
  .foot {{ margin-top: 14px; color: #777; font-size: 12px; line-height: 1.6; }}
  .foot a {{ color: #06c; }}
</style></head>
<body>
  <h1>Przydział brygad — zakłady i przewoźnicy WTP</h1>
  <div class="sub">{len(lines)} linii autobusowych · rozkład: {html.escape(day_note)} —
    najdalej wysunięta w przyszłość edycja w feedzie · bez linii L</div>
  <div class="sub">źródło: <a href="https://mkuran.pl/gtfs/">WarsawGTFS</a> ·
    feed {html.escape(payload["feedVersion"] or "?")} · zaktualizowano {payload["generated"]}</div>
  <div class="tools"><input id="q" type="search" placeholder="filtruj: numer linii lub zakład"></div>
  <div class="scroll"><table>
    <thead><tr><th>linia</th>{head}</tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table></div>
  <div class="legend">{''.join(legend)}</div>
  <div class="foot">
    Zakład wyznaczony z kursów technicznych (zjazdów i wyjazdów) w GTFS, propagowanych po
    całodziennym łańcuchu pojazdu. Kursów technicznych nie publikują przewoźnicy kontraktowi
    (Mobilis, PKS Grodzisk, ReloBus) — ich brygady wychodzą jako <b>nieznany</b>.
    Postój zewnętrzny Wydział Włościańska pomijamy, gdy brygada ma też zjazd do zajezdni —
    mówi on, gdzie autobus nocuje, a nie kto go obsługuje.
    Górny indeks przy numerze brygady oznacza, że kursuje ona tylko w części dni danej kolumny.<br>
    Dane: <a href="https://ztm.waw.pl">ZTM Warszawa</a> ·
    GTFS: <a href="https://mkuran.pl/gtfs/">Mikołaj Kuranowski</a> ·
    kształty tras: <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap (ODbL)</a>
  </div>
<script>
  const q = document.getElementById('q');
  const rows = [...document.querySelectorAll('tbody tr')];
  q.addEventListener('input', () => {{
    const t = q.value.trim().toLowerCase();
    for (const r of rows) r.hidden = t && !r.textContent.toLowerCase().includes(t);
  }});
</script>
</body></html>
"""


def coverage(payload):
    """Share of (line, group, brigade) slots that got a real depot, not 'nieznany'."""
    total = known = 0
    for groups in payload["lines"].values():
        for depots in groups.values():
            for label, brigades in depots.items():
                total += len(brigades)
                if label != UNKNOWN:
                    known += len(brigades)
    return known / total if total else 0.0


def usable(payload):
    return payload and len(payload.get("lines", {})) >= MIN_LINES and coverage(payload) >= MIN_COVERAGE


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed", type=Path, default=FEED_FILE,
                    help=f"path to warsaw.zip (downloaded from {FEED_URL} if missing)")
    args = ap.parse_args()

    download(args.feed)
    feed = Feed(args.feed)
    version = feed.version()

    latest = pick_services(feed)
    services = {sid: code for code, (_date, sid) in latest.items()}
    dates = [date for date, _sid in latest.values()]
    print(f"feed_version: {version}", file=sys.stderr)
    for code, (date, sid) in sorted(latest.items()):
        print(f"  {DAY_NAME[code]:<10} {sid}", file=sys.stderr)

    pairs, block_depots, tech = read_trips(feed, services)
    print(f"{len(pairs)} (day, line, brigade) slots · {len(tech)} technical trips", file=sys.stderr)
    ends = read_technical_terminals(feed, tech, dates)
    pair_depots = resolve_depots(feed, pairs, block_depots, tech, ends)

    payload = {
        "generated": f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}",
        "feedVersion": version,
        "services": {code: {"date": date, "serviceId": sid} for code, (date, sid) in latest.items()},
        "depots": dict(sorted(DEPOT_FULL.items())),
        "lines": group_rows(pair_depots, latest),
    }

    if usable(payload):
        DATA.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{len(payload['lines'])} lines · depot coverage {coverage(payload):.1%}", file=sys.stderr)
    else:
        got = len(payload.get("lines", {}))
        print(f"WARNING: fresh build unusable ({got} lines, coverage {coverage(payload):.1%}); "
              f"falling back to committed {DATA}", file=sys.stderr)
        payload = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else None
        if not usable(payload):
            raise SystemExit(f"ERROR: no usable data — feed looks broken and {DATA} cannot stand in")
        print(f"using committed data from {payload['generated']}", file=sys.stderr)

    OUT.write_text(build_html(payload), encoding="utf-8")
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
