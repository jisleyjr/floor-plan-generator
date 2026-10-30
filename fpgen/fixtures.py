"""Simple plumbing / kitchen fixtures so plans read like real drawings.

Everything is placed greedily against walls and never on top of a door swing,
a door/opening clearance zone, or another fixture. A fixture that cannot be
placed is simply skipped (the plan is still valid without it).
"""
from __future__ import annotations

from matplotlib.patches import Circle, Ellipse, FancyBboxPatch, Rectangle

from .geometry import rect

INK = "black"
LW = 0.7


def _hit(b, obstacles, m=0.05):
    x0, y0, x1, y1 = b
    return any(x0 < o[2] - m and x1 > o[0] + m and y0 < o[3] - m and y1 > o[1] + m for o in obstacles)


class Placer:
    def __init__(self, room, FP, te, ti, obstacles):
        self.r = room
        x0, y0, x1, y1 = rect(room)
        ext = set(FP.exterior_sides(room))
        f = lambda s: te if s in ext else ti / 2
        # inner faces
        self.ix0, self.iy0 = x0 + f("W"), y0 + f("S")
        self.ix1, self.iy1 = x1 - f("E"), y1 - f("N")
        self.obs = list(obstacles)

    def box_on(self, side, along, length, depth):
        """Box flush with a wall. along = distance from the wall's start (W or S end)."""
        if side in "SN":
            a0 = self.ix0 + along
            b = (a0, self.iy0, a0 + length, self.iy0 + depth) if side == "S" else \
                (a0, self.iy1 - depth, a0 + length, self.iy1)
        else:
            a0 = self.iy0 + along
            b = (self.ix0, a0, self.ix0 + depth, a0 + length) if side == "W" else \
                (self.ix1 - depth, a0, self.ix1, a0 + length)
        return b

    def wall_len(self, side):
        return (self.ix1 - self.ix0) if side in "SN" else (self.iy1 - self.iy0)

    def inside(self, b):
        return b[0] >= self.ix0 - 1e-6 and b[2] <= self.ix1 + 1e-6 and b[1] >= self.iy0 - 1e-6 and b[3] <= self.iy1 + 1e-6

    def find(self, length, depth, sides=("N", "S", "E", "W"), step=0.25, from_end=True):
        for side in sides:
            L = self.wall_len(side)
            if L < length - 1e-6:
                continue
            n = int((L - length) / step + 1e-6)
            order = []
            for k in range(n + 1):
                order.append(k * step)
                if from_end:
                    order.append(L - length - k * step)
            for along in order:
                b = self.box_on(side, along, length, depth)
                if self.inside(b) and not _hit(b, self.obs):
                    self.obs.append(b)
                    return side, b
        return None


def _rect(ax, b, lw=LW, fc="white", z=4):
    ax.add_patch(Rectangle((b[0], b[1]), b[2] - b[0], b[3] - b[1], fc=fc, ec=INK, lw=lw, zorder=z))


def _inset(b, d):
    return (b[0] + d, b[1] + d, b[2] - d, b[3] - d)


def _wall_dir(side):
    # unit vector pointing from the wall into the room
    return {"S": (0, 1), "N": (0, -1), "W": (1, 0), "E": (-1, 0)}[side]


def draw_tub(ax, side, b):
    _rect(ax, b)
    ib = _inset(b, 0.25)
    ax.add_patch(FancyBboxPatch((ib[0], ib[1]), ib[2] - ib[0], ib[3] - ib[1],
                                boxstyle="round,pad=0,rounding_size=0.5", fc="white", ec=INK, lw=0.5, zorder=4))


def draw_toilet(ax, side, b):
    dx, dy = _wall_dir(side)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    if side in "SN":
        tank_d = 0.7
        y_w = b[1] if side == "S" else b[3]
        tank = (cx - 0.85, min(y_w, y_w + dy * tank_d), cx + 0.85, max(y_w, y_w + dy * tank_d))
        bowl_c = (cx, y_w + dy * (tank_d + 0.8))
        ell = Ellipse(bowl_c, 1.25, 1.7, fc="white", ec=INK, lw=LW, zorder=4)
    else:
        tank_d = 0.7
        x_w = b[0] if side == "W" else b[2]
        tank = (min(x_w, x_w + dx * tank_d), cy - 0.85, max(x_w, x_w + dx * tank_d), cy + 0.85)
        bowl_c = (x_w + dx * (tank_d + 0.8), cy)
        ell = Ellipse(bowl_c, 1.7, 1.25, fc="white", ec=INK, lw=LW, zorder=4)
    ax.add_patch(ell)
    _rect(ax, tank, z=5)


def draw_vanity(ax, side, b):
    _rect(ax, b)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    w, h = (1.4, 1.0) if side in "SN" else (1.0, 1.4)
    ax.add_patch(Ellipse((cx, cy), w, h, fc="white", ec=INK, lw=0.5, zorder=5))


def draw_counter(ax, side, b):
    _rect(ax, b, lw=0.8)


def draw_sink(ax, side, b):
    # double bowl inside a counter box
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    for k in (-1, 1):
        if side in "SN":
            ax.add_patch(Rectangle((cx + (0 if k > 0 else -1.3) + 0.05 * k, cy - 0.6), 1.25, 1.2,
                                   fc="white", ec=INK, lw=0.5, zorder=5))
        else:
            ax.add_patch(Rectangle((cx - 0.6, cy + (0 if k > 0 else -1.3) + 0.05 * k), 1.2, 1.25,
                                   fc="white", ec=INK, lw=0.5, zorder=5))


def draw_range(ax, side, b):
    _rect(ax, b, z=5)
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    for i in (-1, 1):
        for j in (-1, 1):
            ax.add_patch(Circle((cx + i * 0.6, cy + j * 0.5), 0.33, fc="white", ec=INK, lw=0.5, zorder=6))


def draw_box_label(ax, b, text, size=5):
    _rect(ax, b, z=5)
    ax.text((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, text, ha="center", va="center", fontsize=size, zorder=6)


def draw_fixtures(ax, layout, rep, te, ti, swings):
    FP = rep.footprint
    # clearance boxes in front of every door/opening (both sides)
    clear = []
    for d in rep.doors:
        seg = d["seg"]
        c = 2.5 if d["kind"] == "opening" else 1.5
        if seg.orient == "h":
            clear.append((d["p0"] - 0.2, seg.c - c, d["p1"] + 0.2, seg.c + c))
        else:
            clear.append((seg.c - c, d["p0"] - 0.2, seg.c + c, d["p1"] + 0.2))
    base = clear + list(swings)
    placed = []
    for r in layout["rooms"]:
        t = r["type"]
        if t not in ("bathroom", "half_bath", "kitchen", "laundry"):
            continue
        P = Placer(r, FP, te, ti, base)
        if t == "bathroom":
            short = sorted("NSEW", key=P.wall_len)
            # tub across a short wall when the room is ~5' wide, else along any wall
            got = None
            for side in short:
                L = P.wall_len(side)
                if 4.9 <= L <= 6.6:
                    got = P.find(L, 2.5, sides=(side,))
                    if got:
                        break
            if not got:
                got = P.find(5.0, 2.5, sides=short[::-1])
            if got:
                draw_tub(ax, *got)
            g = P.find(3.0, 1.75) or P.find(2.25, 1.75)
            if g:
                draw_vanity(ax, *g)
            g = P.find(1.9, 2.5, from_end=True)
            if g:
                draw_toilet(ax, *g)
        elif t == "half_bath":
            g = P.find(1.75, 1.5)
            if g:
                draw_vanity(ax, *g)
            g = P.find(1.9, 2.5)
            if g:
                draw_toilet(ax, *g)
        elif t == "laundry":
            g = P.find(4.5, 2.25) or P.find(4.5, 2.25, step=0.1)
            if g:
                side, b = g
                if side in "SN":
                    halves = [(b[0], b[1], b[0] + 2.25, b[3]), (b[0] + 2.25, b[1], b[2], b[3])]
                else:
                    halves = [(b[0], b[1], b[2], b[1] + 2.25), (b[0], b[1] + 2.25, b[2], b[3])]
                draw_box_label(ax, halves[0], "W")
                draw_box_label(ax, halves[1], "D")
        elif t == "kitchen":
            ext = FP.exterior_sides(r)
            order = sorted("NSEW", key=lambda s: (s not in ext, -P.wall_len(s)))
            runs = []
            for side in order:
                L = P.wall_len(side)
                for length in [L - k * 0.5 for k in range(int(L * 2))]:
                    if length < 5.0 or length > 14:
                        continue
                    g = P.find(length, 2.0, sides=(side,), step=0.5)
                    if g:
                        runs.append(g)
                        break
                if len(runs) == 2:
                    break
            for side, b in runs:
                draw_counter(ax, side, b)
            if runs:
                side, b = runs[0]
                draw_sink(ax, side, b)
            if len(runs) > 1:
                side, b = runs[1]
                cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                rb = (cx - 1.25, b[1], cx + 1.25, b[3]) if side in "SN" else (b[0], cy - 1.25, b[2], cy + 1.25)
                draw_range(ax, side, rb)
            # refrigerator
            g = P.find(3.0, 2.5)
            if g:
                draw_box_label(ax, g[1], "REF")
        placed += P.obs[len(base):]
    return placed
