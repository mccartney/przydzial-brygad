#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Grzegorz Olędzki
"""Render a one-page table of "which depot runs which brigade" for every WTP bus and tram line.

Reads the Warsaw GTFS at https://436.pl/gtfs/warsaw.zip, built by
https://github.com/mccartney/WarsawGTFS, and, for each day from today to the feed's newest
timetable edition, reports the operating depot of every (line, brigade) pair. Consecutive days with the same plan
across the whole network share one entry in the page's day selector.

The depot comes straight from `depot_id`, a non-standard column the feed adds to
trips.txt. It names MZA's depots by an internal code — R-7(W) is Woronicza, R11(K)
Kleszczowa — and the contracted operators (Mobilis, PKS Grodzisk, ReloBus, KMŁ) outright,
so it covers the whole network, not just the part that models pull-out/pull-in runs.
Tramwaje Warszawskie's depots come the same way (R1(W) is Wola, ... R5(A) Annopol).

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
TRAM_ROUTE_TYPE = "0"
# Local "L" lines are left out of the table. This feed does name their commune
# operators (depot_id G-14, G-17, G-30, G-42), so they could be added.
LOCAL_LINE = re.compile(r"^L-?\d+$")
# depot_id -> (label used in the table, full name for the legend). MZA's codes
# are internal — the parenthesised letter is the site's initial — so they are relabelled
# to the R-n numbering ZTM and MZA use in public. Verified against the depot stops the
# trips of each code actually terminate at.
#
# The contracted operators get a code per depot (Mob12/Mob88, Relobus29/38/49/58); those
# collapse to one label each, since the table answers who runs a brigade, not from which
# of an operator's yards. Several lines mix an operator's depots.
DEPOTS = {
    "R-7(W)": ("R-1", "R-1 Woronicza (dawne R-7)"),
    "R11(K)": ("R-2", "R-2 Kleszczowa (dawne R-11)"),
    "R10(O)": ("R-3", "R-3 Ostrobramska (dawne R-10)"),
    "R13(S)": ("R-4", "R-4 Stalowa (dawne R-13)"),
    "R14(P)": ("R-6", "R-6 Płochocińska (dawne R-14)"),
    "Mob12": ("Mobilis", "Mobilis"),
    "Mob88": ("Mobilis", "Mobilis"),
    "PKS_A82": ("PKS Grodzisk", "PKS Grodzisk Mazowiecki"),
    "Relobus29": ("Relobus", "Relobus"),
    "Relobus38": ("Relobus", "Relobus"),
    "Relobus49": ("Relobus", "Relobus"),
    "Relobus58": ("Relobus", "Relobus"),
    "KMŁ K-26": ("KMŁ", "Komunikacja Miejska Łomianki"),
    # Tramwaje Warszawskie. Their R-n numbering clashes with MZA's, so the table names
    # them by site. Verified against the "Zjazd do zajezdni …" headsigns of each code.
    "R1(W)": ("Wola", "R-1 Wola (tramwaje)"),
    "R2(P)": ("Praga", "R-2 Praga (tramwaje)"),
    "R3(M)": ("Mokotów", "R-3 Mokotów (tramwaje)"),
    "R4(Z)": ("Żoliborz", "R-4 Żoliborz (tramwaje)"),
    "R5(A)": ("Annopol", "R-5 Annopol (tramwaje)"),
    # A few brigades (line 91) carry the company code but no depot.
    "TW": ("TW", "Tramwaje Warszawskie (bez wskazanej zajezdni)"),
}
TRAM_DEPOTS = {"Wola", "Praga", "Mokotów", "Żoliborz", "Annopol", "TW"}

# Operators that run several contracts, one depot_id per contract. The table files them
# under the operator, as above; picking the operator in the legend splits its rows per
# contract. Labelled by the code's number, since the feed names nothing else. Colours
# are shades of the operator's own.
CONTRACTS = {
    "Mob12": ("Mobilis 12", "#ffe3b0"),
    "Mob88": ("Mobilis 88", "#f6c879"),
    "Relobus29": ("Relobus 29", "#e8f4d3"),
    "Relobus38": ("Relobus 38", "#d2e8ae"),
    "Relobus49": ("Relobus 49", "#b7d98a"),
    "Relobus58": ("Relobus 58", "#9cc768"),
}
CONTRACT_COLOR = dict(CONTRACTS.values())

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
SONG_URL = "https://www.youtube.com/watch?v=QTp036qRSBw"

# Vehicle class from fleet_type, another trips.txt extension: "G-np18m" -> "G" for buses.
# Trams keep fleet_type as is ("120N", "DUO", "2 wagony").
VEHICLE_CLASS = re.compile(r"^([KMLGH])-")
# Smallest first, the order a mixed line lists its classes in.
VEHICLE_NAME = {
    "K": "midi, 8–10 m",
    "M": "maxi, 12 m",
    "L": "elektryczny maxi, 12 m",
    "G": "przegubowy mega, 18 m",
    "H": "elektryczny przegubowy mega, 18 m",
}
# Tram fleet_type -> (tag text, tooltip). The feed's own names, bar the one with a space.
# A type missing here is shown as it comes.
TRAM_FLEET = {
    "2 wagony": ("2w", "skład dwóch wagonów"),
    "DUO": ("DUO", "DUO"),
    "116N/142N": ("116N/142N", "116N/142N"),
    "120N": ("120N", "120N"),
    "134N": ("134N", "134N"),
}
VEHICLE_ORDER = list(VEHICLE_NAME) + list(TRAM_FLEET)

UNKNOWN = "nieznany"
DEPOT_ORDER = ["R-1", "R-2", "R-3", "R-4", "R-5", "R-6",
               "Mobilis", "PKS Grodzisk", "Relobus", "KMŁ",
               "Wola", "Praga", "Mokotów", "Żoliborz", "Annopol", "TW", UNKNOWN]
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
    "Wola": "#ffcdc7",
    "Praga": "#e3d5b8",
    "Mokotów": "#c6e3f7",
    "Żoliborz": "#f5eaa0",
    "Annopol": "#d6d6a8",
    "TW": "#d8dde3",
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


def find_changes(slots, editions, shared):
    """Depot changes between the editions the feed carries, oldest first.

    Each edition is held against the one for the same weekday a week earlier, not just
    the previous edition of its day type: PcS covers Monday to Thursday, and a brigade
    that runs on Thursdays only would otherwise "appear" and "vanish" every week. A
    difference found that way is then dated back to the first edition since which the
    new state holds without a break, which is where the change really starts.

    A brigade that one vehicle runs on several lines (the same block_id in trips.txt, see
    `shared`) changes on all of them at once; that comes out as one change with the lines
    joined, "411+502", rather than one per line, so it is not counted twice.

    Returns [{"date", "line", "brigade", "group", "old", "new"}], with old/new a depot
    label or None for a brigade that does not run.
    """
    state = {}  # sid -> {(line, brigade): depot label}
    for (sid, line, brigade), depots in slots.items():
        state.setdefault(sid, {})[(line, brigade)] = depot_label(depots)
    by_date = {(date, code): sid for sid, (date, code) in editions.items()}
    group_of = {code: key for key, _t, codes in DAY_GROUPS for code in codes}

    changes = {}  # (since, brigade, group, old, new) -> {changed line: lines it shares with}
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
            # The lines it shares the vehicle with, in the edition where it runs.
            partners = shared.get(sid if new else before, {}).get(key, {line})
            changes.setdefault((since, brigade, group_of[code], old, new), {}).setdefault(
                line, set()).update(partners)

    out = []
    for (since, brigade, group, old, new), lines in changes.items():
        # Lines that changed alike and share the brigade's vehicle make one change.
        merged = Merge()
        for line, partners in lines.items():
            for other in partners & lines.keys():
                merged.union(line, other)
        for joined in merged.groups(lines):
            out.append({"date": since.isoformat(), "line": "+".join(sorted(joined, key=line_sort)),
                        "brigade": brigade, "group": group, "old": old, "new": new})
    out.sort(key=lambda c: (c["date"], line_sort(c["line"]), brigade_sort(c["brigade"]) + (c["brigade"],),
                            c["group"], c["old"] or "", c["new"] or ""))
    return out


class Merge:
    """Union-find: items joined into groups by pairs."""

    def __init__(self):
        self.parent = {}

    def find(self, x):
        while self.parent.setdefault(x, x) != x:
            self.parent[x] = x = self.parent[self.parent[x]]
        return x

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)

    def groups(self, items):
        out = {}
        for x in items:
            out.setdefault(self.find(x), set()).add(x)
        return list(out.values())


def shared_lines(blocks):
    """{service_id: {(line, brigade): set of lines}}, the lines each brigade's vehicle runs.

    `blocks` is {service_id: {block_id: {(line, brigade), ...}}}. A block is one vehicle's
    day, and ZTM keeps one brigade number across all the lines it runs, so a block holding
    several lines is a brigade shared between them. Only multi-line brigades are listed.
    """
    out = {}
    for sid, by_block in blocks.items():
        merged = Merge()
        for pairs in by_block.values():
            first, *rest = pairs
            for pair in rest:
                merged.union(first, pair)
        for group in merged.groups({p for pairs in by_block.values() for p in pairs}):
            if len(group) > 1:
                lines = {line for line, _brigade in group}
                for pair in group:
                    out.setdefault(sid, {})[pair] = lines
    return out


def before_date(d, date, days):
    """d falls in the window of `days` days that ends just before `date`, exclusive."""
    return date - datetime.timedelta(days=days) < d < date


def read_trips(feed, services):
    """One pass over trips.txt: (service_id, line, brigade) -> set of depot labels.

    Covers bus and tram lines. Returns that alongside the same slots' vehicle class (bus
    class letter or tram fleet_type, left out where fleet_type is missing), their contracts
    (set of CONTRACTS labels, only for the operators listed there), a count of depot_id
    values missing from DEPOTS, so a renamed or newly added operator shows up in the log
    instead of quietly becoming "nieznany", the set of tram lines, and the slots of every
    block_id ({service_id: {block_id: {(line, brigade), ...}}}).
    """
    lines = {}  # route_id -> line
    trams = set()
    for r in feed.rows("routes"):
        name = r["route_short_name"]
        if r["route_type"] == BUS_ROUTE_TYPE and not LOCAL_LINE.match(name):
            lines[r["route_id"]] = name
        elif r["route_type"] == TRAM_ROUTE_TYPE:
            lines[r["route_id"]] = name
            trams.add(name)

    pair_depots = {}
    fleet = {}
    contracts = {}
    unmapped = {}
    blocks = {}
    for t in feed.rows("trips"):
        sid = t["service_id"]
        line = lines.get(t["route_id"])
        # block_short_name (the brigade) and depot_id are extensions, not GTFS: read them
        # defensively so that a feed dropping either fails the coverage guard instead of
        # the parse.
        brigade = t.get("block_short_name")
        if sid not in services or line is None or not brigade:
            continue
        if t.get("block_id"):
            blocks.setdefault(sid, {}).setdefault(t["block_id"], set()).add((line, brigade))
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
        if depot in CONTRACTS:
            contracts.setdefault((sid, line, brigade), set()).add(CONTRACTS[depot][0])
        # A brigade runs one vehicle all day, so every trip of it carries the same class.
        fleet_type = t.get("fleet_type") or ""
        if line not in trams:
            cls = VEHICLE_CLASS.match(fleet_type)
            fleet_type = cls[1] if cls else ""
        if fleet_type:
            fleet[(sid, line, brigade)] = fleet_type
    return pair_depots, fleet, contracts, unmapped, trams, blocks


def day_plans(slots, fleet, contracts, calendar, editions):
    """Number the network-wide plans of the calendar's days and lay out each line's card.

    A plan is the whole network's {line: {brigade: (depot label, class, contract)}}; two
    editions with identical plans (the feed often repeats a weekday's) share a number, so
    equal consecutive days can be merged into one selector entry. The contract counts too:
    days that differ only in it must not share the card rows that spell it out.

    Returns (days, lines): days is [{"date", "code", "plan"}] in date order; lines is
    {line: [{"plans": [plan numbers],
             "depots": {depot label: [(brigade, class, contract), ...]}}]},
    one entry per distinct plan of the line. A plan the line is missing from is a day it
    does not run.
    """
    by_sid = {}  # sid -> line -> {brigade: (depot label, class)}
    for (sid, line, brigade), depots in slots.items():
        contract = " / ".join(sorted(contracts.get((sid, line, brigade), ()))) or None
        by_sid.setdefault(sid, {}).setdefault(line, {})[brigade] = (
            depot_label(depots), fleet.get((sid, line, brigade)), contract)

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
            for brigade, (label, cls, contract) in brigades.items():
                depots.setdefault(label, []).append((brigade, cls, contract))
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
    # A change on a shared brigade names several lines, "411+502": it goes with its first.
    if "+" in line:
        return line_sort(line.split("+")[0])[:-1] + (line,)
    if line.isdigit():
        return (0, "", int(line), line)
    m = re.match(r"^([A-Za-z]+)-?(\d+)$", line)
    if m:
        return (1, m.group(1), int(m.group(2)), line)
    return (2, line, 0, line)


def vehicle_tag(cls):
    tag, tip = (cls, VEHICLE_NAME[cls]) if cls in VEHICLE_NAME else TRAM_FLEET.get(cls, (cls, cls))
    return f'<span class="vt" title="{html.escape(tip)}">{html.escape(tag)}</span>'


def entry_class(entry):
    """Vehicle class of a brigade entry: (brigade, class_or_None, contract_or_None)."""
    return entry[1]


def entry_contract(entry):
    # brygady.json files from before contracts were tracked have two-element entries.
    return entry[2] if len(entry) > 2 else None


def depot_mode(label):
    """'tram' for a Tramwaje Warszawskie depot, 'bus' for the rest, '' for 'nieznany'."""
    return "" if label == UNKNOWN else "tram" if label in TRAM_DEPOTS else "bus"


def line_mode(line, trams):
    return "tram" if line.split("+")[0] in trams else "bus"


def operators_attr(label):
    """data-o for an element: '|R-2|Relobus|'. Pipes, as labels contain spaces."""
    return html.escape("|" + "|".join(label.split(" / ")) + "|")


def contract_label(label, contract):
    """The depot label with each operator spelled out per contract:
    ('Relobus', 'Relobus 38') -> 'Relobus 38', ('R-2 / Relobus', ...) -> 'R-2 / Relobus 38'."""
    names = contract.split(" / ")
    return " / ".join(" / ".join(n for n in names if n.startswith(part + " ")) or part
                      for part in label.split(" / "))


def format_brigades(entries, by_class=False):
    """Sorted, comma-separated brigade list, each one spelled out: '2, 3, 4, 9, 017'.

    With by_class, split into runs per vehicle class, each led by its letter:
    'M 2, 3, 9 G 4, 017'."""
    entries = sorted(entries, key=lambda e: brigade_sort(e[0]) + (e[0],))
    runs = {}
    for e in entries:
        runs.setdefault(entry_class(e) if by_class else None, []).append(e)
    out = []
    for cls in sorted(runs, key=lambda c: (VEHICLE_ORDER.index(c), "") if c in VEHICLE_ORDER
                      else (len(VEHICLE_ORDER), c or "")):
        text = ", ".join(html.escape(e[0]) for e in runs[cls])
        out.append(f"{vehicle_tag(cls)} {text}" if cls else text)
    return " ".join(out)


def depot_badge(label, full_name, color=None):
    # A brigade split across two depots keeps the first one's colour, so it never reads
    # as grey "nieznany".
    color = color or DEPOT_COLOR.get(label.split(" / ")[0], DEPOT_COLOR[UNKNOWN])
    tip = html.escape(full_name.get(label, label))
    return f'<span class="d" style="background:{color}" title="{tip}">{html.escape(label)}</span>'


def short_date(iso):
    """'2026-10-12' -> '12.10'."""
    d = datetime.date.fromisoformat(iso)
    return f"{d.day}.{d.month:02d}"


def split_renumberings(changes):
    """Takes out a brigade that goes and one that comes in its place under the same number
    spelled with a leading zero, "6" and "06" (same date, line and day group).

    Returns the remaining changes and {(date, line, group, old, new): [("6", "06"), ...]},
    old being the gone brigade's depot and new the new one's."""
    def number(brigade):
        return brigade.lstrip("0") or "0"
    gone, came = {}, {}
    for i, c in enumerate(changes):
        if bool(c["old"]) != bool(c["new"]):
            side = gone if c["old"] else came
            side.setdefault((c["date"], c["line"], c["group"], number(c["brigade"])), []).append(i)
    paired = set()
    renumbered = {}
    for key, olds in gone.items():
        news = list(came.get(key, ()))
        for i in olds:
            j = next((j for j in news if changes[j]["brigade"] != changes[i]["brigade"]), None)
            if j is None:
                continue
            news.remove(j)
            paired.update((i, j))
            a, b = changes[i], changes[j]
            renumbered.setdefault((a["date"], a["line"], a["group"], a["old"], b["new"]), []).append(
                (a["brigade"], b["brigade"]))
    return [c for i, c in enumerate(changes) if i not in paired], renumbered


def build_changes(payload):
    """The list under the cards, in two tabs over the same changes.

    By line: '116/01, 03 DP od 12.10: R-1 → R-2', one item per line, day group, date and
    kind of change, with all the brigades it hits; brigades shared by several lines get an
    item of their own, '411+502 / 721, 722'. By operator: a block per depot or
    operator, listing what it takes over ('116/01, 03 DP od 12.10: z R-1') and what it
    gives up ('… do R-2'), so a change between two of them shows in both blocks. A brigade
    renumbered between '6' and '06' goes to a list of its own, 'zmiany brygad':
    '739/6 → 06 DP od 1.12: R-1'."""
    changes = payload.get("changes")
    editions = payload.get("editions")
    if changes is None or not editions:
        return ""
    full_name = payload["depots"]
    trams = set(payload.get("trams", ()))
    group_name = {k: t for k, t, _ in DAY_GROUPS}
    changes, renumbered = split_renumberings(changes)
    # Changes come sorted by date, line, brigade, so each group's brigades come out sorted.
    grouped = {}
    for c in changes:
        grouped.setdefault((c["date"], c["line"], c["group"], c["old"], c["new"]), []).append(c["brigade"])
    group_order = [k for k, _t, _codes in DAY_GROUPS]

    def row_order(kv):
        (date, line, group, _old, _new), brigades = kv
        first = brigades[0][0] if isinstance(brigades[0], tuple) else brigades[0]
        return line_sort(line), date, group_order.index(group), brigade_sort(first) + (first,)
    rows = sorted(grouped.items(), key=row_order)
    renum_rows = sorted(renumbered.items(), key=row_order)

    def item(ops, date, line, group, brigades, what):
        return (f'<li data-o="|{html.escape("|".join(ops))}|" data-m="{line_mode(line, trams)}">'
                f'<b>{html.escape(line)}{" / " if "+" in line else "/"}'
                f'{html.escape(", ".join(brigades))}</b> '
                f'<span class="dt" title="{html.escape(group_name[group])}">{DAY_SHORT[group]}</span> '
                f'od {short_date(date)}: {what}</li>')

    by_line = []
    # operator -> [items it takes over, items it gives up, brigades in, brigades out,
    #              items renumbering brigades it keeps, brigades renumbered]
    by_op = {}
    for (date, line, group, old, new), brigades in rows:
        old_badge = old and depot_badge(old, full_name)
        new_badge = new and depot_badge(new, full_name)
        if old and new:
            what = f"{old_badge} → {new_badge}"
        elif new:
            what = f"nowa brygada {new_badge}"
        else:
            what = f"likwidacja <span class=\"was\">(było {old_badge})</span>"
        old_parts = old.split(" / ") if old else []
        new_parts = new.split(" / ") if new else []
        by_line.append(item(old_parts + new_parts, date, line, group, brigades, what))
        # A part on both sides (R-1 → R-1 / R-2) neither gains nor loses the brigade.
        for part in new_parts:
            if part not in old_parts:
                gains = by_op.setdefault(part, [[], [], 0, 0, [], 0])
                gains[0].append(item([part], date, line, group, brigades,
                                     f"z {old_badge}" if old else "nowa brygada"))
                gains[2] += len(brigades)
        for part in old_parts:
            if part not in new_parts:
                losses = by_op.setdefault(part, [[], [], 0, 0, [], 0])
                losses[1].append(item([part], date, line, group, brigades,
                                      f"do {new_badge}" if new else "likwidacja"))
                losses[3] += len(brigades)

    # A brigade renumbered between "6" and "06": one item, "739/6 → 06", not a removal
    # and a new brigade.
    by_line_renum = []
    for (date, line, group, old, new), pairs in renum_rows:
        old_badge = depot_badge(old, full_name)
        new_badge = depot_badge(new, full_name)
        labels = [f"{a} → {b}" for a, b in pairs]
        old_parts = old.split(" / ")
        new_parts = new.split(" / ")
        by_line_renum.append(item(old_parts + new_parts, date, line, group, labels,
                                  new_badge if old == new else f"{old_badge} → {new_badge}"))
        # Per operator, one that keeps the brigade sees the renumbering; one that takes it
        # over or gives it up sees that.
        for part in new_parts:
            entry = by_op.setdefault(part, [[], [], 0, 0, [], 0])
            if part in old_parts:
                entry[4].append(item([part], date, line, group, labels, depot_badge(part, full_name)))
                entry[5] += len(pairs)
            else:
                entry[0].append(item([part], date, line, group, labels, f"z {old_badge}"))
                entry[2] += len(pairs)
        for part in old_parts:
            if part not in new_parts:
                entry = by_op.setdefault(part, [[], [], 0, 0, [], 0])
                entry[1].append(item([part], date, line, group, labels, f"do {new_badge}"))
                entry[3] += len(pairs)

    def change_list(title, items, n=None):
        count = "" if n is None else f' <span class="cnt">{n}</span>'
        heading = f"<h4>{title}{count}</h4>" if title else ""
        return f'<div class="cl">{heading}<ul class="changes">{"".join(items)}</ul></div>' if items else ""

    blocks = []
    for op in sorted(by_op, key=depot_sort):
        gains, losses, n_in, n_out, renums, n_renum = by_op[op]
        lists = (change_list("przejmuje", gains, n_in) + change_list("oddaje", losses, n_out)
                 + change_list("zmiany brygad", renums, n_renum))
        blocks.append(
            f'<div class="cg" data-o="|{html.escape(op)}|" data-m="{depot_mode(op)}"><h3><span class="sw" '
            f'style="background:{DEPOT_COLOR.get(op, DEPOT_COLOR[UNKNOWN])}"></span>'
            f'{html.escape(full_name.get(op, op))} <span class="cnt">+{n_in} −{n_out}</span></h3>'
            f'{lists}</div>')

    span = f"{short_date(editions[0])}–{short_date(editions[-1])}"
    if not by_line and not by_line_renum:
        body = '<div class="sub">Brak zmian.</div>'
    else:
        body = f"""<div class="tabs" role="tablist" aria-label="Grupowanie zmian">
      <button type="button" role="tab" id="tab-line" aria-controls="ch-line" aria-selected="true">wg linii</button>
      <button type="button" role="tab" id="tab-op" aria-controls="ch-op" aria-selected="false" tabindex="-1">wg zakładu / przewoźnika</button>
    </div>
    <div id="ch-line" role="tabpanel" aria-labelledby="tab-line">{change_list("", by_line)}{change_list("zmiany brygad", by_line_renum, sum(len(p) for _k, p in renum_rows))}</div>
    <div id="ch-op" role="tabpanel" aria-labelledby="tab-op" hidden>{"".join(blocks)}</div>"""
    return f"""<section>
    <h2 class="sec">Zmiany przydziału</h2>
    <div class="sub">Rozkłady w feedzie: {span}. Każdą edycję porównujemy z tym samym dniem
      tygodnia tydzień wcześniej. Liczby przy zakładach to brygady (osobno dla DP i DŚ).</div>
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
    # brygady.json files from before trams were added have no "trams": all buses.
    trams = set(payload.get("trams", ()))
    day_runs = runs(days)
    n_plans = max(d["plan"] for d in days) + 1
    # Rendered for the first day; the script switches to today's on load.
    shown = day_runs[0][2]

    counts = {}  # depot -> [brigades in plan 0, plan 1, ...]
    # The same per mode, for "nieznany", the one entry both buses and trams can have.
    mode_counts = {}  # (depot, mode) -> [...]
    for line, variants in lines.items():
        for v in variants:
            for label, brigades in v["depots"].items():
                for part in label.split(" / "):
                    for per_plan in (counts.setdefault(part, [0] * n_plans),
                                     mode_counts.setdefault((part, line_mode(line, trams)), [0] * n_plans)):
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
            blocks = []
            for label in sorted(v["depots"], key=depot_sort):
                entries = v["depots"][label]
                ops = operators_attr(label)
                per_contract = {}
                for e in entries:
                    per_contract.setdefault(entry_contract(e), []).append(e)
                split = any(per_contract)
                blocks.append(
                    f'<div class="g{" has-k" if split else ""}" data-o="{ops}">'
                    f'{depot_badge(label, full_name)}'
                    f'<span>{format_brigades(entries, by_class)}</span></div>')
                if not split:
                    continue
                # Shown instead of the row above while its operator is picked in the legend.
                for contract in sorted(per_contract, key=lambda c: c or ""):
                    name = contract_label(label, contract) if contract else label
                    color = CONTRACT_COLOR.get(name.split(" / ")[0])
                    blocks.append(
                        f'<div class="g k" data-o="{ops}" hidden>'
                        f'{depot_badge(name, full_name, color)}'
                        f'<span>{format_brigades(per_contract[contract], by_class)}</span></div>')
            rows.append(f'<div class="v" {plans_attr(v["plans"])}>{"".join(blocks)}</div>')
        idle = [n for n in range(n_plans) if not any(n in v["plans"] for v in variants)]
        if idle:
            rows.append(f'<div class="v none" {plans_attr(idle)}>nie kursuje</div>')
        cards.append(
            f'<article class="card" data-m="{line_mode(line, trams)}"><h2><a href="{html.escape(url)}" target="_blank" '
            f'rel="noopener" title="{html.escape(line)} na zbiorkom.live">{html.escape(line)}</a>{line_tag}</h2>'
            + "".join(rows) + "</article>"
        )

    legend = []
    for label in DEPOT_ORDER:
        if label not in counts:
            continue
        full = full_name.get(label, label)
        n = counts[label]
        per_mode = "" if depot_mode(label) else "".join(
            f' data-n{m}="{" ".join(map(str, mode_counts.get((label, m), [0] * n_plans)))}"'
            for m in ("bus", "tram"))
        legend.append(
            f'<button type="button" class="leg" data-o="{html.escape(label)}" data-m="{depot_mode(label)}" '
            f'aria-pressed="false" '
            f'data-n="{" ".join(map(str, n))}"{per_mode}{"" if n[shown] else " hidden"}>'
            f'<span class="sw" style="background:{DEPOT_COLOR[label]}"></span>'
            f'{html.escape(full)} <span class="cnt">{n[shown]}</span></button>'
        )
    split_ops = sorted({name.rsplit(" ", 1)[0] for name in CONTRACT_COLOR}, key=depot_sort)

    selector = []
    for i, (first, last, plan, members) in enumerate(day_runs):
        selector.append(
            f'<button type="button" data-p="{plan}" data-from="{first.isoformat()}" '
            f'data-to="{last.isoformat()}" aria-pressed="{"true" if i == 0 else "false"}">'
            f'{run_label(first, last, members)}</button>')

    vehicle_note = " · ".join(f"<b>{c}</b> — {html.escape(n)}" for c, n in VEHICLE_NAME.items())
    tram_note = " · ".join(f"<b>{html.escape(tag)}</b>" + (f" — {html.escape(tip)}" if tip != tag else "")
                           for tag, tip in TRAM_FLEET.values())
    span = f"{short_date(days[0]['date'])}–{short_date(days[-1]['date'])}"
    n_tram = sum(line in trams for line in lines)
    n_bus = len(lines) - n_tram
    line_count = f"{n_bus} linii autobusowych" + (f" i {n_tram} tramwajowych" if n_tram else "")
    modes = "".join(
        f'<button type="button" data-m="{m}" aria-pressed="{"true" if m == "bus" else "false"}">{t}</button>'
        for m, t in (("bus", "autobusy"), ("tram", "tramwaje")))
    # Not a mode: the combined view it would name is a T.Love song instead.
    modes += (f'<a href="{SONG_URL}" target="_blank" rel="noopener" '
              f'title="T.Love — Autobusy i tramwaje">autobusy i tramwaje ♪</a>')

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
  .card[data-m="tram"] h2 a {{ color: #b60000; }}
  .card h2 a:hover, .card[data-m="tram"] h2 a:hover {{ color: #06c; text-decoration: underline; }}
  .card h2 a:focus-visible {{ outline: 2px solid #06c; outline-offset: 2px; border-radius: 2px; }}
  .days, .modes {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 14px 0 0; }}
  .days button, .modes button, .modes a {{ font: inherit; font-size: 13px; padding: 4px 10px; cursor: pointer;
    border: 1px solid #ccc; border-radius: 5px; background: #fff; color: inherit; }}
  .days button:hover, .modes button:hover, .modes a:hover {{ border-color: #06c; }}
  .modes a {{ text-decoration: none; }}
  .days button[aria-pressed="true"], .modes button[aria-pressed="true"] {{ background: #06c; border-color: #06c; color: #fff; }}
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
  .leg {{ display: inline-flex; align-items: center; gap: 5px; font: inherit; color: inherit;
    padding: 2px 6px; margin: -2px -6px; border: 1px solid transparent; border-radius: 5px;
    background: none; cursor: pointer; }}
  .leg:hover {{ border-color: #ccc; }}
  .leg[aria-pressed="true"] {{ border-color: #06c; background: #eef4ff; }}
  .legend.on .leg[aria-pressed="false"] {{ opacity: .45; }}
  .sw {{ width: 13px; height: 13px; border-radius: 3px; border: 1px solid rgba(0,0,0,0.12);
    display: inline-block; }}
  .cnt {{ color: #999; }}
  .sec {{ font-size: 16px; margin: 22px 0 4px; }}
  .changes {{ margin: 8px 0 0; padding-left: 20px; font-size: 13px; line-height: 1.9; }}
  .changes .d {{ min-width: 0; margin: 0; }}
  .changes .dt {{ line-height: inherit; }}
  .was {{ color: #999; }}
  .tabs {{ display: flex; gap: 6px; margin: 10px 0 0; }}
  .tabs button {{ font: inherit; font-size: 13px; padding: 4px 10px; cursor: pointer;
    border: 1px solid #ccc; border-radius: 5px; background: #fff; color: inherit; }}
  .tabs button:hover {{ border-color: #06c; }}
  .tabs button[aria-selected="true"] {{ background: #06c; border-color: #06c; color: #fff; }}
  .cg h3 {{ font-size: 14px; margin: 16px 0 2px; display: flex; align-items: center; gap: 6px; }}
  .cg h3 .cnt {{ margin-left: 2px; font-weight: 400; font-size: 12px; }}
  .cl h4 {{ font-size: 12px; font-weight: 600; color: #666; margin: 6px 0 0; }}
  .cl .changes {{ margin-top: 0; }}
  .foot {{ margin-top: 14px; color: #777; font-size: 12px; line-height: 1.6; }}
  .foot a {{ color: #06c; }}
</style></head>
<body>
  <header class="h436"><a href="https://436.pl/"><img src="https://436.pl/436.png" alt="436"
    width="480" height="289">.pl/</a><span>przydzial-brygad/</span></header>
  <h1>Przydział brygad — zakłady i przewoźnicy WTP</h1>
  <div class="sub">{line_count} · rozkłady na dni {span} · bez linii L</div>
  <div class="sub">źródło: <a href="https://github.com/mccartney/WarsawGTFS">mccartney/WarsawGTFS</a> ·
    feed {html.escape(payload["feedVersion"] or "?")} · zaktualizowano {payload["generated"]}</div>
  <div class="sub">Uwagi? Można zgłaszać na <a href="https://wawkom.waw.pl/viewtopic.php?t=11090">forum wawkom</a>
    albo <a href="https://github.com/mccartney/przydzial-brygad/issues">w GitHubie</a>.</div>
  <nav class="modes" aria-label="Rodzaj linii">{modes}</nav>
  <div class="legend">{''.join(legend)}</div>
  <div class="sub">Kliknij zakład lub przewoźnika, by pokazać tylko jego brygady<span data-m="bus">;
    {' i '.join(html.escape(o) for o in split_ops)} — z podziałem na umowy (numer kodu z feedu)</span>.</div>
  <div class="sub" data-m="bus">{vehicle_note}</div>
  <div class="sub" data-m="tram">tramwaje: {tram_note} (typ taboru z feedu)</div>
  <nav class="days" aria-label="Dzień">{''.join(selector)}</nav>
  <div class="tools"><input id="q" type="search" placeholder="filtruj: numer linii lub zakład"></div>
  <div class="cards">{''.join(cards)}</div>
  {build_changes(payload)}
  <div class="foot">
    Zakład bierzemy wprost z pola <code>depot_id</code>, które feed GTFS dokłada do
    <code>trips.txt</code> — obejmuje ono zajezdnie MZA, przewoźników kontraktowych
    i zajezdnie Tramwajów Warszawskich. Jako <b>nieznany</b> wychodzą tylko kursy bez tego pola. Mobilis i Relobus
    mają po kilka kodów, po jednym na umowę; po wybraniu przewoźnika w legendzie brygady są
    rozpisane na te kody (np. <code>Relobus38</code> → „Relobus 38”).
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
  const legendBox = document.querySelector('.legend');
  const modeButtons = [...document.querySelectorAll('.modes button')];
  let plan = buttons[0].dataset.p;
  let op = '';  // the depot or operator picked in the legend, '' for all
  let mode = 'bus';  // 'bus' or 'tram'
  // Elements with no data-m (or an empty one, like "nieznany") belong to both modes.
  const inMode = e => !e.dataset.m || e.dataset.m === mode;
  const mine = e => !op || e.dataset.o.includes(`|${{op}}|`);
  const filter = () => {{
    const t = q.value.trim().toLowerCase();
    for (const c of cards) {{
      if (!inMode(c)) {{ c.hidden = true; continue; }}
      // With an operator picked, only its rows stay, those with contracts split per contract.
      for (const g of c.querySelectorAll('.g')) {{
        g.hidden = !mine(g) || (g.classList.contains('k') ? !op : !!op && g.classList.contains('has-k'));
      }}
      // Match a card on its number and the selected day's visible rows only.
      const rows = [...c.querySelectorAll('.v')].filter(v => !v.hidden);
      const shown = rows.flatMap(v => v.classList.contains('none') ? [v] : [...v.querySelectorAll('.g')]
        .filter(g => !g.hidden));
      c.hidden = (op && !shown.some(e => e.classList.contains('g'))) || (t &&
        ![c.querySelector('h2'), ...shown].some(e => e.textContent.toLowerCase().includes(t)));
    }}
    // An item in the per-operator tab also matches on its block's heading.
    const text = c => ((c.closest('.cg')?.querySelector('h3').textContent || '') + ' ' + c.textContent).toLowerCase();
    for (const c of changes) c.hidden = !inMode(c) || !mine(c) || (t && !text(c).includes(t));
    // In the per-operator tab, drop the lists and blocks the filters have emptied.
    for (const b of document.querySelectorAll('.cl, .cg')) b.hidden = !b.querySelector('li:not([hidden])');
  }};
  const tabs = [...document.querySelectorAll('.tabs [role=tab]')];
  const showTab = tab => {{
    for (const o of tabs) {{
      o.setAttribute('aria-selected', o === tab);
      o.tabIndex = o === tab ? 0 : -1;
      document.getElementById(o.getAttribute('aria-controls')).hidden = o !== tab;
    }}
  }};
  for (const tab of tabs) {{
    tab.addEventListener('click', () => showTab(tab));
    tab.addEventListener('keydown', e => {{
      const step = {{ArrowRight: 1, ArrowLeft: -1}}[e.key];
      if (!step) return;
      const next = tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length];
      showTab(next);
      next.focus();
    }});
  }}
  const pick = o => {{
    op = o;
    for (const l of legend) l.setAttribute('aria-pressed', l.dataset.o === op);
    legendBox.classList.toggle('on', !!op);
    history.replaceState(null, '', op ? '#' + encodeURIComponent(op) : location.pathname + location.search);
    filter();
  }};
  for (const l of legend) l.addEventListener('click', () => pick(op === l.dataset.o ? '' : l.dataset.o));
  // Legend entries for the selected day and mode; a picked one that goes away is let go.
  const showLegend = () => {{
    for (const l of legend) {{
      const n = (l.dataset['n' + mode] || l.dataset.n).split(' ')[plan];
      l.querySelector('.cnt').textContent = n;
      l.hidden = n === '0' || !inMode(l);
      if (l.hidden && l.dataset.o === op) pick('');
    }}
  }};
  const setMode = m => {{
    mode = m;
    for (const b of modeButtons) b.setAttribute('aria-pressed', b.dataset.m === mode);
    for (const e of document.querySelectorAll('.sub[data-m], .sub [data-m]')) e.hidden = !inMode(e);
    showLegend();
    filter();
  }};
  for (const b of modeButtons) b.addEventListener('click', () => {{
    setMode(b.dataset.m);
    try {{ localStorage.setItem('tryb', mode); }} catch (e) {{}}
  }});
  const select = b => {{
    plan = b.dataset.p;
    for (const o of buttons) o.setAttribute('aria-pressed', o === b);
    for (const v of document.querySelectorAll('.v')) v.hidden = !v.dataset.p.includes(` ${{plan}} `);
    showLegend();
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
  // The mode picked last time, unless a linked depot belongs to the other one.
  let saved = null;
  try {{ saved = localStorage.getItem('tryb'); }} catch (e) {{}}
  if (modeButtons.some(b => b.dataset.m === saved)) mode = saved;
  let fromHash = '';
  try {{ fromHash = decodeURIComponent(location.hash.slice(1)); }} catch (e) {{}}
  const linked = legend.find(l => l.dataset.o === fromHash);
  if (linked && !inMode(linked)) mode = linked.dataset.m;
  if (linked) pick(fromHash);
  setMode(mode);
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

    slots, fleet, contracts, unmapped, trams, blocks = read_trips(feed, editions)
    days, lines = day_plans(slots, fleet, contracts, calendar, editions)
    for day in days:
        print(f"  {day['date']} {calendar[datetime.date.fromisoformat(day['date'])]} "
              f"plan {day['plan']}", file=sys.stderr)
    changes = find_changes(slots, editions, shared_lines(blocks))
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
        "trams": sorted(trams & lines.keys(), key=line_sort),
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
