"""Layout validation - the "LLM hook".

Any layout JSON (hand-written, LLM-written, or produced by generate.py) goes
through ``validate_layout`` before it is rendered. It checks:

* schema: required fields, positive sizes, unique room ids, known references
* geometry: rooms inside the footprint, no overlaps, no uncovered gaps
* doors: the two rooms really share a wall long enough for the door, door
  offsets fit, exterior doors sit on an exterior wall, doors do not collide
* windows: on an exterior wall of their room, fit the wall, do not hit a door
* circulation: every room reachable from an exterior door; bedrooms open off
  a hallway / living area; bathrooms open off circulation or a bedroom (ensuite)
* sizes: code-style minimums (errors) and "realistic" sizes (warnings)

Usage::

    from fpgen import load_layout, validate_layout
    report = validate_layout(load_layout("my_layout.json"))
    if not report.ok:
        print(report.format())
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .geometry import (CIRCULATION, SIDES, TOL, Footprint, Seg, overlap_area,
                       rect, shared_segment, side_of, side_segment)

KNOWN_TYPES = {
    "bedroom", "bathroom", "half_bath", "kitchen", "living", "dining", "hallway",
    "closet", "walk_in_closet", "laundry", "pantry", "entry", "foyer", "family",
    "office", "storage", "garage", "mudroom", "other",
}
DOOR_KINDS = {"door", "opening", "sliding", "garage"}   # garage = overhead door on an exterior wall
UNCONDITIONED = {"garage"}

# (min area sqft, min dimension ft) -> hard errors (roughly IRC R304/R305 + common sense)
HARD_MIN = {
    "bedroom": (70, 7), "living": (70, 7), "kitchen": (50, 6), "dining": (50, 6),
    "bathroom": (30, 4.5), "half_bath": (15, 3), "hallway": (0, 3),
    "closet": (4, 2), "walk_in_closet": (16, 3.5), "laundry": (15, 3), "pantry": (6, 2),
    "office": (50, 6),
    "garage": (180, 10),
}
# realistic sizes -> warnings
SOFT_MIN = {
    "bedroom": (100, 9.5), "living": (180, 12), "kitchen": (100, 9), "dining": (100, 9.5),
    "bathroom": (40, 5), "hallway": (0, 3.5),
}
WINDOW_ROOMS = {"bedroom", "living", "kitchen", "dining", "family", "office"}


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    doors: list[dict] = field(default_factory=list)    # resolved door geometry
    windows: list[dict] = field(default_factory=list)  # resolved window geometry
    footprint: Footprint | None = None
    living_sqft: float = 0.0        # conditioned area (everything except garage / unconditioned rooms)
    garage_sqft: float = 0.0
    other_unconditioned_sqft: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self) -> str:
        lines = []
        if self.errors:
            lines.append(f"INVALID layout: {len(self.errors)} error(s)")
            lines += [f"  ERROR: {e}" for e in self.errors]
        else:
            lines.append("Layout is valid.")
        lines += [f"  warning: {w}" for w in self.warnings]
        return "\n".join(lines)


def load_layout(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # tolerate LLM output wrapped in a ```json fence
    if "```" in text:
        parts = text.split("```")
        for p in parts[1::2]:
            p = p.strip()
            if p.startswith("json"):
                p = p[4:]
            if p.strip().startswith("{"):
                text = p
                break
    return json.loads(text)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _label(room: dict) -> str:
    return f"'{room.get('name', room.get('id'))}'"


def validate_layout(layout: dict) -> ValidationReport:
    rep = ValidationReport()
    E, Wn = rep.errors.append, rep.warnings.append

    # ---------------- schema ----------------
    FP, fp_errs = Footprint.from_json(layout.get("footprint"))
    if FP is None:
        for e in fp_errs:
            E(e)
        return rep
    rep.footprint = FP
    W, D = FP.W, FP.D
    fp_desc = f"{W:g}' x {D:g}'" + ("" if FP.is_rect else f" ({len(FP.rects)}-rectangle)")
    rooms = layout.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        E("'rooms' must be a non-empty list")
        return rep

    by_id: dict[str, dict] = {}
    name_to_id: dict[str, str] = {}
    good_rooms = []
    for i, r in enumerate(rooms):
        if not isinstance(r, dict):
            E(f"rooms[{i}] is not an object")
            continue
        rid = r.get("id") or r.get("name")
        if not rid:
            E(f"rooms[{i}] needs an 'id' (or 'name')")
            continue
        r.setdefault("id", rid)
        r.setdefault("name", rid)
        missing = [k for k in ("x", "y", "w", "d") if not _num(r.get(k))]
        if missing:
            E(f"room {_label(r)} is missing numeric {', '.join(missing)} "
              "(every room needs x, y = south-west corner and w, d = size in feet)")
            continue
        if r["w"] <= 0 or r["d"] <= 0:
            E(f"room {_label(r)} has non-positive size {r['w']} x {r['d']}")
            continue
        if rid in by_id:
            E(f"duplicate room id '{rid}'")
            continue
        if r.get("type") not in KNOWN_TYPES:
            Wn(f"room {_label(r)} has unknown type {r.get('type')!r}; treating as 'other' "
               f"(known: {', '.join(sorted(KNOWN_TYPES))})")
            r["type"] = "other"
        by_id[rid] = r
        name_to_id.setdefault(r["name"], rid)
        good_rooms.append(r)

    def lookup(ref):
        if ref in by_id:
            return ref
        return name_to_id.get(ref)

    # ---------------- geometry ----------------
    n_err_before_geom = len(rep.errors)
    for r in good_rooms:
        x0, y0, x1, y1 = rect(r)
        out = r["w"] * r["d"] - FP.covered(r)
        if out > 0.05:
            E(f"room {_label(r)} ({x0:g},{y0:g})-({x1:g},{y1:g}) is outside the "
              f"{fp_desc} footprint ({out:.1f} sq ft outside)")
    for i, a in enumerate(good_rooms):
        for b in good_rooms[i + 1:]:
            area, box = overlap_area(a, b)
            if area > 0.05:
                E(f"rooms {_label(a)} and {_label(b)} overlap by {area:.1f} sq ft "
                  f"in ({box[0]:g},{box[1]:g})-({box[2]:g},{box[3]:g})")
    covered = sum(FP.covered(r) for r in good_rooms)
    if covered < FP.area - 0.5 and not any("overlap" in e for e in rep.errors[n_err_before_geom:]):
        # locate a gap on a 0.5 ft grid
        gap = None
        step = 0.5
        y = step / 2
        while y < D and gap is None:
            x = step / 2
            while x < W:
                if FP.contains(x, y) and not any(rect(r)[0] <= x <= rect(r)[2] and rect(r)[1] <= y <= rect(r)[3]
                                                 for r in good_rooms):
                    gap = (x, y)
                    break
                x += step
            y += step
        where = f" e.g. near ({gap[0]:g}, {gap[1]:g})" if gap else ""
        E(f"rooms do not fill the footprint: {FP.area - covered:.1f} sq ft uncovered{where}")

    # ---------------- sizes ----------------
    for r in good_rooms:
        t = r["type"]
        area, mind = r["w"] * r["d"], min(r["w"], r["d"])
        if t in HARD_MIN:
            a_min, d_min = HARD_MIN[t]
            if area < a_min - 0.01 or mind < d_min - 0.01:
                E(f"{t} {_label(r)} is too small ({r['w']:g}' x {r['d']:g}' = {area:.0f} sq ft; "
                  f"minimum {a_min} sq ft and {d_min}' in each direction)")
                continue
        if t in SOFT_MIN:
            a_min, d_min = SOFT_MIN[t]
            if area < a_min or mind < d_min:
                Wn(f"{t} {_label(r)} is smaller than typical ({r['w']:g}' x {r['d']:g}')")
        if t not in ("hallway", "closet", "walk_in_closet", "pantry", "storage", "laundry"):
            ratio = max(r["w"], r["d"]) / mind
            if ratio > 2.6:
                Wn(f"{_label(r)} is very elongated ({r['w']:g}' x {r['d']:g}')")
        tgt = r.get("target") or {}
        tarea = tgt.get("area") or ((tgt.get("w") or 0) * (tgt.get("d") or 0))
        if tarea and abs(area - tarea) / tarea > 0.35:
            Wn(f"{_label(r)} area {area:.0f} sq ft is far from its target {tarea:.0f} sq ft")

    # ---------------- doors ----------------
    adj = defaultdict(set)
    ext_rooms = set()
    door_lines = defaultdict(list)  # (orient, c) -> [(s0, s1, desc)]
    person_doors = 0
    doors = layout.get("doors", [])
    if not isinstance(doors, list):
        E("'doors' must be a list")
        doors = []
    for i, d in enumerate(doors):
        desc = f"door #{i} ({d.get('from')} -> {d.get('to')})"
        kind = d.get("type", "door")
        if kind not in DOOR_KINDS:
            E(f"{desc}: type must be one of {sorted(DOOR_KINDS)}")
            continue
        refs = [d.get("from"), d.get("to")]
        is_ext = "exterior" in refs
        ids = []
        bad = False
        for ref in refs:
            if ref == "exterior":
                continue
            rid = lookup(ref)
            if rid is None:
                E(f"{desc}: unknown room '{ref}'")
                bad = True
            ids.append(rid)
        if bad:
            continue
        width = d.get("width", 3.0 if is_ext else 2.67)
        if not _num(width) or width <= 0:
            E(f"{desc}: width must be a positive number of feet")
            continue
        if is_ext:
            if len(ids) != 1:
                E(f"{desc}: an exterior door connects exactly one room to 'exterior'")
                continue
            room = by_id[ids[0]]
            sides = FP.exterior_sides(room)
            wall = d.get("wall")
            if not sides:
                E(f"{desc}: room {_label(room)} does not touch an exterior wall")
                continue
            if wall is None:
                wall = sides[0]
            if wall not in sides:
                E(f"{desc}: room {_label(room)} has no exterior wall on side {wall!r} "
                  f"(its exterior sides: {', '.join(sides)})")
                continue
            seg = side_segment(room, wall)
            swing_room = room
            if kind == "garage" and room["type"] != "garage":
                E(f"{desc}: overhead 'garage' doors are only allowed on a garage")
                continue
        else:
            if kind == "garage":
                E(f"{desc}: a 'garage' (overhead) door must lead to 'exterior'")
                continue
            if len(ids) != 2 or ids[0] == ids[1]:
                E(f"{desc}: a door must connect two different rooms")
                continue
            a, b = by_id[ids[0]], by_id[ids[1]]
            seg = shared_segment(a, b)
            if seg is None:
                E(f"{desc}: rooms {_label(a)} and {_label(b)} do not share a wall")
                continue
            sw = d.get("swing_into", ids[1])
            swr = lookup(sw)
            if swr not in ids:
                E(f"{desc}: swing_into must be one of the two rooms")
                continue
            swing_room = by_id[swr]
        if width > seg.length + TOL:
            E(f"{desc}: {width:g}' door does not fit on the {seg.length:g}' shared wall")
            continue
        off = d.get("offset", (seg.length - width) / 2)
        if not _num(off) or off < -TOL or off + width > seg.length + TOL:
            E(f"{desc}: offset {off} puts the door outside the {seg.length:g}' wall "
              f"(valid 0..{seg.length - width:g})")
            continue
        p0, p1 = seg.s0 + off, seg.s0 + off + width
        if is_ext:
            parts = FP.exterior_parts(room, wall)
            if not any(a - TOL <= p0 and p1 <= b + TOL for a, b in parts):
                E(f"{desc}: the door ({p0:g}..{p1:g}) is not on the exterior part of the {wall} wall of "
                  f"{_label(room)} (exterior portion(s): "
                  + ", ".join(f"{a:g}..{b:g}" for a, b in parts) + ")")
                continue
        else:
            types = {by_id[i]["type"] for i in ids}
            if "garage" in types and "bedroom" in types:
                E(f"{desc}: a bedroom may not open directly into a garage")
        key = (seg.orient, round(seg.c, 2))
        for q0, q1, other in door_lines[key]:
            if min(p1, q1) - max(p0, q0) > TOL:
                E(f"{desc} overlaps {other} on the same wall")
        door_lines[key].append((p0, p1, desc))
        hinge = d.get("hinge", "start")
        rep.doors.append({
            "seg": seg, "p0": p0, "p1": p1, "kind": kind, "exterior": is_ext,
            "hinge": p0 if hinge == "start" else p1, "hinge_end": hinge,
            "dir": side_of(swing_room, seg), "width": width,
            "room": ids[0] if is_ext else None,
        })
        if is_ext:
            ext_rooms.add(ids[0])
            if kind != "garage":
                person_doors += 1
        else:
            adj[ids[0]].add(ids[1])
            adj[ids[1]].add(ids[0])

    # ---------------- windows ----------------
    has_window = set()
    for i, w in enumerate(layout.get("windows", []) or []):
        rid = lookup(w.get("room"))
        desc = f"window #{i} ({w.get('room')}, {w.get('wall')})"
        if rid is None:
            E(f"{desc}: unknown room")
            continue
        room = by_id[rid]
        side = w.get("wall")
        if side not in SIDES:
            E(f"{desc}: wall must be one of S, N, W, E")
            continue
        if side not in FP.exterior_sides(room):
            E(f"{desc}: the {side} wall of {_label(room)} is not an exterior wall")
            continue
        seg = side_segment(room, side)
        width = w.get("width", 3.0)
        off = w.get("offset", (seg.length - width) / 2)
        if not (_num(width) and _num(off)) or off < -TOL or off + width > seg.length + TOL:
            E(f"{desc}: window (offset {off}, width {width}) does not fit the {seg.length:g}' wall")
            continue
        p0, p1 = seg.s0 + off, seg.s0 + off + width
        parts = FP.exterior_parts(room, side)
        if not any(a - TOL <= p0 and p1 <= b + TOL for a, b in parts):
            E(f"{desc}: window ({p0:g}..{p1:g}) is not on the exterior part of that wall "
              "(exterior portion(s): " + ", ".join(f"{a:g}..{b:g}" for a, b in parts) + ")")
            continue
        key = (seg.orient, round(seg.c, 2))
        if any(min(p1, q1) - max(p0, q0) > TOL for q0, q1, _ in door_lines[key]):
            E(f"{desc}: window overlaps a door")
            continue
        has_window.add(rid)
        rep.windows.append({"seg": seg, "p0": p0, "p1": p1, "dir": side_of(room, seg)})

    # ---------------- circulation ----------------
    if good_rooms and not person_doors:
        E("no exterior door: add a door with \"to\": \"exterior\" (e.g. the front door)")
    seen = set(ext_rooms)
    q = deque(ext_rooms)
    while q:
        cur = q.popleft()
        for n in adj[cur]:
            if n not in seen:
                seen.add(n)
                q.append(n)
    unreachable = [r for r in good_rooms if r["id"] not in seen]
    if ext_rooms and unreachable:
        E("unreachable from any exterior door: " + ", ".join(_label(r) for r in unreachable))
    for r in good_rooms:
        t = r["type"]
        ntypes = {by_id[n]["type"] for n in adj[r["id"]]}
        if t == "bedroom" and r["id"] in seen and not (ntypes & CIRCULATION):
            E(f"bedroom {_label(r)} must open off a hallway or living area "
              f"(it only connects to: {', '.join(_label(by_id[n]) for n in adj[r['id']]) or 'nothing'})")
        if t in ("bathroom", "half_bath") and r["id"] in seen and not (ntypes & (CIRCULATION | {"bedroom"})):
            E(f"bathroom {_label(r)} must open off a hallway, living area or a bedroom (ensuite)")
        if t in WINDOW_ROOMS and r["id"] not in has_window and FP.exterior_sides(r):
            Wn(f"{_label(r)} touches an exterior wall but has no window"
               + (" (bedrooms need an egress window)" if t == "bedroom" else ""))
        if t == "bedroom" and not FP.exterior_sides(r):
            Wn(f"bedroom {_label(r)} has no exterior wall, so no egress window is possible")
    if any(r["type"] == "bedroom" for r in good_rooms):
        baths = [r for r in good_rooms if r["type"] == "bathroom"]
        if baths and not any({by_id[n]["type"] for n in adj[b["id"]]} & CIRCULATION for b in baths):
            Wn("no full bathroom is reachable without walking through a bedroom")
    # ---------------- garage + areas ----------------
    garage_ids = {r["id"] for r in good_rooms if r["type"] == "garage"}
    for g in garage_ids:
        if not any(d["exterior"] and d["kind"] == "garage" and d.get("room") == g for d in rep.doors):
            Wn(f"garage {_label(by_id[g])} has no overhead 'garage' door on an exterior wall")
    for r in good_rooms:
        a = r["w"] * r["d"]
        if r["type"] in UNCONDITIONED:
            rep.garage_sqft += a
        elif r.get("conditioned") is False:
            rep.other_unconditioned_sqft += a
        else:
            rep.living_sqft += a
    return rep
