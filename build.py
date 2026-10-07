#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grzegorz Olędzki
"""Render a one-page table of "which depot runs which brigade" for every WTP bus line.

Reads the Warsaw GTFS at https://436.pl/gtfs/warsaw.zip, built by
https://github.com/mccartney/WarsawGTFS, and, for the newest timetable of each day type
the feed carries, reports the operating depot of every (line, brigade) pair.

The depot comes straight from `depot_id`, a non-standard column the feed adds to
trips.txt. It names MZA's depots by an internal code — R-7(W) is Woronicza, R11(K)
Kleszczowa — and the contracted operators (Mobilis, PKS Grodzisk, ReloBus, KMŁ) outright,
so it covers the whole network, not just the part that models pull-out/pull-in runs.

Day types come from the service ids, which name them outright ("2026-10-03:SbS"), so
a public holiday running the Sunday timetable is already filed as a Sunday.

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
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

FEED_URL = "https://436.pl/gtfs/warsaw.zip"
# Identify ourselves rather than send the default "Python-urllib/x.y" agent, which
# CDNs like Cloudflare tend to 403.
USER_AGENT = "przydzial-brygad (+https://github.com/mccartney/przydzial-brygad)"
FEED_FILE = Path("warsaw.zip")
DATA = Path("brygady.json")
OUT = Path("przydzial.html")

BUS_ROUTE_TYPE = "3"
# Local "L" lines are left out of the table. This feed does name their commune
# operators (depot_id G-14, G-17, G-30, G-42), so they could be added.
LOCAL_LINE = re.compile(r"^L-?\d+$")
# depot_id -> (label used in the table, full name for the legend). MZA's codes
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
# One-letter tag for each column on the line cards.
DAY_SHORT = {"powszedni": "DP", "swiateczny": "DŚ"}
DAY_NAME = {"PcS": "pon.–czw.", "PtS": "piątek", "SbS": "sobota", "NdS": "niedziela"}

ROUTE_URL = "https://zbiorkom.live/warsaw/route/{}/brigades"

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


# Bus service ids name the timetable's first date and ZTM day type: "2026-10-03:SbS".
# Metro services (PcM, NdM, ...) and anything else fall outside the pattern.
SERVICE_ID = re.compile(r"^(\d{4})-(\d{2})-(\d{2}):(PcS|PtS|SbS|NdS)$")


def read_editions(feed):
    """Every bus timetable edition in the feed: {service_id: (date, day-type code)}.

    The feed carries one service per day of its first week or so and then repeats those
    services on the dates after it, so the last calendar date of a type can point back to
    an older edition. The date inside the service id is what orders the editions. The day
    type comes from the id as well, so a public holiday on the Sunday timetable already
    counts as NdS.
    """
    editions = {}
    for sid in {r["service_id"] for r in feed.rows("calendar_dates")}:
        m = SERVICE_ID.match(sid)
        if m:
            editions[sid] = (datetime.date(int(m[1]), int(m[2]), int(m[3])), m[4])
    return editions


def pick_days(editions):
    """Newest edition of each day type: {code: (date, service_id)}."""
    days = {}
    for sid, (date, code) in editions.items():
        if code not in days or date > days[code][0]:
            days[code] = (date, sid)
    return days


def newest_slots(slots, days):
    """Narrow read_trips' per-edition slots to the newest edition of each day type,
    re-keyed by the day-type code: (code, line, brigade) -> set of depot labels."""
    newest = {sid: code for code, (_date, sid) in days.items()}
    return {(newest[sid], line, brigade): depots
            for (sid, line, brigade), depots in slots.items() if sid in newest}


def find_changes(slots, editions):
    """Depot changes between the editions the feed carries, oldest first.

    Each edition is held against the one for the same weekday a week earlier, not just
    the previous edition of its day type: PcS covers Monday to Thursday, and a brigade
    that runs on Thursdays only would otherwise "appear" and "vanish" every week. A
    difference found that way is then dated back to the first edition since which the
    new state holds without a break, which is where the change really starts.

    Returns [{"date", "line", "brigade", "group", "old", "new"}], with old/new a depot
    label or None for a brigade that does not run.
    """
    state = {}  # sid -> {(line, brigade): depot label}
    for (sid, line, brigade), depots in slots.items():
        state.setdefault(sid, {})[(line, brigade)] = depot_label(depots)
    by_date = {(date, code): sid for sid, (date, code) in editions.items()}
    group_of = {code: key for key, _t, codes in DAY_GROUPS for code in codes}

    changes = {}
    for sid, (date, code) in editions.items():
        before = by_date.get((date - datetime.timedelta(days=7), code))
        if before is None:
            continue
        now, then = state.get(sid, {}), state.get(before, {})
        # Same-type editions in between, newest first, to date the change back through.
        between = sorted((d for (d, c) in by_date if c == code and before_date(d, date, 7)),
                         reverse=True)
        for key in set(now) | set(then):
            new, old = now.get(key), then.get(key)
            if new == old:
                continue
            since = date
            for d in between:
                if state.get(by_date[(d, code)], {}).get(key) != new:
                    break
                since = d
            line, brigade = key
            changes[(since, line, brigade, group_of[code], old, new)] = None
    return [{"date": since.isoformat(), "line": line, "brigade": brigade, "group": group,
             "old": old, "new": new}
            for since, line, brigade, group, old, new in sorted(
                changes, key=lambda c: (c[0], line_sort(c[1]), brigade_sort(c[2]) + (c[2],), c[3]))]


def before_date(d, date, days):
    """d falls in the window of `days` days that ends just before `date`, exclusive."""
    return date - datetime.timedelta(days=days) < d < date


def read_trips(feed, services):
    """One pass over trips.txt: (service_id, line, brigade) -> set of depot labels.

    Returns that alongside a count of depot_id values missing from DEPOTS, so a renamed
    or newly added operator shows up in the log instead of quietly becoming "nieznany".
    """
    bus_lines = {}
    for r in feed.rows("routes"):
        if r["route_type"] == BUS_ROUTE_TYPE and not LOCAL_LINE.match(r["route_short_name"]):
            bus_lines[r["route_id"]] = r["route_short_name"]

    pair_depots = {}
    unmapped = {}
    for t in feed.rows("trips"):
        sid = t["service_id"]
        line = bus_lines.get(t["route_id"])
        # block_short_name (the brigade) and depot_id are extensions, not GTFS: read them
        # defensively so that a feed dropping either fails the coverage guard instead of
        # the parse.
        brigade = t.get("block_short_name")
        if sid not in services or line is None or not brigade:
            continue
        depot = t.get("depot_id")
        label = None
        if depot in DEPOTS:
            label, full = DEPOTS[depot]
            DEPOT_FULL.setdefault(label, full)
        elif depot:
            unmapped[depot] = unmapped.get(depot, 0) + 1
        slot = pair_depots.setdefault((sid, line, brigade), set())
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
                label = depot_label(entry["depots"])
                out.setdefault(line, {}).setdefault(group_key, {}).setdefault(label, []).append(
                    (brigade, entry["only"])
                )
    for groups in out.values():
        for group_key, depots in groups.items():
            for brigades in depots.values():
                brigades.sort(key=lambda e: brigade_sort(e[0]) + (e[0],))
            groups[group_key] = {label: depots[label] for label in sorted(depots, key=depot_sort)}
    # Sorted so the committed brygady.json diffs line by line between rebuilds; the feed
    # lists trips in no particular order.
    return {line: out[line] for line in sorted(out, key=line_sort)}


def depot_label(depots):
    """'R-1', or 'R-1 / R-2' for a brigade split across depots, or 'nieznany'."""
    return " / ".join(sorted(depots, key=depot_sort)) or UNKNOWN


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


def format_brigades(entries):
    """Sorted, comma-separated brigade list, each one spelled out: '2, 3, 4, 9, 017'."""
    entries = sorted(entries, key=lambda e: brigade_sort(e[0]) + (e[0],))
    return ", ".join(html.escape(brigade) + (marker(only) if only else "")
                     for brigade, only in entries)


def depot_badge(label, full_name):
    # A brigade split across two depots keeps the first one's colour, so it never reads
    # as grey "nieznany".
    color = DEPOT_COLOR.get(label.split(" / ")[0], DEPOT_COLOR[UNKNOWN])
    tip = html.escape(full_name.get(label, label))
    return f'<span class="d" style="background:{color}" title="{tip}">{html.escape(label)}</span>'


def short_date(iso):
    """'2026-10-12' -> '12.10'."""
    d = datetime.date.fromisoformat(iso)
    return f"{d.day}.{d.month:02d}"


def build_changes(payload):
    """The list under the cards: '116/01 DP od 12.10: R-1 → R-2', one item per change."""
    changes = payload.get("changes")
    editions = payload.get("editions")
    if changes is None or not editions:
        return ""
    full_name = payload["depots"]
    items = []
    for c in changes:
        old = c["old"] and depot_badge(c["old"], full_name)
        new = c["new"] and depot_badge(c["new"], full_name)
        if old and new:
            what = f"{old} → {new}"
        elif new:
            what = f"nowa brygada {new}"
        else:
            what = f"likwidacja <span class=\"was\">(było {old})</span>"
        group = c["group"]
        items.append(
            f'<li><b>{html.escape(c["line"])}/{html.escape(c["brigade"])}</b> '
            f'<span class="dt" title="{html.escape(dict((k, t) for k, t, _ in DAY_GROUPS)[group])}">'
            f'{DAY_SHORT[group]}</span> od {short_date(c["date"])}: {what}</li>'
        )
    span = f"{short_date(editions[0])}–{short_date(editions[-1])}"
    body = f'<ul class="changes">{"".join(items)}</ul>' if items else \
        '<div class="sub">Brak zmian.</div>'
    return f"""<section>
    <h2 class="sec">Zmiany przydziału</h2>
    <div class="sub">Rozkłady w feedzie: {span}. Każdą edycję porównujemy z tym samym dniem
      tygodnia tydzień wcześniej; karty powyżej pokazują stan po wszystkich zmianach.</div>
    {body}
  </section>"""


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

    cards = []
    for line in sorted(lines, key=line_sort):
        url = ROUTE_URL.format(urllib.parse.quote(line.lower()))
        rows = []
        for group_key, title, _codes in DAY_GROUPS:
            tag = (f'<span class="dt" title="{html.escape(title)}">'
                   f'{html.escape(DAY_SHORT[group_key])}</span>')
            depots = lines[line].get(group_key)
            if not depots:
                rows.append(f'<div class="day">{tag}<div class="none">—</div></div>')
                continue
            blocks = []
            for label in sorted(depots, key=depot_sort):
                blocks.append(
                    f'<div class="g">{depot_badge(label, full_name)}'
                    f'<span>{format_brigades(depots[label])}</span></div>'
                )
            rows.append(f'<div class="day">{tag}<div>{"".join(blocks)}</div></div>')
        cards.append(
            f'<article class="card"><h2><a href="{html.escape(url)}" target="_blank" '
            f'rel="noopener" title="{html.escape(line)} na zbiorkom.live">{html.escape(line)}</a></h2>'
            + "".join(rows) + "</article>"
        )

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
<link rel="stylesheet" href="https://436.pl/_/436.css">
<style>
  :root {{ font-family: -apple-system, system-ui, sans-serif; }}
  body {{ margin: 24px; color: #1b1b1b; }}
  .h436 {{ margin-bottom: 14px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: #666; font-size: 13px; margin-bottom: 3px; }}
  .sub a {{ color: #06c; }}
  .tools {{ margin: 14px 0 0; }}
  #q {{ font: inherit; font-size: 13px; padding: 5px 9px; width: 220px;
    border: 1px solid #ccc; border-radius: 5px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(min(320px, 100%), 1fr));
    gap: 10px; margin-top: 12px; font-size: 13px; }}
  .card {{ border: 1px solid #e2e2e2; border-radius: 8px; padding: 7px 10px 8px;
    background: #fff; }}
  .card h2 {{ margin: 0 0 4px; font-size: 16px; font-variant-numeric: tabular-nums; }}
  .card h2 a {{ color: inherit; text-decoration: none; }}
  .card h2 a:hover {{ color: #06c; text-decoration: underline; }}
  .card h2 a:focus-visible {{ outline: 2px solid #06c; outline-offset: 2px; border-radius: 2px; }}
  .day {{ display: grid; grid-template-columns: 2.1em 1fr; align-items: start;
    padding: 3px 0; }}
  .day + .day {{ border-top: 1px dashed #eee; }}
  .dt {{ color: #999; font-size: 11px; font-weight: 600; line-height: 19px; cursor: help; }}
  .none {{ color: #bbb; line-height: 19px; }}
  .g {{ display: flex; align-items: baseline; margin: 1px 0; line-height: 1.5; }}
  .d {{ flex: none; display: inline-block; min-width: 3.4em; margin-right: 6px; padding: 0 6px;
    border-radius: 4px; font-size: 11px; font-weight: 600; text-align: center;
    border: 1px solid rgba(0,0,0,0.10); }}
  .mk {{ color: #999; font-size: 9px; margin-left: 1px; }}
  .legend {{ margin: 12px 0 0; display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 12px; }}
  .leg {{ display: inline-flex; align-items: center; gap: 5px; }}
  .sw {{ width: 13px; height: 13px; border-radius: 3px; border: 1px solid rgba(0,0,0,0.12);
    display: inline-block; }}
  .cnt {{ color: #999; }}
  .sec {{ font-size: 16px; margin: 22px 0 4px; }}
  .changes {{ margin: 8px 0 0; padding-left: 20px; font-size: 13px; line-height: 1.9; }}
  .changes .d {{ min-width: 0; margin: 0; }}
  .changes .dt {{ line-height: inherit; }}
  .was {{ color: #999; }}
  .foot {{ margin-top: 14px; color: #777; font-size: 12px; line-height: 1.6; }}
  .foot a {{ color: #06c; }}
</style></head>
<body>
  <header class="h436"><a href="https://436.pl/"><img src="https://436.pl/436.png" alt="436"
    width="480" height="289">.pl/</a><span>przydzial-brygad/</span></header>
  <h1>Przydział brygad — zakłady i przewoźnicy WTP</h1>
  <div class="sub">{len(lines)} linii autobusowych · rozkład: {html.escape(day_note)} —
    najnowsza edycja w feedzie · bez linii L</div>
  <div class="sub">źródło: <a href="https://github.com/mccartney/WarsawGTFS">mccartney/WarsawGTFS</a> ·
    feed {html.escape(payload["feedVersion"] or "?")} · zaktualizowano {payload["generated"]}</div>
  <div class="legend">{''.join(legend)}</div>
  <div class="sub"><b>DP</b> — dzień powszedni · <b>DŚ</b> — sobota / niedziela i święta</div>
  <div class="tools"><input id="q" type="search" placeholder="filtruj: numer linii lub zakład"></div>
  <div class="cards">{''.join(cards)}</div>
  {build_changes(payload)}
  <div class="foot">
    Zakład bierzemy wprost z pola <code>depot_id</code>, które feed GTFS dokłada do
    <code>trips.txt</code> — obejmuje ono zarówno zajezdnie MZA, jak i przewoźników
    kontraktowych. Jako <b>nieznany</b> wychodzą tylko kursy bez tego pola.
    Górny indeks przy numerze brygady oznacza, że kursuje ona tylko w części dni danego wiersza (DP lub DŚ).<br>
    Dane: <a href="https://ztm.waw.pl">ZTM Warszawa</a> ·
    GTFS: <a href="https://github.com/mccartney/WarsawGTFS">mccartney/WarsawGTFS</a> ·
    kształty tras: <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap (ODbL)</a>
  </div>
<script>
  const q = document.getElementById('q');
  const cards = [...document.querySelectorAll('.card, .changes li')];
  q.addEventListener('input', () => {{
    const t = q.value.trim().toLowerCase();
    for (const c of cards) c.hidden = t && !c.textContent.toLowerCase().includes(t);
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

    editions = read_editions(feed)
    days = pick_days(editions)
    print(f"feed_version: {version} · {len(editions)} editions", file=sys.stderr)
    for code, (date, sid) in sorted(days.items()):
        print(f"  {DAY_NAME[code]:<10} {sid}", file=sys.stderr)

    slots, unmapped = read_trips(feed, editions)
    pair_depots = newest_slots(slots, days)
    changes = find_changes(slots, editions)
    print(f"{len(pair_depots)} (day, line, brigade) slots · {len(changes)} changes", file=sys.stderr)
    for depot, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
        print(f"  WARNING: depot_id {depot!r} not in DEPOTS ({n} trips)", file=sys.stderr)

    payload = {
        "generated": f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}",
        "feedVersion": version,
        # In column order, not pick_days' order: that walks a set, which Python's string
        # hashing reshuffles on every run.
        "services": {code: {"date": days[code][0].isoformat()}
                     for _k, _t, codes in DAY_GROUPS for code in codes if code in days},
        "depots": dict(sorted(DEPOT_FULL.items())),
        "lines": group_rows(pair_depots, days),
        # Dated depot changes across the editions, for the list under the cards; the
        # cards themselves show the newest edition, i.e. the state after all of these.
        "editions": sorted({date.isoformat() for date, _code in editions.values()}),
        "changes": changes,
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
