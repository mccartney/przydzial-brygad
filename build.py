#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grzegorz Olędzki
"""Render a one-page table of "which depot runs which brigade" for every WTP bus line.

Reads the Warsaw GTFS at https://436.pl/gtfs/warsaw.zip, built by
https://github.com/mccartney/WarsawGTFS, and, for each day from today to the feed's newest
timetable edition, reports the operating depot of every (line, brigade) pair. Consecutive days with the same plan
across the whole network share one entry in the page's day selector.

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
import zoneinfo
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
# The contracted operators get a code per depot (Mob12/Mob88, Relobus29/38/49); those
# collapse to one label each, since the table answers who runs a brigade, not from which
# of an operator's yards. Several lines mix an operator's depots.
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
    "Relobus49": ("Relobus", "Relobus"),
    "KMŁ K-26": ("KMŁ", "Komunikacja Miejska Łomianki"),
}

TZ = zoneinfo.ZoneInfo("Europe/Warsaw")

# The day groups the list of changes files each change under.
DAY_GROUPS = [
    ("powszedni", "Dzień powszedni", ("PcS", "PtS")),
    ("swiateczny", "Sobota / niedziela i święta", ("SbS", "NdS")),
]
DAY_SHORT = {"powszedni": "DP", "swiateczny": "DŚ"}

WEEKDAY_SHORT = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]
MONTH_GENITIVE = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca",
                  "sierpnia", "września", "października", "listopada", "grudnia"]
# The ZTM day type each weekday normally runs. A date on another one is a holiday (NdS on
# a weekday) or similar, and its selector entry says which timetable it runs.
USUAL_CODE = ["PcS", "PcS", "PcS", "PcS", "PtS", "SbS", "NdS"]
CODE_NAME = {"PcS": "powszedni", "PtS": "piątkowy", "SbS": "sobotni", "NdS": "świąteczny"}

ROUTE_URL = "https://zbiorkom.live/warsaw/route/{}/brigades"

# Vehicle class from fleet_type, another trips.txt extension: "G-np18m" -> "G". Only the
# bus classes; tram and rail types never reach the bus table anyway.
VEHICLE_CLASS = re.compile(r"^([KMLGH])-")
# Smallest first, the order a mixed line lists its classes in.
VEHICLE_NAME = {
    "K": "mini 8–10 m",
    "M": "12 m",
    "L": "12 m elektryczny",
    "G": "przegubowy 18 m",
    "H": "przegubowy elektryczny",
}

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


def read_calendar(feed, editions, start):
    """The bus edition running on each date from `start`: {date: service_id}.

    Stops at the newest edition's date. The feed spells out its first week or so, one
    edition per day, and then repeats those editions for weeks after; those later dates
    are a guess that misses any timetable change ZTM has not published yet, so they are
    left out. The feed has one bus service per date; should a date ever get two, the newer
    edition wins.
    """
    end = max(date for date, _code in editions.values()) + datetime.timedelta(days=1)
    calendar = {}
    for r in feed.rows("calendar_dates"):
        sid = r["service_id"]
        if sid not in editions or r["exception_type"] != "1":
            continue
        d = r["date"]
        date = datetime.date(int(d[:4]), int(d[4:6]), int(d[6:]))
        if start <= date < end and (date not in calendar or
                                    editions[sid][0] > editions[calendar[date]][0]):
            calendar[date] = sid
    return dict(sorted(calendar.items()))


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

    Returns that alongside the same slots' vehicle class letter (left out where fleet_type
    is missing) and a count of depot_id values missing from DEPOTS, so a renamed or newly
    added operator shows up in the log instead of quietly becoming "nieznany".
    """
    bus_lines = {}
    for r in feed.rows("routes"):
        if r["route_type"] == BUS_ROUTE_TYPE and not LOCAL_LINE.match(r["route_short_name"]):
            bus_lines[r["route_id"]] = r["route_short_name"]

    pair_depots = {}
    fleet = {}
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
        # A brigade runs one vehicle all day, so every trip of it carries the same class.
        cls = VEHICLE_CLASS.match(t.get("fleet_type") or "")
        if cls:
            fleet[(sid, line, brigade)] = cls[1]
    return pair_depots, fleet, unmapped


def day_plans(slots, fleet, calendar, editions):
    """Number the network-wide plans of the calendar's days and lay out each line's card.

    A plan is the whole network's {line: {brigade: (depot label, class)}}; two editions
    with identical plans (the feed often repeats a weekday's) share a number, so equal
    consecutive days can be merged into one selector entry.

    Returns (days, lines): days is [{"date", "code", "plan"}] in date order; lines is
    {line: [{"plans": [plan numbers], "depots": {depot label: [(brigade, class), ...]}}]},
    one entry per distinct plan of the line. A plan the line is missing from is a day it
    does not run.
    """
    by_sid = {}  # sid -> line -> {brigade: (depot label, class)}
    for (sid, line, brigade), depots in slots.items():
        by_sid.setdefault(sid, {}).setdefault(line, {})[brigade] = (
            depot_label(depots), fleet.get((sid, line, brigade)))

    def key(plan):
        return repr(sorted((k, sorted(v.items())) for k, v in plan.items()))

    numbers, plans, days = {}, [], []
    for date, sid in calendar.items():
        plan = by_sid.get(sid, {})
        n = numbers.setdefault(key(plan), len(numbers))
        if n == len(plans):
            plans.append(plan)
        days.append({"date": date.isoformat(), "code": editions[sid][1], "plan": n})

    out = {}
    for n, plan in enumerate(plans):
        for line, brigades in plan.items():
            variants = out.setdefault(line, {})
            variants.setdefault(key({"": brigades}), (brigades, []))[1].append(n)
    lines = {}
    # Sorted so the committed brygady.json diffs line by line between rebuilds; the feed
    # lists trips in no particular order.
    for line in sorted(out, key=line_sort):
        lines[line] = []
        for brigades, numbers_ in out[line].values():
            depots = {}
            for brigade, (label, cls) in brigades.items():
                depots.setdefault(label, []).append((brigade, cls))
            for entries in depots.values():
                entries.sort(key=lambda e: brigade_sort(e[0]) + (e[0],))
            lines[line].append({"plans": numbers_,
                                "depots": {label: depots[label] for label in sorted(depots, key=depot_sort)}})
    return days, lines


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


def vehicle_tag(cls):
    return f'<span class="vt" title="{html.escape(VEHICLE_NAME[cls])}">{cls}</span>'


def entry_class(entry):
    """Vehicle class of a brigade entry: (brigade, class_or_None)."""
    return entry[1]


def format_brigades(entries, by_class=False):
    """Sorted, comma-separated brigade list, each one spelled out: '2, 3, 4, 9, 017'.

    With by_class, split into runs per vehicle class, each led by its letter:
    'M 2, 3, 9 G 4, 017'."""
    entries = sorted(entries, key=lambda e: brigade_sort(e[0]) + (e[0],))
    runs = {}
    for e in entries:
        runs.setdefault(entry_class(e) if by_class else None, []).append(e)
    out = []
    for cls in sorted(runs, key=lambda c: list(VEHICLE_NAME).index(c) if c in VEHICLE_NAME else len(VEHICLE_NAME)):
        text = ", ".join(html.escape(e[0]) for e in runs[cls])
        out.append(f"{vehicle_tag(cls)} {text}" if cls else text)
    return " ".join(out)


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
    """The list under the cards: '116/01, 03 DP od 12.10: R-1 → R-2', one item per line,
    day group, date and kind of change, with all the brigades it hits."""
    changes = payload.get("changes")
    editions = payload.get("editions")
    if changes is None or not editions:
        return ""
    full_name = payload["depots"]
    # Changes come sorted by date, line, brigade, so each group's brigades come out sorted.
    grouped = {}
    for c in changes:
        grouped.setdefault((c["date"], c["line"], c["group"], c["old"], c["new"]), []).append(c["brigade"])
    group_order = [k for k, _t, _codes in DAY_GROUPS]
    rows = sorted(grouped.items(), key=lambda kv: (
        line_sort(kv[0][1]), kv[0][0], group_order.index(kv[0][2]), brigade_sort(kv[1][0]) + (kv[1][0],)))
    items = []
    for (date, line, group, old, new), brigades in rows:
        old = old and depot_badge(old, full_name)
        new = new and depot_badge(new, full_name)
        if old and new:
            what = f"{old} → {new}"
        elif new:
            what = f"nowa brygada {new}"
        else:
            what = f"likwidacja <span class=\"was\">(było {old})</span>"
        items.append(
            f'<li><b>{html.escape(line)}/{html.escape(", ".join(brigades))}</b> '
            f'<span class="dt" title="{html.escape(dict((k, t) for k, t, _ in DAY_GROUPS)[group])}">'
            f'{DAY_SHORT[group]}</span> od {short_date(date)}: {what}</li>'
        )
    span = f"{short_date(editions[0])}–{short_date(editions[-1])}"
    body = f'<ul class="changes">{"".join(items)}</ul>' if items else \
        '<div class="sub">Brak zmian.</div>'
    return f"""<section>
    <h2 class="sec">Zmiany przydziału</h2>
    <div class="sub">Rozkłady w feedzie: {span}. Każdą edycję porównujemy z tym samym dniem
      tygodnia tydzień wcześniej.</div>
    {body}
  </section>"""


def runs(days):
    """Consecutive days on the same plan: [(first, last, plan, [days])]."""
    out = []
    for day in days:
        date = datetime.date.fromisoformat(day["date"])
        if out and out[-1][2] == day["plan"] and out[-1][1] + datetime.timedelta(days=1) == date:
            first, _last, plan, members = out[-1]
            out[-1] = (first, date, plan, members + [day])
        else:
            out.append((date, date, day["plan"], [day]))
    return out


def run_label(first, last, members):
    """'sob 31 października', 'wt–czw 3–5 listopada', 'pon–śr 30 listopada – 2 grudnia',
    plus the timetable a holiday runs on."""
    weekdays = WEEKDAY_SHORT[first.weekday()]
    if last != first:
        weekdays += "–" + WEEKDAY_SHORT[last.weekday()]
    if first == last:
        dates = f"{first.day} {MONTH_GENITIVE[first.month - 1]}"
    elif first.month == last.month:
        dates = f"{first.day}–{last.day} {MONTH_GENITIVE[first.month - 1]}"
    else:
        dates = (f"{first.day} {MONTH_GENITIVE[first.month - 1]} – "
                 f"{last.day} {MONTH_GENITIVE[last.month - 1]}")
    label = f"<b>{weekdays}</b> {dates}"
    unusual = [d["code"] for d in members
               if d["code"] != USUAL_CODE[datetime.date.fromisoformat(d["date"]).weekday()]]
    if unusual:
        label += f' <span class="hol">rozkład {CODE_NAME[unusual[0]]}</span>'
    return label


def build_html(payload):
    lines = payload["lines"]
    days = payload["days"]
    full_name = payload["depots"]
    day_runs = runs(days)
    n_plans = max(d["plan"] for d in days) + 1
    # Rendered for the first day; the script switches to today's on load.
    shown = day_runs[0][2]

    counts = {}  # depot -> [brigades in plan 0, plan 1, ...]
    for variants in lines.values():
        for v in variants:
            for label, brigades in v["depots"].items():
                for part in label.split(" / "):
                    per_plan = counts.setdefault(part, [0] * n_plans)
                    for n in v["plans"]:
                        per_plan[n] += len(brigades)

    def plans_attr(numbers):
        hidden = "" if shown in numbers else " hidden"
        return f'data-p=" {" ".join(map(str, numbers))} "{hidden}'

    cards = []
    for line in sorted(lines, key=line_sort):
        url = ROUTE_URL.format(urllib.parse.quote(line.lower()))
        variants = lines[line]
        # One class for the whole line goes next to its number; otherwise every brigade
        # list is split by class.
        classes = {entry_class(e) for v in variants
                   for brigades in v["depots"].values() for e in brigades}
        by_class = len(classes) > 1
        sole = None if by_class else next(iter(classes), None)
        line_tag = vehicle_tag(sole) if sole else ""
        rows = []
        for v in variants:
            blocks = "".join(
                f'<div class="g">{depot_badge(label, full_name)}'
                f'<span>{format_brigades(v["depots"][label], by_class)}</span></div>'
                for label in sorted(v["depots"], key=depot_sort))
            rows.append(f'<div class="v" {plans_attr(v["plans"])}>{blocks}</div>')
        idle = [n for n in range(n_plans) if not any(n in v["plans"] for v in variants)]
        if idle:
            rows.append(f'<div class="v none" {plans_attr(idle)}>nie kursuje</div>')
        cards.append(
            f'<article class="card"><h2><a href="{html.escape(url)}" target="_blank" '
            f'rel="noopener" title="{html.escape(line)} na zbiorkom.live">{html.escape(line)}</a>{line_tag}</h2>'
            + "".join(rows) + "</article>"
        )

    legend = []
    for label in DEPOT_ORDER:
        if label not in counts:
            continue
        full = full_name.get(label, label)
        n = counts[label]
        legend.append(
            f'<span class="leg" data-n="{" ".join(map(str, n))}"{"" if n[shown] else " hidden"}>'
            f'<span class="sw" style="background:{DEPOT_COLOR[label]}"></span>'
            f'{html.escape(full)} <span class="cnt">{n[shown]}</span></span>'
        )

    selector = []
    for i, (first, last, plan, members) in enumerate(day_runs):
        selector.append(
            f'<button type="button" data-p="{plan}" data-from="{first.isoformat()}" '
            f'data-to="{last.isoformat()}" aria-pressed="{"true" if i == 0 else "false"}">'
            f'{run_label(first, last, members)}</button>')

    vehicle_note = " · ".join(f"<b>{c}</b> — {html.escape(n)}" for c, n in VEHICLE_NAME.items())
    span = f"{short_date(days[0]['date'])}–{short_date(days[-1]['date'])}"

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
  .days {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 14px 0 0; }}
  .days button {{ font: inherit; font-size: 13px; padding: 4px 10px; cursor: pointer;
    border: 1px solid #ccc; border-radius: 5px; background: #fff; color: inherit; }}
  .days button:hover {{ border-color: #06c; }}
  .days button[aria-pressed="true"] {{ background: #06c; border-color: #06c; color: #fff; }}
  .days button.today::after {{ content: "dziś"; margin-left: 6px; font-size: 10px;
    font-weight: 600; text-transform: uppercase; opacity: .7; }}
  .hol {{ font-size: 11px; opacity: .75; }}
  .v {{ padding: 2px 0; }}
  .dt {{ color: #999; font-size: 11px; font-weight: 600; cursor: help; white-space: nowrap; }}
  .none {{ color: #bbb; line-height: 19px; }}
  [hidden] {{ display: none !important; }}
  .g {{ display: flex; align-items: baseline; margin: 1px 0; line-height: 1.5; }}
  .d {{ flex: none; display: inline-block; min-width: 3.4em; margin-right: 6px; padding: 0 6px;
    border-radius: 4px; font-size: 11px; font-weight: 600; text-align: center;
    border: 1px solid rgba(0,0,0,0.10); }}
  .vt {{ display: inline-block; padding: 0 3px; border: 1px solid #ccc; border-radius: 3px;
    color: #777; font-size: 10px; font-weight: 600; line-height: 1.3; cursor: help; }}
  .card h2 .vt {{ margin-left: 6px; vertical-align: 2px; }}
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
  <div class="sub">{len(lines)} linii autobusowych · rozkłady na dni {span} · bez linii L</div>
  <div class="sub">źródło: <a href="https://github.com/mccartney/WarsawGTFS">mccartney/WarsawGTFS</a> ·
    feed {html.escape(payload["feedVersion"] or "?")} · zaktualizowano {payload["generated"]}</div>
  <div class="sub">Uwagi? Można zgłaszać na <a href="https://wawkom.waw.pl/viewtopic.php?t=11090">forum wawkom</a>
    albo <a href="https://github.com/mccartney/przydzial-brygad/issues">w GitHubie</a>.</div>
  <div class="legend">{''.join(legend)}</div>
  <div class="sub">{vehicle_note}</div>
  <nav class="days" aria-label="Dzień">{''.join(selector)}</nav>
  <div class="tools"><input id="q" type="search" placeholder="filtruj: numer linii lub zakład"></div>
  <div class="cards">{''.join(cards)}</div>
  {build_changes(payload)}
  <div class="foot">
    Zakład bierzemy wprost z pola <code>depot_id</code>, które feed GTFS dokłada do
    <code>trips.txt</code> — obejmuje ono zarówno zajezdnie MZA, jak i przewoźników
    kontraktowych. Jako <b>nieznany</b> wychodzą tylko kursy bez tego pola.
    Typ taboru (litera przy numerze linii lub przed brygadami) pochodzi z pola <code>fleet_type</code>.
    Kolejne dni z identycznym przydziałem w całej sieci są połączone w jeden przycisk;
    domyślnie wybrany jest dzisiejszy dzień.<br>
    Dane: <a href="https://ztm.waw.pl">ZTM Warszawa</a> ·
    GTFS: <a href="https://github.com/mccartney/WarsawGTFS">mccartney/WarsawGTFS</a> ·
    kształty tras: <a href="https://www.openstreetmap.org/copyright">© OpenStreetMap (ODbL)</a>
  </div>
<script>
  const q = document.getElementById('q');
  const cards = [...document.querySelectorAll('.card')];
  const changes = [...document.querySelectorAll('.changes li')];
  const buttons = [...document.querySelectorAll('.days button')];
  const legend = [...document.querySelectorAll('.leg')];
  let plan = buttons[0].dataset.p;
  const filter = () => {{
    const t = q.value.trim().toLowerCase();
    // Match a card on its number and the selected day's row only.
    for (const c of cards) c.hidden = t && ![c.querySelector('h2'), ...c.querySelectorAll('.v')]
      .some(e => !e.hidden && e.textContent.toLowerCase().includes(t));
    for (const c of changes) c.hidden = t && !c.textContent.toLowerCase().includes(t);
  }};
  const select = b => {{
    plan = b.dataset.p;
    for (const o of buttons) o.setAttribute('aria-pressed', o === b);
    for (const v of document.querySelectorAll('.v')) v.hidden = !v.dataset.p.includes(` ${{plan}} `);
    for (const l of legend) {{
      const n = l.dataset.n.split(' ')[plan];
      l.querySelector('.cnt').textContent = n;
      l.hidden = n === '0';
    }}
    filter();
  }};
  // Today in Warsaw as YYYY-MM-DD. Days already past are dropped, unless the page is so
  // stale that nothing else is left.
  const today = new Date().toLocaleDateString('sv-SE', {{timeZone: 'Europe/Warsaw'}});
  for (const b of buttons) {{
    b.hidden = b.dataset.to < today;
    b.classList.toggle('today', b.dataset.from <= today && today <= b.dataset.to);
    b.addEventListener('click', () => select(b));
  }}
  const live = buttons.filter(b => !b.hidden);
  if (!live.length) buttons.at(-1).hidden = false;
  select(live[0] || buttons.at(-1));
  q.addEventListener('input', filter);
</script>
</body></html>
"""


def coverage(payload):
    """Share of (line, plan variant, brigade) slots that got a real depot, not 'nieznany'."""
    total = known = 0
    for variants in payload["lines"].values():
        for v in variants:
            for label, brigades in v["depots"].items():
                total += len(brigades)
                if label != UNKNOWN:
                    known += len(brigades)
    return known / total if total else 0.0


def usable(payload):
    return (payload and payload.get("days") and len(payload.get("lines", {})) >= MIN_LINES
            and coverage(payload) >= MIN_COVERAGE)


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
    today = datetime.datetime.now(TZ).date()
    calendar = read_calendar(feed, editions, today)
    print(f"feed_version: {version} · {len(editions)} editions", file=sys.stderr)

    slots, fleet, unmapped = read_trips(feed, editions)
    days, lines = day_plans(slots, fleet, calendar, editions)
    for day in days:
        print(f"  {day['date']} {calendar[datetime.date.fromisoformat(day['date'])]} "
              f"plan {day['plan']}", file=sys.stderr)
    changes = find_changes(slots, editions)
    print(f"{len(slots)} (edition, line, brigade) slots · {len(changes)} changes", file=sys.stderr)
    for depot, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
        print(f"  WARNING: depot_id {depot!r} not in DEPOTS ({n} trips)", file=sys.stderr)

    payload = {
        "generated": f"{datetime.datetime.now(datetime.timezone.utc):%Y-%m-%d}",
        "feedVersion": version,
        "depots": dict(sorted(DEPOT_FULL.items())),
        # Today to the newest edition, each day pointing at its network-wide plan by number.
        "days": days,
        "lines": lines,
        # Dated depot changes across the editions, for the list under the cards.
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
