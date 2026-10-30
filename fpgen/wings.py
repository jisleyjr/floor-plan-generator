"""Attached garages and L-shaped footprints.

The house body is produced by the rectangular generator (generate.py). A *wing*
column is then attached to the hallway end of the body:

* ``shape="rect"`` + garage: the wing runs the full depth of the house (garage at
  the front, unconditioned garage storage behind it) -> the footprint stays a rectangle.
* ``shape="L"``: the wing is shallower than the house, flush with the front (or
  projecting forward when the house is shallow), so the footprint becomes an L.
  The wing is the garage when ``garage`` > 0, otherwise a family room.

Because the wing sits at the end of the hallway, the garage man door opens into the
laundry (mudroom), kitchen or the hallway, never into a bedroom.
"""
from __future__ import annotations

import copy

from .geometry import Footprint, shared_segment, side_segment
from .validate import validate_layout

GARAGE_SIZES = {1: (12.0, 22.0), 2: (22.0, 22.0), 3: (32.0, 22.0)}
GARAGE_DOORS = {1: [(9.0, 1.5)], 2: [(16.0, 3.0)], 3: [(16.0, 2.0), (9.0, 21.0)]}  # (width, offset)
MAN_DOOR_PREF = ["laundry", "mudroom", "kitchen", "hallway", "entry", "foyer"]
FAMILY_PREF = ["living", "kitchen", "dining", "hallway"]
WIN_W = {"bedroom": 3.0, "living": 5.0, "dining": 4.0, "kitchen": 3.5, "bathroom": 2.0,
         "half_bath": 2.0, "laundry": 2.5, "office": 3.0, "family": 5.0, "garage": 3.0}


def mirror_x(lay: dict) -> dict:
    """Mirror a rectangular layout east-west (keeps doors/windows consistent)."""
    lay = copy.deepcopy(lay)
    W = float(lay["footprint"]["width"])
    by = {r["id"]: r for r in lay["rooms"]}
    for d in lay["doors"]:
        if d["to"] == "exterior":
            room = by[d["from"]]
            seg = side_segment(room, d["wall"])
            d["wall"] = {"E": "W", "W": "E"}.get(d["wall"], d["wall"])
        else:
            seg = shared_segment(by[d["from"]], by[d["to"]])
        if seg.orient == "h":
            if "offset" in d:
                d["offset"] = round(seg.length - d["offset"] - d["width"], 3)
            if d.get("type", "door") == "door":
                d["hinge"] = "end" if d.get("hinge", "start") == "start" else "start"
    for w in lay["windows"]:
        room = by[w["room"]]
        if w["wall"] in "SN":
            if "offset" in w:
                w["offset"] = round(room["w"] - w["offset"] - w["width"], 3)
        else:
            w["wall"] = {"E": "W", "W": "E"}[w["wall"]]
    for r in lay["rooms"]:
        r["x"] = W - r["x"] - r["w"]
    return lay


def _place_windows(lay, FP, rooms):
    by_side_doors = {}
    for d in lay["doors"]:
        if d["to"] == "exterior":
            by_side_doors.setdefault((d["from"], d["wall"]), []).append(d)
    have = {w["room"] for w in lay["windows"]}
    for r in rooms:
        t = r["type"]
        if t not in WIN_W or r["id"] in have:
            continue
        ww = WIN_W[t]
        sides = FP.exterior_sides(r)
        groups = [[sd] for sd in sides] if t == "family" else [sides]  # family room: a window per wall
        for grp in groups:
            _one_window(lay, FP, r, grp, ww, by_side_doors)


def _one_window(lay, FP, r, sides, ww, by_side_doors):
    best = None
    for side in sides:
        seg = side_segment(r, side)
        for a, b in FP.exterior_parts(r, side):
            free = [(a + 1.0, b - 1.0)]
            for d in by_side_doors.get((r["id"], side), []):
                q0 = seg.s0 + d.get("offset", (seg.length - d["width"]) / 2)
                q1 = q0 + d["width"]
                nf = []
                for f0, f1 in free:
                    if q1 + 1 <= f0 or q0 - 1 >= f1:
                        nf.append((f0, f1))
                    else:
                        if q0 - 1 > f0:
                            nf.append((f0, q0 - 1))
                        if q1 + 1 < f1:
                            nf.append((q1 + 1, f1))
                free = nf
            for f0, f1 in free:
                if f1 - f0 >= ww and (best is None or f1 - f0 > best[0]):
                    best = (f1 - f0, side, (f0 + f1) / 2 - ww / 2 - seg.s0)
    if best:
        lay["windows"].append({"room": r["id"], "wall": best[1], "width": ww,
                               "offset": round(best[2], 3)})


def generate_with_wing(beds, baths, sqft, seed, laundry, garage, shape, garage_side):
    from .generate import _generate_house, snap

    family = garage == 0  # an L without a garage gets a family-room wing
    house_sqft = sqft
    fam_w = 14.0 if sqft < 2000 else 16.0
    if family:
        d_est = (sqft * 0.85 / 1.55) ** 0.5
        house_sqft = sqft - fam_w * max(12.0, min(20.0, d_est - 9))
    for _attempt in range(2):
        try:
            house = _generate_house(beds, baths, house_sqft, seed, laundry, laundry_last=bool(garage))
        except RuntimeError:
            raise RuntimeError(f"{sqft:g} sq ft is too small for this program as an L-shaped house "
                               "(the family-room wing takes part of the area); use --shape rect, "
                               "add a garage, or more sq ft") from None
        W, D = float(house["footprint"]["width"]), float(house["footprint"]["depth"])
        hall = next(r for r in house["rooms"] if r["type"] == "hallway")
        hall_end = "E" if abs(hall["x"] + hall["w"] - W) < 1e-6 else "W"
        hall_top = hall["y"] + hall["d"]
        if family:
            wd = snap(min(max(D - 9, hall_top + 2, 12.0), 22.0))
            area = fam_w * wd
            # keep the living area close to the request: re-run once with the real wing size
            if abs(house_sqft + area - sqft) > 60 and _attempt == 0:
                house_sqft = sqft - area
                continue
        break
    # default side: garage at the hallway end (man door into laundry/hall);
    # family-room wing next to the living room (great-room style)
    living_end = "W" if hall_end == "E" else "E"
    anchor = living_end if family else hall_end   # which end of the body the wing must touch
    want = {"left": "W", "right": "E"}.get(garage_side, anchor)
    if want != anchor:
        house = mirror_x(house)
        hall = next(r for r in house["rooms"] if r["type"] == "hallway")

    if garage:
        gw, gd = GARAGE_SIZES[garage]
        gd = max(gd, hall_top)  # the wing must reach past the hallway so the man door can land there
    else:
        gw, gd = fam_w, wd
    if shape == "rect":
        if D < 20:
            raise RuntimeError("house too shallow for a full-depth garage; use --shape L")
        wd, y0 = D, 0.0
    else:
        wd = gd
        y0 = min(0.0, D - 6.0 - wd)  # keep a >= 6' notch at the back; project forward if needed
    dx = gw if want == "W" else 0.0
    dy = -y0
    lay = copy.deepcopy(house)
    for r in lay["rooms"]:
        r["x"] = r["x"] + dx
        r["y"] = r["y"] + dy
    wx = 0.0 if want == "W" else W
    used = {r["id"] for r in lay["rooms"]}
    new_rooms = []
    if garage:
        if shape == "L":
            g_depth = gd
        elif D - gd >= 5:
            g_depth = max(gd, D - 10.0)  # deep house: deeper garage (workbench room), storage <= 10'
        else:
            g_depth = D
        g = {"id": "garage", "name": "Garage", "type": "garage", "x": wx, "y": 0.0,
             "w": gw, "d": g_depth, "conditioned": False, "cars": garage}
        new_rooms.append(g)
        if shape == "rect" and D - g_depth >= 5:
            new_rooms.append({"id": "garage-storage", "name": "Storage", "type": "storage",
                              "x": wx, "y": g_depth, "w": gw, "d": D - g_depth, "conditioned": False})
    else:
        new_rooms.append({"id": "family-room", "name": "Family Room", "type": "family",
                          "x": wx, "y": 0.0, "w": gw, "d": wd, "target": {"area": round(gw * wd)}})
    assert not ({r["id"] for r in new_rooms} & used)
    lay["rooms"].extend(new_rooms)

    if shape == "rect":
        FPj = {"width": W + gw, "depth": D}
    else:
        FPj = {"width": W + gw, "depth": D + dy,
               "rects": [{"x": dx, "y": dy, "w": W, "d": D}, {"x": wx, "y": 0.0, "w": gw, "d": wd}]}
    lay["footprint"] = FPj
    FP, _ = Footprint.from_json(FPj)
    by = {r["id"]: r for r in lay["rooms"]}

    # drop exterior doors / windows that the wing has turned into interior walls
    def still_exterior(room_id, wall, off, width):
        r = by[room_id]
        seg = side_segment(r, wall)
        p0 = seg.s0 + (off if off is not None else (seg.length - width) / 2)
        return any(a - 1e-6 <= p0 and p0 + width <= b + 1e-6 for a, b in FP.exterior_parts(r, wall))
    lay["doors"] = [d for d in lay["doors"] if d["to"] != "exterior"
                    or still_exterior(d["from"], d["wall"], d.get("offset"), d["width"])]
    lay["windows"] = [w for w in lay["windows"]
                      if still_exterior(w["room"], w["wall"], w.get("offset"), w["width"])]

    wing = new_rooms[0]
    # connection between the wing and the house
    pref = MAN_DOOR_PREF if garage else FAMILY_PREF
    cands = []
    for r in lay["rooms"]:
        if r in new_rooms:
            continue
        seg = shared_segment(wing, r)
        if seg and seg.length >= 3.5 and r["type"] in pref:
            cands.append((pref.index(r["type"]), -seg.length, r, seg))
    if not cands:
        raise RuntimeError("could not connect the wing to the house")
    cands.sort(key=lambda c: (c[0], c[1]))
    _, _, target, seg = cands[0]
    if garage:
        w = 3.0 if seg.length >= 4 else 2.67
        # swing into the house, except into a hallway (keep the hall clear) -> swing into the garage
        lay["doors"].append({"from": wing["id"], "to": target["id"], "type": "door", "width": w,
                             "offset": round((seg.length - w) / 2, 3), "hinge": "start",
                             "swing_into": wing["id"] if target["type"] == "hallway" else target["id"]})
        for w_, off in GARAGE_DOORS[garage]:
            lay["doors"].append({"from": "garage", "to": "exterior", "wall": "S", "type": "garage",
                                 "width": w_, "offset": off})
        outer = "W" if want == "W" else "E"
        glen = wing["d"]
        if glen >= 9:
            lay["doors"].append({"from": "garage", "to": "exterior", "wall": outer, "type": "door",
                                 "width": 3.0, "offset": glen - 4.5, "hinge": "end"})
        if len(new_rooms) > 1:
            lay["doors"].append({"from": "garage", "to": "garage-storage", "type": "door", "width": 2.67,
                                 "offset": round((gw - 2.67) / 2, 3), "hinge": "start",
                                 "swing_into": "garage-storage"})
    else:
        lay["doors"].append({"from": target["id"], "to": wing["id"], "type": "opening",
                             "width": round(min(seg.length - 1.0, 8.0) if seg.length > 6 else seg.length - 0.5, 3)})
    _place_windows(lay, FP, lay["rooms"])

    lay["title"] = f"{beds} Bedroom / {baths:g} Bath" + (f" + {garage}-Car Garage" if garage else "")
    rep = validate_layout(copy.deepcopy(lay))
    if not rep.ok:
        raise RuntimeError("internal error: wing layout invalid:\n" + rep.format())
    lay["meta"] = {"generator": "fpgen strip-subdivision + wing", "seed": seed, "beds": beds,
                   "baths": baths, "requested_sqft": sqft, "shape": shape, "garage_cars": garage,
                   "garage_side": "left" if want == "W" else "right",
                   "living_sqft": rep.living_sqft, "garage_sqft": rep.garage_sqft,
                   "garage_storage_sqft": rep.other_unconditioned_sqft,
                   "total_footprint_sqft": FP.area}
    return lay
