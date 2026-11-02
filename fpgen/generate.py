"""Deterministic layout generator (no ML).

Algorithm - zoned strip subdivision ("bar house"):

    north  +-------------------------------------------------+
           |  rear band: bedrooms / baths / closets / laundry |  depth Dr
           +-----------+-------------------------------------+
           |           |  hallway (4')                       |
           |  living   +-------------------------------------+
           |           |  front band: dining, kitchen,       |  depth Df
           |           |  (+ overflow bedroom groups)        |
    south  +-----------+-------------------------------------+  <- front door

1. The footprint (W x D) is chosen from the target square footage and an
   aspect ratio. A 4' hallway band splits it into a front (public) band and a
   rear (private) band; the living room spans front band + hallway depth.
2. Rooms are bundled into *groups* of *column units* (e.g. bedroom + closet
   column, primary bedroom + walk-in/bath column, bath + closet column).
   Each column unit is stacked from the hallway side to the exterior wall,
   so every unit touches the hallway (or living room) and the exterior.
3. Columns are given widths by a 1-D allocation (minimum widths respected,
   slack shared by target area) and snapped to a 6" grid. Groups that do not
   fit the rear band overflow to the far end of the front band.
4. Doors, openings and windows are placed by rule; the result is validated.
5. Many (aspect, band depth, overflow) candidates are tried and scored for
   room proportions / target areas; the best valid one wins. The seed picks
   the structural variant (mirroring, room order, group order, preferred
   aspect) so different seeds give different - but always valid - plans.
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field

from .geometry import CIRCULATION, TOL, exterior_sides, shared_segment, side_segment
from .validate import validate_layout

HALL = 4.0
GRID = 0.5
STRIP = 2.5          # reach-in closet depth
BATH_D = 8.5         # bath depth in a stacked column
LAUNDRY_D = 7.0


def snap(v: float) -> float:
    return round(v / GRID) * GRID


@dataclass(eq=False)
class Piece:
    type: str
    name: str
    depth: float | None          # None = takes the rest of the band depth
    door: object = None          # "circ" | Piece | None
    kind: str = "door"           # door | sliding | opening
    width: float = 2.67
    pref: str = "center"         # center | corner
    swing_into_self: bool = True
    target: float | None = None  # target area
    room: dict | None = None     # filled in at placement


@dataclass(eq=False)
class Unit:
    pieces: list
    min_w: float
    target_w: float
    grow: float
    x0: float = 0.0
    w: float = 0.0


@dataclass
class Targets:
    living: float
    dining: float
    kitchen: float
    primary: float
    bed: float


def _targets(sqft: float) -> Targets:
    c = lambda v, lo, hi: max(lo, min(hi, v))
    return Targets(
        living=c(0.15 * sqft, 200, 420),
        dining=c(0.075 * sqft, 110, 200),
        kitchen=c(0.085 * sqft, 120, 240),
        primary=c(0.11 * sqft, 170, 280),
        bed=c(0.085 * sqft, 120, 190),
    )


# --------------------------------------------------------------------------- groups
def _bed_unit(name, area, B, primary=False):
    p = Piece("bedroom", name, None, door="circ", pref="corner", width=2.67, target=area)
    mn = 11.5 if primary else 10.0
    return Unit([p], mn, max(mn, area / B), 1.0), p


def _strip_unit(bed: Piece, B):
    L = min(9.0, B - 3.5)
    if L >= 5:
        pieces = [Piece("closet", "Linen", None, door="circ", width=2.0),
                  Piece("closet", "Closet", L, door=bed, kind="sliding")]
    else:
        pieces = [Piece("closet", "Closet", None, door=bed, kind="sliding")]
    return Unit(pieces, STRIP, STRIP, 0.0)


def _group(desc, B, T: Targets, flip: bool):
    """Build the column units for a group descriptor in a band of depth B."""
    kind = desc[0]
    units = []
    if kind == "primary":
        _, bed_name, bath_name, hall_bath = desc
        bu, bed = _bed_unit(bed_name, T.primary, B, primary=True)
        units.append(bu)
        if bath_name and B - BATH_D >= 4:
            wic = Piece("walk_in_closet", "Walk-in Closet", None, door=bed, width=2.33,
                        swing_into_self=True)
            bath = Piece("bathroom", bath_name, BATH_D, door=bed, width=2.5)
            units.append(Unit([wic, bath], 7.5, 8.0, 0.15))
        elif bath_name:
            units.append(Unit([Piece("bathroom", bath_name, None, door=bed, width=2.5)], 7.5, 8.0, 0.15))
            units.insert(0, _strip_unit(bed, B))
        elif hall_bath and B - BATH_D >= 3:
            bath = Piece("bathroom", hall_bath, BATH_D, door="circ", width=2.5)
            cl = Piece("closet", "Closet", None, door=bed, kind="sliding")
            units.append(Unit([bath, cl], 5.0, 5.5, 0.12))
        else:
            units.append(_strip_unit(bed, B))
            if hall_bath:
                units.append(Unit([Piece("bathroom", hall_bath, None, door="circ", width=2.5)], 5.0, 5.5, 0.12))
    elif kind == "bed":
        _, bed_name, bath_name = desc
        bu, bed = _bed_unit(bed_name, T.bed, B)
        units.append(bu)
        if bath_name and B - BATH_D >= 3:
            bath = Piece("bathroom", bath_name, BATH_D, door="circ", width=2.5)
            cl = Piece("closet", "Closet", None, door=bed, kind="sliding")
            units.append(Unit([bath, cl], 5.0, 5.5, 0.12))
        else:
            units.append(_strip_unit(bed, B))
            if bath_name:
                units.append(Unit([Piece("bathroom", bath_name, None, door="circ", width=2.5)], 5.0, 5.5, 0.12))
    elif kind == "bath":
        _, bath_name = desc
        if B - BATH_D >= 3:
            bath = Piece("bathroom", bath_name, BATH_D, door="circ", width=2.5)
            units.append(Unit([bath, Piece("closet", "Linen", None, door=bath, width=2.0)], 5.0, 5.5, 0.12))
        else:
            units.append(Unit([Piece("bathroom", bath_name, None, door="circ", width=2.5)], 5.0, 5.5, 0.12))
    elif kind == "laundry":
        if B - LAUNDRY_D >= 3:
            lau = Piece("laundry", "Laundry", LAUNDRY_D, door="circ", width=2.5)
            units.append(Unit([lau, Piece("storage", "Storage", None, door=lau, width=2.33)], 5.5, 6.0, 0.15))
        else:
            units.append(Unit([Piece("laundry", "Laundry", None, door="circ", width=2.5)], 5.5, 6.0, 0.15))
    elif kind == "office":
        pass
    elif kind == "powder":
        pw = Piece("half_bath", "Powder Room", 6.0 if B >= 9 else None, door="circ", width=2.33)
        ps = [pw]
        if B >= 9:
            ps.append(Piece("closet", "Linen", None, door=pw, width=2.0))
        units.append(Unit(ps, 4.5, 5.0, 0.1))
    if flip:
        units.reverse()
    return units


# --------------------------------------------------------------------------- allocation
def _allocate(units, total):
    mins = [u.min_w for u in units]
    tg = [max(u.min_w, u.target_w) for u in units]
    if sum(mins) > total + 1e-6:
        return False
    if sum(tg) <= total:
        g = [u.grow for u in units]
        if sum(g) <= 0:
            return False
        w = [t + (total - sum(tg)) * gi / sum(g) for t, gi in zip(tg, g)]
    else:
        span = sum(t - m for t, m in zip(tg, mins))
        w = [m + (total - sum(mins)) * (t - m) / span for t, m in zip(tg, mins)]
    ws = [u.min_w if (u.grow == 0 and u.min_w == u.target_w) else snap(x) for u, x in zip(units, w)]
    diff = round((total - sum(ws)) / GRID)
    flex = sorted(range(len(units)), key=lambda i: -units[i].grow)
    flex = [i for i in flex if units[i].grow > 0]
    k = 0
    while diff != 0 and flex:
        i = flex[k % len(flex)]
        step = GRID if diff > 0 else -GRID
        if ws[i] + step >= units[i].min_w - 1e-9:
            ws[i] += step
            diff -= 1 if diff > 0 else -1
        k += 1
        if k > 1000:
            return False
    if diff != 0:
        return False
    for u, wv in zip(units, ws):
        u.w = wv
    return True


# --------------------------------------------------------------------------- build
@dataclass
class Plan:
    beds: int
    baths: float
    sqft: float
    living_left: bool
    front_order: tuple
    rear_groups: list        # descriptors, ordered from the living side outwards
    flips: dict
    front_door_frac: float
    aspect_pref: float
    laundry: bool
    public_band: str = "front"


def _make_plan(beds, baths, sqft, seed, laundry):
    rng = random.Random(seed)
    full = int(math.floor(baths + 1e-9))
    half = (baths - full) >= 0.5 - 1e-9
    ensuite = beds >= 2 and full >= 2
    hall_baths = full - (1 if ensuite else 0)
    bath_no = 1
    groups = []
    if beds >= 1:
        if ensuite:
            groups.append(("primary", "Bedroom 1", "Bath 1", None))
            bath_no = 2
        elif hall_baths > 0:
            # no ensuite: the hall bath sits next to the primary bedroom (its closet behind the bath)
            groups.append(("primary", "Bedroom 1", None, "Bath 1"))
            bath_no = 2
            hall_baths -= 1
        else:
            groups.append(("primary", "Bedroom 1", None, None))
    secondaries = []
    for i in range(2, beds + 1):
        bname = None
        if hall_baths > 0:
            bname = f"Bath {bath_no}"
            bath_no += 1
            hall_baths -= 1
        secondaries.append(("bed", f"Bedroom {i}", bname))
    extra = []
    for _ in range(hall_baths):
        extra.append(("bath", f"Bath {bath_no}"))
        bath_no += 1
    if half:
        extra.append(("powder",))
    if laundry:
        extra.append(("laundry",))
    # spread bath-bearing groups among plain ones for variety
    rng.shuffle(secondaries)
    others = secondaries + extra
    rng.shuffle(others)
    if rng.random() < 0.75:
        rear = others + groups          # primary suite at the far end (most private)
    else:
        rear = groups + others          # primary suite next to the living room
    flips = {g: rng.random() < 0.5 for g in rear}
    rng.random()
    # the two big structural switches cycle with the seed so neighbouring seeds always differ:
    # seed%2 -> kitchen/dining in the front or the rear band; seed%4 -> living room left or right
    living_left = (seed % 4) in (0, 1)
    return Plan(beds, baths, sqft, living_left,
                rng.choice([("dining", "kitchen"), ("kitchen", "dining")]),
                rear, flips, rng.uniform(0.2, 0.8), rng.uniform(1.3, 1.85), laundry,
                "rear" if seed % 2 == 0 else "front")


def _slug(name, used):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s, n = base, 2
    while s in used:
        s, n = f"{base}-{n}", n + 1
    used.add(s)
    return s


def _build(plan: Plan, W: float, D: float, Dr: float, n_over: int, office: bool = False,
           kd_combined: bool = False):
    T = _targets(plan.sqft)
    Df = D - HALL - Dr
    if Df < 10.5 or Dr < 10.5:
        return None
    Lh = Df + HALL
    Lw = snap(max(12.0, min(T.living / Lh, 0.42 * W)))
    if Lw > 0.5 * W:
        return None

    groups = list(plan.rear_groups)
    # overflow: move groups (not the primary suite) from the rear to the front band
    movable = [g for g in groups if g[0] != "primary"]
    movable.sort(key=lambda g: {"laundry": 0, "powder": 1, "bath": 2, "bed": 3}[g[0]])
    if n_over > len(movable):
        return None
    over = movable[:n_over]
    rear = [g for g in groups if g not in over]
    pub_rear = plan.public_band == "rear"
    PB = Dr if pub_rear else Df          # depth of the band holding kitchen/dining
    use_pantry_laundry = False
    # a deep band: stack a laundry/pantry between the hallway and the kitchen
    kit = Piece("kitchen", "Kitchen", None, target=T.kitchen)
    if PB >= 16:
        extra_d = min(7.0, snap(PB - 12))
        if ("laundry",) in groups:
            use_pantry_laundry = True
            hp = Piece("laundry", "Laundry", extra_d, door="circ", width=2.5)
        else:
            hp = Piece("pantry", "Pantry", extra_d, door=kit, width=2.33)
        kitchen_units = [Unit([hp, kit], 10.0, max(10.0, T.kitchen / (PB - extra_d)), 1.0)]
    else:
        kitchen_units = [Unit([kit], 10.0, max(10.0, T.kitchen / PB), 1.0)]
    if use_pantry_laundry:
        rear = [g for g in rear if g != ("laundry",)]
        over = [g for g in over if g != ("laundry",)]
    din = Piece("dining", "Dining", None, target=T.dining)
    dining_units = [Unit([din], 10.0, max(10.0, T.dining / PB), 1.0)]
    pub = {"dining": dining_units, "kitchen": kitchen_units}
    pub_units = pub[plan.front_order[0]] + pub[plan.front_order[1]]
    if kd_combined:  # small homes: one eat-in kitchen instead of separate kitchen + dining
        kd = Piece("kitchen", "Kitchen / Dining", None, target=T.kitchen + T.dining * 0.6)
        pub_units = [Unit([kd], 11.0, max(11.0, kd.target / PB), 1.0)]

    rear_units = list(pub_units) if pub_rear else []
    for g in rear:
        rear_units += _group(g, Dr, T, plan.flips[g])
    front_units = [] if pub_rear else list(pub_units)
    for g in over:
        front_units += _group(g, Df, T, plan.flips[g])
    if pub_rear and not front_units:
        return None
    if office:  # large homes: a den/office off the hall soaks up front-band width
        front_units.append(Unit([Piece("office", "Office", None, door="circ", pref="corner", width=2.67,
                                       target=130)], 9.5, max(9.5, 130 / Df), 1.0))

    if not _allocate(rear_units, W) or not _allocate(front_units, W - Lw):
        return None

    # ---------------------------------------------------------------- rooms
    used = set()
    rooms = []

    def to_x(u, w):
        return u if plan.living_left else W - u - w

    def add(piece: Piece, x, y, w, d):
        r = {"id": _slug(piece.name, used), "name": piece.name, "type": piece.type,
             "x": x, "y": y, "w": w, "d": d}
        if piece.target:
            r["target"] = {"area": round(piece.target)}
        piece.room = r
        rooms.append(r)
        return r

    living = add(Piece("living", "Living Room", None, target=T.living), to_x(0, Lw), 0.0, Lw, Lh)
    hall = add(Piece("hallway", "Hall", None), to_x(Lw, W - Lw), Df, W - Lw, HALL)

    all_pieces = []

    def place_band(units, u_start, band):  # band: "rear" | "front"
        u = u_start
        B = Dr if band == "rear" else Df
        for unit in units:
            fixed = sum(p.depth for p in unit.pieces if p.depth is not None)
            rest = B - fixed
            if any(p.depth is None for p in unit.pieces) and rest < 2.5 - 1e-9:
                return False
            v = 0.0
            x = to_x(u, unit.w)
            for p in unit.pieces:
                d = p.depth if p.depth is not None else rest
                if band == "rear":
                    y = Df + HALL + v
                else:
                    y = Df - v - d
                add(p, x, y, unit.w, d)
                all_pieces.append(p)
                v += d
            u += unit.w
        return True

    if not place_band(rear_units, 0.0, "rear") or not place_band(front_units, Lw, "front"):
        return None

    # ---------------------------------------------------------------- doors
    doors = []
    room_doors = {}  # room id -> list of (x, y) door centers

    def center_of(seg, p0, p1):
        m = (p0 + p1) / 2
        return (m, seg.c) if seg.orient == "h" else (seg.c, m)

    def add_door(a, b, kind="door", width=2.67, pref="center", swing=None, opening_margin=1.0):
        seg = shared_segment(a, b)
        if seg is None:
            return False
        L = seg.length
        if kind == "opening":
            width = min(L - 2 * opening_margin, 8.0) if L > 6 else L - 0.5
        elif kind == "sliding":
            width = min(L - 1.0, 6.0)
        if width < 2.0 or width > L - 0.5 + 1e-9:
            return False
        hinge = "start"
        if pref == "corner":
            # door near the end of the wall that is farthest from the room's other doors
            others = room_doors.get(b["id"], [])
            best = None
            for end in ("start", "end"):
                off = 0.5 if end == "start" else L - width - 0.5
                c = center_of(seg, seg.s0 + off, seg.s0 + off + width)
                score = min((math.dist(c, o) for o in others), default=0)
                if best is None or score > best[0] + 1e-6:
                    best = (score, off, end)
            _, off, hinge = best
        else:
            off = (L - width) / 2
        off = round(off, 3)
        d = {"from": a["id"], "to": b["id"], "type": kind, "width": round(width, 3), "offset": off}
        if kind == "door":
            d["hinge"] = hinge
            d["swing_into"] = (swing or b)["id"]
        doors.append(d)
        c = center_of(seg, seg.s0 + off, seg.s0 + off + width)
        room_doors.setdefault(a["id"], []).append(c)
        room_doors.setdefault(b["id"], []).append(c)
        return True

    # 1) openings between public rooms and hall/living
    chain = [living] + [u.pieces[-1].room for u in pub_units]
    for a, b in zip(chain, chain[1:]):
        if not add_door(a, b, kind="opening"):
            return None
    if not add_door(living, hall, kind="opening", opening_margin=0.25):
        return None
    # 2) doors from closets/baths into their owner room (placed first so corner doors avoid them)
    for p in all_pieces:
        if isinstance(p.door, Piece):
            tgt = p.door.room
            swing = p.room if p.swing_into_self and p.kind == "door" else tgt
            if not add_door(tgt, p.room, kind=p.kind, width=p.width, swing=swing):
                return None
    # 3) doors from circulation
    for p in all_pieces:
        if p.door == "circ":
            best = None
            for c in (hall, living):
                seg = shared_segment(c, p.room)
                # prefer the hallway; fall back to the living room
                if seg and seg.length >= p.width + 0.5 - 1e-9 and best is None:
                    best = (seg.length, c)
            if best is None:
                return None
            if not add_door(best[1], p.room, kind=p.kind, width=p.width, pref=p.pref):
                return None
    # 4) exterior doors: front door into the living room, side door at the end of the hall
    off = round(snap(plan.front_door_frac * (Lw - 3.0)), 2)
    off = max(1.0, min(Lw - 4.0, off))
    doors.append({"from": living["id"], "to": "exterior", "wall": "S", "type": "door",
                  "width": 3.0, "offset": off, "hinge": "start"})
    # secondary (back/side) door: laundry/mudroom or kitchen side wall, else the end of the hall
    side_door = None
    for r in rooms:
        if r["type"] not in ("laundry", "kitchen"):
            continue
        for side in exterior_sides(r, W, D):
            if side == "S":
                continue
            seg = side_segment(r, side)
            if seg.length < 5.0:
                continue
            # put the door at the end of the wall farthest from the hallway
            if seg.orient == "v":
                far_end_high = (r["y"] + r["d"] / 2) > hall["y"] + HALL / 2
            else:
                far_end_high = (r["x"] + r["w"] / 2) > hall["x"] + hall["w"] / 2
            off = seg.length - 3.5 if far_end_high else 0.5
            cand = (0 if r["type"] == "laundry" else 1,
                    {"from": r["id"], "to": "exterior", "wall": side, "type": "door", "width": 3.0,
                     "offset": off, "hinge": "end" if far_end_high else "start"})
            if side_door is None or cand[0] < side_door[0]:
                side_door = cand
    if side_door:
        doors.append(side_door[1])
    else:
        end_x = hall["x"] + hall["w"] if plan.living_left else hall["x"]
        crowded = any(abs(c[0] - end_x) < 4.5 for c in room_doors.get(hall["id"], []))
        if not crowded:
            doors.append({"from": hall["id"], "to": "exterior", "wall": "E" if plan.living_left else "W",
                          "type": "door", "width": 3.0, "offset": 0.5, "hinge": "start"})

    # ---------------------------------------------------------------- windows
    windows = []
    ext_door_iv = {}
    for d in doors:
        if d["to"] == "exterior":
            r = next(r for r in rooms if r["id"] == d["from"])
            seg = side_segment(r, d["wall"])
            ext_door_iv.setdefault(r["id"], []).append((d["wall"], seg.s0 + d["offset"],
                                                        seg.s0 + d["offset"] + d["width"]))
    win_w = {"bedroom": 3.0, "living": 5.0, "dining": 4.0, "kitchen": 3.5, "bathroom": 2.0,
             "half_bath": 2.0, "laundry": 2.5, "office": 3.0}
    for r in rooms:
        t = r["type"]
        if t not in win_w:
            continue
        for side in exterior_sides(r, W, D):
            seg = side_segment(r, side)
            if seg.length < win_w[t] + 2.0:
                continue
            n = 2 if (seg.length >= 15 and t in ("living", "bedroom", "dining")) else 1
            if t == "bedroom" and len(exterior_sides(r, W, D)) > 1:
                n = 1
            # free intervals along the wall
            free = [(seg.s0 + 1.0, seg.s1 - 1.0)]
            for s, q0, q1 in ext_door_iv.get(r["id"], []):
                if s != side:
                    continue
                nf = []
                for f0, f1 in free:
                    if q1 + 1.0 <= f0 or q0 - 1.0 >= f1:
                        nf.append((f0, f1))
                    else:
                        if q0 - 1.0 > f0:
                            nf.append((f0, q0 - 1.0))
                        if q1 + 1.0 < f1:
                            nf.append((q1 + 1.0, f1))
                free = nf
            free = [f for f in free if f[1] - f[0] >= win_w[t]]
            if not free:
                continue
            if len(free) >= 2:
                spots = free[:2] if n == 2 else [max(free, key=lambda f: f[1] - f[0])]
                for f0, f1 in spots:
                    c = (f0 + f1) / 2
                    windows.append({"room": r["id"], "wall": side, "width": win_w[t],
                                    "offset": round(c - win_w[t] / 2 - seg.s0, 3)})
            else:
                f0, f1 = free[0]
                ww = win_w[t]
                if n == 2 and f1 - f0 >= 2 * ww + 3:
                    cs = [f0 + (f1 - f0) / 4, f0 + 3 * (f1 - f0) / 4]
                else:
                    cs = [(f0 + f1) / 2]
                for c in cs:
                    windows.append({"room": r["id"], "wall": side, "width": ww,
                                    "offset": round(c - ww / 2 - seg.s0, 3)})

    layout = {
        "version": 1,
        "units": "ft",
        "title": f"{plan.beds} Bedroom / {plan.baths:g} Bath",
        "footprint": {"width": W, "depth": D},
        "rooms": rooms,
        "doors": doors,
        "windows": windows,
    }
    return layout


def _score(layout, plan: Plan, aspect):
    s = 0.0
    for r in layout["rooms"]:
        t = r["type"]
        w, d = r["w"], r["d"]
        ratio = max(w, d) / min(w, d)
        if t == "office":
            s += 0.5 + 10 * max(0.0, ratio - 1.4) ** 2 + 3 * ((w * d - 130) / 130) ** 2
        if t in ("bedroom", "living", "dining", "kitchen"):
            s += 10 * max(0.0, ratio - 1.3) ** 2
            tgt = (r.get("target") or {}).get("area")
            if tgt:
                s += 3 * ((w * d - tgt) / tgt) ** 2 + 25 * max(0.0, w * d / tgt - 1.25) ** 2
        if t == "bathroom":
            s += 2 * max(0.0, max(w, d) - 11) ** 2 / 10 + 2 * max(0.0, w * d - 90) / 50
        if t in ("closet", "storage", "pantry") and w * d > 50:
            s += (w * d - 50) / 40
        if t == "walk_in_closet":
            s += max(0.0, w * d - 70) / 30
        if t == "laundry":
            s += max(0.0, w * d - 70) / 40
    if any(r["name"] == "Kitchen / Dining" for r in layout["rooms"]):
        s += 1.0
    hall = next(r for r in layout["rooms"] if r["type"] == "hallway")
    s += 0.02 * hall["w"]
    s += 2 * (aspect - plan.aspect_pref) ** 2
    return s


def check_generation_request(beds, baths, sqft, garage=0, shape="rect", garage_side=None) -> None:
    """Reject a program the generator cannot attempt.

    These are the same checks the command line runs before it searches for a
    layout. Callers can use them to refuse a job without generating one.
    """
    if beds < 1 or beds > 6:
        raise ValueError("beds must be between 1 and 6")
    if baths < 1 or baths > 5:
        raise ValueError("baths must be between 1 and 5")
    if garage not in (0, 1, 2, 3):
        raise ValueError("garage must be 0, 1, 2 or 3 cars")
    if shape not in ("rect", "L"):
        raise ValueError("shape must be 'rect' or 'L'")
    if garage_side not in (None, "left", "right"):
        raise ValueError("garage_side must be 'left' or 'right'")
    min_sqft = 350 + 220 * beds + 50 * baths
    if sqft < min_sqft:
        raise ValueError(f"{sqft:g} sq ft is too small for {beds} bed / {baths:g} bath "
                         f"(need at least ~{min_sqft:g} sq ft)")


def generate_layout(beds: int = 3, baths: float = 2, sqft: float = 1600, seed: int = 1,
                    laundry: bool | None = None, garage: int = 0, shape: str = "rect",
                    garage_side: str | None = None) -> dict:
    """Generate a valid layout dict for the given program. Deterministic per seed.

    garage: 0 (none) or 1/2/3 cars. shape: "rect" or "L". garage_side: "left"/"right"/None
    (None = the hallway end, so the garage man door lands in the hall/laundry).
    ``sqft`` is always the conditioned (living) area; the garage is reported separately.
    """
    check_generation_request(beds, baths, sqft, garage, shape, garage_side)
    if laundry is None:
        laundry = sqft >= 1100
    if garage == 0 and shape == "rect":
        return _generate_house(beds, baths, sqft, seed, laundry)
    from .wings import generate_with_wing
    return generate_with_wing(beds, baths, sqft, seed, laundry, garage, shape, garage_side)


def _generate_house(beds, baths, sqft, seed, laundry, laundry_last=False):
    """The original rectangular generator (unchanged behaviour for rect plans)."""
    plan = _make_plan(beds, baths, sqft, seed, laundry)
    if laundry_last and ("laundry",) in plan.rear_groups:
        # garage plans: put the laundry at the hallway end, next to the garage (mudroom entry)
        plan.rear_groups = [g for g in plan.rear_groups if g != ("laundry",)] + [("laundry",)]
    best = _search(plan, sqft)
    if best is None:  # this seed's structural variant can't hold the program: try the other one
        import dataclasses
        alt = dataclasses.replace(plan, public_band="front" if plan.public_band == "rear" else "rear")
        best = _search(alt, sqft)
    if best is None:
        raise RuntimeError("could not find a valid layout for this program; try more sq ft "
                           "or fewer rooms")
    lay = best[1]
    lay["meta"] = {"generator": "fpgen strip-subdivision", "seed": seed, "beds": beds,
                   "baths": baths, "requested_sqft": sqft,
                   "actual_sqft": lay["footprint"]["width"] * lay["footprint"]["depth"]}
    return lay


def _search(plan: Plan, sqft: float):
    best = None
    offices = [False, True] if sqft >= 1900 else [False]
    kds = [False, True] if sqft < 1300 else [False]
    aspects = [round(0.9 + 0.05 * i, 2) for i in range(31)]  # 0.90 .. 2.40
    for aspect in aspects:
        D = round(math.sqrt(sqft / aspect))
        W = round(sqft / D)
        for Dr in [10.5 + 0.5 * i for i in range(12)]:
            for n_over, office, kdc in [(n, o, k) for n in range(0, 6) for o in offices for k in kds]:
                lay = _build(plan, float(W), float(D), Dr, n_over, office, kdc)
                if lay is None:
                    continue
                sc = _score(lay, plan, W / D)
                if best is None or sc < best[0]:
                    if validate_layout(_copy(lay)).ok:
                        best = (sc, lay)
    return best


def _copy(obj):
    import copy
    return copy.deepcopy(obj)
