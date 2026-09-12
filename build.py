#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grzegorz Olędzki
"""Render a one-page table of "which depot runs which brigade" for every WTP bus line.

Reads the daily-rebuilt zbiorkom.live Warsaw GTFS
(https://cdn.zbiorkom.live/gtfs/warsaw.zip) and, for the furthest-future date of each
day type the feed carries, reports the operating depot of every (line, brigade) pair.

The depot comes straight from `depot_id`, a non-standard column zbiorkom adds to
trips.txt. It names MZA's depots by an internal code — R-7(W) is Woronicza, R11(K)
Kleszczowa — and the contracted operators (Mobilis, PKS Grodzisk, ReloBus, KMŁ) outright,
so it covers the whole network, not just the part that models pull-out/pull-in runs.

Day types are not in the feed either — the service ids are opaque numbers — so they are
derived from the weekday. Public holidays are not yet handled: WTP runs its Sunday
timetable on them, but they read as ordinary weekdays here.

Stdlib only; reads trips.txt, routes.txt and calendar_dates.txt, never stop_times.txt.
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

FEED_URL = "https://cdn.zbiorkom.live/gtfs/warsaw.zip"
# The CDN sits behind Cloudflare, which 403s the default "Python-urllib/x.y" agent.
USER_AGENT = "przydzial-brygad (+https://github.com/mccartney/przydzial-brygad)"
FEED_FILE = Path("warsaw.zip")
DATA = Path("brygady.json")
OUT = Path("przydzial.html")

BUS_ROUTE_TYPE = "3"
# Suburban "L" lines are left out of the table. This feed does name their commune
# operators (depot_id G-14, G-17, G-30, G-42), so they could be added.
SUBURBAN_LINE = re.compile(r"^L-?\d+$")
# zbiorkom's depot_id -> (label used in the table, full name for the legend). MZA's codes
# are internal — the parenthesised letter is the site's initial — so they are relabelled
# to the R-n numbering ZTM and MZA use in public. Verified against the depot stops the
# trips of each code actually terminate at.
#
# The contracted operators get a code per depot (Mob12/Mob88, Relobus29/Relobus38); those
# collapse to one label each, since the table answers who runs a brigade, not from which
# of an operator's yards. Several lines mix both of an operator's depots.
DEPOTS = {
    "R-7(W)": ("R-1", "R-1 Woronicza"),
    "R11(K)": ("R-2", "R-2 Kleszczowa"),
    "R10(O)": ("R-3", "R-3 Ostrobramska"),
    "R13(S)": ("R-4", "R-4 Stalowa"),
    "R14(P)": ("R-6", "R-6 Płochocińska"),
    "Mob12": ("Mobilis", "Mobilis"),
    "Mob88": ("Mobilis", "Mobilis"),
    "PKS_A82": ("PKS Grodzisk", "PKS Grodzisk Mazowiecki"),
    "Relobus29": ("Relobus", "Relobus"),
    "Relobus38": ("Relobus", "Relobus"),
    "KMŁ K-26": ("KMŁ", "Komunikacja Miejska Łomianki"),
}

# The two columns of the table, each merging the ZTM day-types it covers.
DAY_GROUPS = [
    ("powszedni", "Dzień powszedni", ("PcS", "PtS")),
    ("swiateczny", "Sobota / niedziela i święta", ("SbS", "NdS")),
]
DAY_NAME = {"PcS": "pon.–czw.", "PtS": "piątek", "SbS": "sobota", "NdS": "niedziela"}

UNKNOWN = "nieznany"
DEPOT_ORDER = ["R-1", "R-2", "R-3", "R-4", "R-5", "R-6",
               "Mobilis", "PKS Grodzisk", "Relobus", "KMŁ", UNKNOWN]
DEPOT_COLOR = {
    "R-1": "#cfe0ff",
    "R-2": "#cdeed6",
    "R-3": "#ffdfb5",
    "R-4": "#f8d1e4",
    "R-5": "#c9ece9",
    "R-6": "#ddd2f4",
    "Mobilis": "#ffe3b0",
    "PKS Grodzisk": "#dcccc4",
    "Relobus": "#d2e8ae",
    "KMŁ": "#f2c9e2",
    UNKNOWN: "#e6e6e6",
}
DEPOT_FULL = {}  # short label -> "R-1 Woronicza", filled while parsing

# Guards against publishing a page built from a truncated or malformed feed. depot_id
# covers all but a handful of trips, so anything near the old heuristic's ~88% means the
# column has changed shape or the DEPOTS codes have been renamed underneath us.
MIN_LINES = 200
MIN_COVERAGE = 0.90


class Feed:
    """Reads GTFS tables straight out of the .zip — nothing is unpacked to disk."""

    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)

    def _open(self, table):
        return io.TextIOWrapper(self.zip.open(table + ".txt"), encoding="utf-8-sig", newline="")

    def rows(self, table):
        with self._open(table) as fh:
            yield from csv.DictReader(fh)

    def version(self):
        for row in self.rows("feed_info"):
            return row["feed_version"]
        return None


def download(path):
    if path.exists():
        print(f"using existing {path}", file=sys.stderr)
        return
    print(f"downloading {FEED_URL}", file=sys.stderr)
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=300) as resp, open(path, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    print(f"  {path.stat().st_size / 1e6:.0f} MB", file=sys.stderr)


def day_code(date):
    """ZTM day type of a calendar date.

    The feed does not carry it — service ids are opaque numbers — so it comes from the
    weekday alone. A public holiday runs the Sunday timetable but still looks like a
    weekday here, so one falling inside the feed window lands in the wrong column.
    """
    return {4: "PtS", 5: "SbS", 6: "NdS"}.get(date.weekday(), "PcS")


def pick_days(feed):
    """Furthest-future date the feed carries for each day type.

    The feed spans about nine days, and a timetable change lands on the later ones first,
    so the last date of each type is the newest edition of that day's schedule.
    """
    days = {}
    for raw in sorted({r["date"] for r in feed.rows("calendar_dates")}):
        date = datetime.date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
        days[day_code(date)] = date
    return days


def read_services(feed, days):
    """service_id -> the day-type codes it runs under, for the picked dates only.

    A service id is opaque here and nothing stops one from running on two of the picked
    dates, so a service maps to a set of codes rather than a single one.
    """
    wanted = {f"{date:%Y%m%d}": code for code, date in days.items()}
    services = {}
    for r in feed.rows("calendar_dates"):
        code = wanted.get(r["date"])
        if code:
            services.setdefault(r["service_id"], set()).add(code)
    return services


def read_trips(feed, services):
    """One pass over trips.txt: (day-type, line, brigade) -> set of depot labels.

    Returns that alongside a count of depot_id values missing from DEPOTS, so a renamed
    or newly added operator shows up in the log instead of quietly becoming "nieznany".
    """
    bus_lines = {}
    for r in feed.rows("routes"):
        if r["route_type"] == BUS_ROUTE_TYPE and not SUBURBAN_LINE.match(r["route_short_name"]):
            bus_lines[r["route_id"]] = r["route_short_name"]

    pair_depots = {}
    unmapped = {}
    for t in feed.rows("trips"):
        codes = services.get(t["service_id"])
        line = bus_lines.get(t["route_id"])
        # brigade and depot_id are zbiorkom extensions, not GTFS: read them defensively so
        # that a feed dropping either fails the coverage guard instead of the parse.
        brigade = t.get("brigade")
        if not codes or line is None or not brigade:
            continue
        depot = t.get("depot_id")
        label = None
        if depot in DEPOTS:
            label, full = DEPOTS[depot]
            DEPOT_FULL.setdefault(label, full)
        elif depot:
            unmapped[depot] = unmapped.get(depot, 0) + 1
        for code in codes:
            slot = pair_depots.setdefault((code, line, brigade), set())
            if label:
                slot.add(label)
    return pair_depots, unmapped


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
                label = " / ".join(sorted(entry["depots"], key=depot_sort)) or UNKNOWN
                out.setdefault(line, {}).setdefault(group_key, {}).setdefault(label, []).append(
                    (brigade, entry["only"])
                )
    for groups in out.values():
        for depots in groups.values():
            for brigades in depots.values():
                brigades.sort(key=lambda e: brigade_sort(e[0]) + (e[0],))
    # Sorted so the committed brygady.json diffs line by line between rebuilds; the feed
    # lists trips in no particular order.
    return {line: out[line] for line in sorted(out, key=line_sort)}


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
  <div class="sub">źródło: <a href="https://zbiorkom.live">zbiorkom.live</a> ·
    feed {html.escape(payload["feedVersion"] or "?")} · zaktualizowano {payload["generated"]}</div>
  <div class="tools"><input id="q" type="search" placeholder="filtruj: numer linii lub zakład"></div>
  <div class="scroll"><table>
    <thead><tr><th>linia</th>{head}</tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table></div>
  <div class="legend">{''.join(legend)}</div>
  <div class="foot">
    Zakład bierzemy wprost z pola <code>depot_id</code>, które zbiorkom.live dokłada do
    <code>trips.txt</code> — obejmuje ono zarówno zajezdnie MZA, jak i przewoźników
    kontraktowych. Jako <b>nieznany</b> wychodzą tylko kursy bez tego pola.
    Górny indeks przy numerze brygady oznacza, że kursuje ona tylko w części dni danej kolumny.<br>
    Dane: <a href="https://ztm.waw.pl">ZTM Warszawa</a> ·
    GTFS: <a href="https://zbiorkom.live">zbiorkom.live</a> ·
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

    days = pick_days(feed)
    services = read_services(feed, days)
    print(f"feed_version: {version}", file=sys.stderr)
    for code, date in sorted(days.items()):
        print(f"  {DAY_NAME[code]:<10} {date}", file=sys.stderr)

    pair_depots, unmapped = read_trips(feed, services)
    print(f"{len(pair_depots)} (day, line, brigade) slots", file=sys.stderr)
    for depot, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
        print(f"  WARNING: depot_id {depot!r} not in DEPOTS ({n} trips)", file=sys.stderr)

    payload = {
        "generated": f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}",
        "feedVersion": version,
        "services": {code: {"date": date.isoformat()} for code, date in days.items()},
        "depots": dict(sorted(DEPOT_FULL.items())),
        "lines": group_rows(pair_depots, days),
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
