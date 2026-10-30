"""Small geometry helpers shared by the generator, validator and renderer.

Coordinate system (feet): origin at the south-west (bottom-left) corner of the
footprint, x grows east (right), y grows north (up). The front of the house is
the south wall (y = 0).
"""
from __future__ import annotations

from dataclasses import dataclass

TOL = 0.02  # feet; coordinates closer than this are treated as equal

# Room types that count as circulation (rooms you can walk *through*).
CIRCULATION = {"hallway", "living", "family", "entry", "foyer"}
SIDES = ("S", "N", "W", "E")


@dataclass(frozen=True)
class Seg:
    """Axis aligned wall segment. orient 'h': y=c, x in [s0,s1]; 'v': x=c, y in [s0,s1]."""
    orient: str
    c: float
    s0: float
    s1: float

    @property
    def length(self) -> float:
        return self.s1 - self.s0


def rect(room: dict) -> tuple[float, float, float, float]:
    """Return (x0, y0, x1, y1) of a room dict."""
    x, y = float(room["x"]), float(room["y"])
    return x, y, x + float(room["w"]), y + float(room["d"])


def overlap_area(a: dict, b: dict) -> tuple[float, tuple]:
    ax0, ay0, ax1, ay1 = rect(a)
    bx0, by0, bx1, by1 = rect(b)
    w = min(ax1, bx1) - max(ax0, bx0)
    h = min(ay1, by1) - max(ay0, by0)
    if w <= TOL or h <= TOL:
        return 0.0, ()
    return w * h, (max(ax0, bx0), max(ay0, by0), min(ax1, bx1), min(ay1, by1))


def shared_segment(a: dict, b: dict) -> Seg | None:
    """The wall segment two rectangular rooms share, or None if they do not touch along a wall."""
    ax0, ay0, ax1, ay1 = rect(a)
    bx0, by0, bx1, by1 = rect(b)
    for xa, xb in ((ax1, bx0), (bx1, ax0)):
        if abs(xa - xb) < TOL:
            s0, s1 = max(ay0, by0), min(ay1, by1)
            if s1 - s0 > TOL:
                return Seg("v", xa, s0, s1)
    for ya, yb in ((ay1, by0), (by1, ay0)):
        if abs(ya - yb) < TOL:
            s0, s1 = max(ax0, bx0), min(ax1, bx1)
            if s1 - s0 > TOL:
                return Seg("h", ya, s0, s1)
    return None


def exterior_sides(room: dict, W: float, D: float) -> list[str]:
    x0, y0, x1, y1 = rect(room)
    out = []
    if abs(y0) < TOL:
        out.append("S")
    if abs(y1 - D) < TOL:
        out.append("N")
    if abs(x0) < TOL:
        out.append("W")
    if abs(x1 - W) < TOL:
        out.append("E")
    return out


def side_segment(room: dict, side: str) -> Seg:
    """The full edge of a room on the given side (S/N/W/E)."""
    x0, y0, x1, y1 = rect(room)
    return {
        "S": Seg("h", y0, x0, x1),
        "N": Seg("h", y1, x0, x1),
        "W": Seg("v", x0, y0, y1),
        "E": Seg("v", x1, y0, y1),
    }[side]


def side_of(room: dict, seg: Seg) -> int:
    """+1 if the room lies on the positive side (north/east) of the segment line, else -1."""
    x0, y0, x1, y1 = rect(room)
    if seg.orient == "h":
        return 1 if (y0 + y1) / 2 > seg.c else -1
    return 1 if (x0 + x1) / 2 > seg.c else -1


def fmt_ft(v: float) -> str:
    """12.5 -> 12'-6\"   12.0 -> 12'"""
    total_in = int(round(v * 12))
    ft, inch = divmod(total_in, 12)
    return f"{ft}'" if inch == 0 else f"{ft}'-{inch}\""


# --------------------------------------------------------------------------- footprints
_NORMAL = {"S": (0, -1), "N": (0, 1), "W": (-1, 0), "E": (1, 0)}


class Footprint:
    """A footprint made of non-overlapping axis-aligned rectangles (x0, y0, x1, y1).

    JSON forms accepted by ``from_json``:
      {"width": W, "depth": D}                                  plain rectangle at the origin
      {"rects": [{"x":0,"y":0,"w":50,"d":30}, ...]}             union of rectangles (L, T, U ...)
      {"polygon": [[0,0],[62,0],[62,22],[50,22],[50,30],[0,30]]}  rectilinear polygon
    ``width``/``depth`` next to ``rects``/``polygon`` are informational only.
    """

    def __init__(self, rects):
        self.rects = [tuple(float(v) for v in r) for r in rects]
        self.W = max(r[2] for r in self.rects)
        self.D = max(r[3] for r in self.rects)
        self.minx = min(r[0] for r in self.rects)
        self.miny = min(r[1] for r in self.rects)

    @property
    def is_rect(self) -> bool:
        return len(self.rects) == 1

    @property
    def area(self) -> float:
        return sum((r[2] - r[0]) * (r[3] - r[1]) for r in self.rects)

    @classmethod
    def from_json(cls, fp) -> tuple["Footprint | None", list[str]]:
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        if not isinstance(fp, dict):
            return None, ["footprint must be an object"]
        if "rects" in fp:
            rs = fp["rects"]
            if not isinstance(rs, list) or not rs:
                return None, ["footprint.rects must be a non-empty list of {x, y, w, d}"]
            out = []
            for i, r in enumerate(rs):
                if not isinstance(r, dict) or not all(num(r.get(k)) for k in ("x", "y", "w", "d")):
                    return None, [f"footprint.rects[{i}] needs numeric x, y, w, d"]
                if r["w"] <= 0 or r["d"] <= 0:
                    return None, [f"footprint.rects[{i}] has non-positive size"]
                out.append((r["x"], r["y"], r["x"] + r["w"], r["y"] + r["d"]))
            errs = []
            for i in range(len(out)):
                for j in range(i + 1, len(out)):
                    a, b = out[i], out[j]
                    if min(a[2], b[2]) - max(a[0], b[0]) > TOL and min(a[3], b[3]) - max(a[1], b[1]) > TOL:
                        errs.append(f"footprint.rects[{i}] and footprint.rects[{j}] overlap")
            if errs:
                return None, errs
            f = cls(out)
            if f.minx < -TOL or f.miny < -TOL:
                return None, ["footprint must start at (0,0): no negative coordinates"]
            return f, []
        if "polygon" in fp:
            pts = fp["polygon"]
            if (not isinstance(pts, list) or len(pts) < 4 or
                    not all(isinstance(p, (list, tuple)) and len(p) == 2 and num(p[0]) and num(p[1]) for p in pts)):
                return None, ["footprint.polygon must be a list of at least 4 [x, y] points"]
            n = len(pts)
            for i in range(n):
                (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % n]
                if abs(x0 - x1) > TOL and abs(y0 - y1) > TOL:
                    return None, [f"footprint.polygon edge {i} ({x0},{y0})->({x1},{y1}) is not "
                                  "horizontal or vertical (only rectilinear footprints are supported)"]
            xs = sorted({float(p[0]) for p in pts})
            ys = sorted({float(p[1]) for p in pts})

            def inside(px, py):
                c = False
                for i in range(n):
                    (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % n]
                    if (y0 > py) != (y1 > py) and px < x0 + (py - y0) * (x1 - x0) / (y1 - y0):
                        c = not c
                return c
            out = []
            for j in range(len(ys) - 1):  # merge cells row by row into strips
                run = None
                for i in range(len(xs) - 1):
                    if inside((xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2):
                        run = (run[0], xs[i + 1]) if run else (xs[i], xs[i + 1])
                    elif run:
                        out.append((run[0], ys[j], run[1], ys[j + 1]))
                        run = None
                if run:
                    out.append((run[0], ys[j], run[1], ys[j + 1]))
            if not out:
                return None, ["footprint.polygon encloses no area"]
            f = cls(out)
            if f.minx < -TOL or f.miny < -TOL:
                return None, ["footprint must start at (0,0): no negative coordinates"]
            return f, []
        if num(fp.get("width")) and num(fp.get("depth")):
            if fp["width"] <= 0 or fp["depth"] <= 0:
                return None, ["footprint width/depth must be positive"]
            return cls([(0, 0, fp["width"], fp["depth"])]), []
        return None, ["footprint needs 'width' and 'depth' (rectangle), 'rects' or 'polygon'"]

    def contains(self, x, y, eps=1e-9) -> bool:
        return any(r[0] - eps <= x <= r[2] + eps and r[1] - eps <= y <= r[3] + eps for r in self.rects)

    def covered(self, room) -> float:
        """Area of the room that lies inside the footprint."""
        x0, y0, x1, y1 = rect(room)
        tot = 0.0
        for r in self.rects:
            w = min(x1, r[2]) - max(x0, r[0])
            h = min(y1, r[3]) - max(y0, r[1])
            if w > 0 and h > 0:
                tot += w * h
        return tot

    def exterior_parts(self, room, side) -> list[tuple[float, float]]:
        """Sub-intervals of a room side that lie on the outside boundary of the footprint."""
        seg = side_segment(room, side)
        brk = {seg.s0, seg.s1}
        for r in self.rects:
            for v in ((r[0], r[2]) if seg.orient == "h" else (r[1], r[3])):
                if seg.s0 + TOL < v < seg.s1 - TOL:
                    brk.add(v)
        brk = sorted(brk)
        nx, ny = _NORMAL[side]
        parts = []
        for a, b in zip(brk, brk[1:]):
            m = (a + b) / 2
            px, py = (m, seg.c) if seg.orient == "h" else (seg.c, m)
            if not self.contains(px + nx * 0.05, py + ny * 0.05):
                if parts and abs(parts[-1][1] - a) < TOL:
                    parts[-1] = (parts[-1][0], b)
                else:
                    parts.append((a, b))
        return parts

    def exterior_sides(self, room) -> list[str]:
        return [s for s in SIDES if self.exterior_parts(room, s)]

    def to_json(self) -> dict:
        if self.is_rect and self.minx == 0 and self.miny == 0:
            return {"width": self.W, "depth": self.D}
        return {"width": self.W, "depth": self.D,
                "rects": [{"x": r[0], "y": r[1], "w": r[2] - r[0], "d": r[3] - r[1]} for r in self.rects]}
