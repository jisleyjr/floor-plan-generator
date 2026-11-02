"""Render a validated layout to clean black-on-white PNG and SVG with matplotlib."""
from __future__ import annotations

from collections import defaultdict

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.font_manager import FontProperties  # noqa: E402
from matplotlib.patches import Arc, Polygon, Rectangle  # noqa: E402
from matplotlib.textpath import TextPath  # noqa: E402

from .geometry import TOL, Footprint, fmt_ft, rect, side_segment  # noqa: E402
from .validate import validate_layout  # noqa: E402
from .fixtures import draw_fixtures  # noqa: E402

SCALE = 0.2          # inches of paper per foot
MARGIN_LEFT = 7.0
MARGIN_RIGHT = 4.0
MARGIN_TOP = 7.0
MARGIN_BOTTOM = 13.0
MAX_RENDER_PIXELS = 25_000_000
PT = SCALE * 72      # points per foot
TE = 0.5             # exterior wall thickness (ft), drawn inward from the footprint line
TI = 0.375           # interior wall thickness (ft), centred on the room boundary
INK = "black"
MINOR = {"closet", "storage", "pantry", "walk_in_closet"}
ABBR = {"Closet": "CL", "Linen": "LIN", "Walk-in Closet": "W.I.C.", "Storage": "STOR",
        "Laundry": "LAUN", "Pantry": "PAN", "Powder Room": "PWDR", "Hall": "HALL"}

_FONT = FontProperties(family="DejaVu Sans")
_FONT_B = FontProperties(family="DejaVu Sans", weight="bold")


def _text_wh(s: str, size: float, bold=False):
    ext = TextPath((0, 0), s, size=size, prop=_FONT_B if bold else _FONT).get_extents()
    return ext.width, size * 1.15


def _union(iv):
    iv = sorted(iv)
    out = []
    for a, b in iv:
        if out and a <= out[-1][1] + TOL:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _subtract(iv, gaps):
    """iv: [a,b] list; returns list of (a, b, cut_at_a, cut_at_b)."""
    res = [(a, b, False, False) for a, b in iv]
    for g0, g1 in gaps:
        nxt = []
        for a, b, ca, cb in res:
            if g1 <= a + TOL or g0 >= b - TOL:
                nxt.append((a, b, ca, cb))
                continue
            if g0 > a + TOL:
                nxt.append((a, g0, ca, True))
            if g1 < b - TOL:
                nxt.append((g1, b, True, cb))
        res = nxt
    return res


def _side_of_seg(seg, W, D):
    if seg.orient == "h":
        return "S" if abs(seg.c) < TOL else ("N" if abs(seg.c - D) < TOL else None)
    return "W" if abs(seg.c) < TOL else ("E" if abs(seg.c - W) < TOL else None)


class RenderTooLarge(ValueError):
    """The drawing would be larger than ``MAX_RENDER_PIXELS``."""


def render_pixel_count(width_ft: float, depth_ft: float, dpi: int) -> float:
    """Pixel count of the figure ``render_layout`` would allocate."""
    width_in = (width_ft + MARGIN_LEFT + MARGIN_RIGHT) * SCALE
    height_in = (depth_ft + MARGIN_TOP + MARGIN_BOTTOM) * SCALE
    return width_in * dpi * height_in * dpi


def ensure_render_budget(layout: dict, dpi: int) -> None:
    """Raise before a figure is allocated when the drawing is too large."""
    footprint, _errors = Footprint.from_json(layout.get("footprint") if isinstance(layout, dict) else None)
    if footprint is None or isinstance(dpi, bool) or not isinstance(dpi, int):
        return
    count = render_pixel_count(footprint.W, footprint.D, dpi)
    if count > MAX_RENDER_PIXELS:
        raise RenderTooLarge(
            f"Rendering this plan at {dpi} dpi would be {count:,.0f} pixels, "
            f"above the limit of {MAX_RENDER_PIXELS:,.0f}. Lower the dpi or use a smaller plan."
        )


def render_layout(layout: dict, out_base: str, dpi: int = 170, title: str | None = None,
                  formats=("png", "svg"), fixtures: bool = True) -> list[str]:
    rep = validate_layout(layout)
    if not rep.ok:
        raise ValueError(rep.format())
    ensure_render_budget(layout, dpi)
    FP = rep.footprint
    W, D = FP.W, FP.D
    rooms = layout["rooms"]
    ml, mr, mt, mb = MARGIN_LEFT, MARGIN_RIGHT, MARGIN_TOP, MARGIN_BOTTOM
    fig = plt.figure(figsize=((W + ml + mr) * SCALE, (D + mt + mb) * SCALE))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(-ml, W + mr)
    ax.set_ylim(-mb, D + mt)
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor("white")

    def box(x0, y0, x1, y1, **kw):
        ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, facecolor=kw.get("fc", INK),
                               edgecolor=kw.get("ec", "none"), lw=kw.get("lw", 0), zorder=kw.get("z", 3)))

    # ------------------------------------------------------------------ exterior walls
    # Each room side is split into exterior parts (thick wall drawn inward, into the room)
    # and interior parts (thin wall centred on the line). Works for any union-of-rectangles footprint.
    gaps = defaultdict(list)
    for d in rep.doors:
        if d["exterior"]:
            gaps[(d["seg"].orient, round(d["seg"].c, 3))].append((d["p0"], d["p1"]))
    for w in rep.windows:
        gaps[(w["seg"].orient, round(w["seg"].c, 3))].append((w["p0"], w["p1"]))
    lines = defaultdict(list)
    for r in rooms:
        for side in "SNWE":
            seg = side_segment(r, side)
            parts = FP.exterior_parts(r, side)
            inward = -1 if side in "NE" else 1
            # interior remainder of this side
            rest = _subtract([[seg.s0, seg.s1]], parts)
            for a, b, _, _ in rest:
                lines[(seg.orient, round(seg.c, 3))].append((a, b))
            for a, b in parts:
                # extend into reflex (inside) corners so the inner wall faces meet
                ea, eb = a, b
                for end, sgn in ((a, -1), (b, 1)):
                    px = end + sgn * TE / 2
                    q = seg.c + inward * TE / 2
                    pt = (px, q) if seg.orient == "h" else (q, px)
                    if FP.contains(*pt, eps=-1e-6):
                        if sgn < 0:
                            ea = a - TE
                        else:
                            eb = b + TE
                for a2, b2, ca, cb in _subtract([[ea, eb]], gaps[(seg.orient, round(seg.c, 3))]):
                    if seg.orient == "h":
                        box(a2, min(seg.c, seg.c + inward * TE), b2, max(seg.c, seg.c + inward * TE))
                    else:
                        box(min(seg.c, seg.c + inward * TE), a2, max(seg.c, seg.c + inward * TE), b2)
    # windows: three thin lines (outer face, glass, inner face)
    for w in rep.windows:
        seg, p0, p1 = w["seg"], w["p0"], w["p1"]
        inward = w["dir"]
        for k in (0.0, 0.5, 1.0):
            off = seg.c + inward * TE * k
            if seg.orient == "h":
                ax.plot([p0, p1], [off, off], color=INK, lw=0.7, zorder=4, solid_capstyle="butt")
            else:
                ax.plot([off, off], [p0, p1], color=INK, lw=0.7, zorder=4, solid_capstyle="butt")
        for p in (p0, p1):  # jamb ticks
            if seg.orient == "h":
                ax.plot([p, p], [seg.c, seg.c + inward * TE], color=INK, lw=0.7, zorder=4)
            else:
                ax.plot([seg.c, seg.c + inward * TE], [p, p], color=INK, lw=0.7, zorder=4)

    # ------------------------------------------------------------------ interior walls
    door_gaps = defaultdict(list)
    for d in rep.doors:
        if not d["exterior"]:
            door_gaps[(d["seg"].orient, round(d["seg"].c, 3))].append((d["p0"], d["p1"]))
    for (orient, c), iv in lines.items():
        for a, b, ca, cb in _subtract(_union(iv), door_gaps[(orient, c)]):
            a2 = a if ca else a - TI / 2
            b2 = b if cb else b + TI / 2
            if orient == "h":
                box(a2, c - TI / 2, b2, c + TI / 2)
            else:
                box(c - TI / 2, a2, c + TI / 2, b2)

    # ------------------------------------------------------------------ door symbols
    swings = []  # door swing boxes, used to keep labels clear of arcs
    for d in rep.doors:
        seg, kind = d["seg"], d["kind"]
        thick = TE if d["exterior"] else TI
        if kind == "opening":
            continue
        if kind == "garage":
            # overhead door: thin panel in the opening + dashed outline of the raised door
            dirn = d["dir"]
            face = seg.c + dirn * TE
            depth = min(7.0, 0.35 * d["width"] + 2.5)
            p0, p1 = d["p0"], d["p1"]
            if seg.orient == "h":
                box(p0, seg.c + dirn * 0.17 - 0.06, p1, seg.c + dirn * 0.17 + 0.06, fc="white", ec=INK, lw=0.6, z=5)
                ys = sorted((face, face + dirn * depth))
                ax.add_patch(Rectangle((p0, ys[0]), p1 - p0, ys[1] - ys[0], fill=False, ec=INK, lw=0.6,
                                       ls=(0, (4, 3)), zorder=5))
                swings.append((p0, ys[0], p1, ys[1]))
            else:
                box(seg.c + dirn * 0.17 - 0.06, p0, seg.c + dirn * 0.17 + 0.06, p1, fc="white", ec=INK, lw=0.6, z=5)
                xs = sorted((face, face + dirn * depth))
                ax.add_patch(Rectangle((xs[0], p0), xs[1] - xs[0], p1 - p0, fill=False, ec=INK, lw=0.6,
                                       ls=(0, (4, 3)), zorder=5))
                swings.append((xs[0], p0, xs[1], p1))
            continue
        if kind == "sliding":
            mid = (d["p0"] + d["p1"]) / 2
            for k, (a, b) in enumerate(((d["p0"], mid + 0.2), (mid - 0.2, d["p1"]))):
                off = seg.c + (-1 if k == 0 else 1) * TI / 4
                if seg.orient == "h":
                    box(a, off - 0.06, b, off + 0.06, fc="white", ec=INK, lw=0.6, z=5)
                else:
                    box(off - 0.06, a, off + 0.06, b, fc="white", ec=INK, lw=0.6, z=5)
            continue
        dirn = d["dir"]
        face = seg.c + dirn * (thick if d["exterior"] else thick / 2)
        if d["exterior"]:
            face = seg.c + dirn * thick
        h = d["hinge"]
        sgn = 1 if d["hinge_end"] == "start" else -1
        w = d["p1"] - d["p0"]
        if seg.orient == "h":
            hx, hy = h, face
            tip = (h, face + dirn * w)
            a_leaf = 90 if dirn > 0 else 270
            a_jamb = 0 if sgn > 0 else 180
        else:
            hx, hy = face, h
            tip = (face + dirn * w, h)
            a_leaf = 0 if dirn > 0 else 180
            a_jamb = 90 if sgn > 0 else 270
        swings.append((min(hx, tip[0], hx + (sgn * w if seg.orient == "h" else 0)),
                       min(hy, tip[1], hy + (sgn * w if seg.orient == "v" else 0)),
                       max(hx, tip[0], hx + (sgn * w if seg.orient == "h" else 0)),
                       max(hy, tip[1], hy + (sgn * w if seg.orient == "v" else 0))))
        ax.plot([hx, tip[0]], [hy, tip[1]], color=INK, lw=1.3, zorder=5, solid_capstyle="butt")
        t1, t2 = (a_leaf, a_jamb) if (a_jamb - a_leaf) % 360 == 90 else (a_jamb, a_leaf)
        ax.add_patch(Arc((hx, hy), 2 * w, 2 * w, theta1=t1, theta2=t2, color=INK, lw=0.6, zorder=5))

    if fixtures:
        # fixtures become obstacles for labels too
        swings = swings + draw_fixtures(ax, layout, rep, TE, TI, swings)

    # ------------------------------------------------------------------ room labels
    for r in rooms:
        x0, y0, x1, y1 = rect(r)
        name = r.get("label") or r["name"]
        dims = f"{fmt_ft(r['w'])} x {fmt_ft(r['d'])}"
        pad = 0.75
        aw, ah = (x1 - x0 - 2 * pad) * PT, (y1 - y0 - 2 * pad) * PT
        show_dims = r["type"] not in MINOR and r["type"] != "hallway"
        options = []
        for size in (10, 9, 8, 7, 6.5):
            if show_dims:
                options.append((name, dims, size, 0))
        for size in (9, 8, 7, 6.5, 6, 5.5):
            options.append((name, None, size, 0))
            options.append((name, None, size, 90))
        ab = ABBR.get(name)
        if ab:
            for size in (6.5, 6, 5.5, 5):
                options.append((ab, None, size, 0))
                options.append((ab, None, size, 90))
        cx0, cy0 = (x0 + x1) / 2, (y0 + y1) / 2
        cands = []
        for fx in (0, -0.12, 0.12, -0.22, 0.22, -0.3, 0.3):
            for fy in (0, -0.12, 0.12, -0.22, 0.22, -0.3, 0.3):
                cands.append((abs(fx) + abs(fy), cx0 + fx * (x1 - x0), cy0 + fy * (y1 - y0)))
        cands.sort()

        def clear_spot(lw_, lh_):
            for _, px, py in cands:
                bx0, by0, bx1, by1 = px - lw_ / 2, py - lh_ / 2, px + lw_ / 2, py + lh_ / 2
                if bx0 < x0 + 0.6 or bx1 > x1 - 0.6 or by0 < y0 + 0.6 or by1 > y1 - 0.6:
                    continue
                if any(bx0 < s2 and bx1 > s0 and by0 < s3 and by1 > s1 for s0, s1, s2, s3 in swings):
                    continue
                return px, py
            return None

        chosen = first_fit = None
        for nm, dm, size, rot in options:
            tw, th = _text_wh(nm, size, bold=True)
            if dm:
                dw, dh = _text_wh(dm, size * 0.82)
                tw, th = max(tw, dw), th + dh
            bw, bh = (aw, ah) if rot == 0 else (ah, aw)
            if tw <= bw and th <= bh:
                if first_fit is None:
                    first_fit = (nm, dm, size, rot)
                lw_, lh_ = (tw / PT, th / PT) if rot == 0 else (th / PT, tw / PT)
                spot = clear_spot(lw_, lh_)
                if spot:
                    chosen = (nm, dm, size, rot)
                    cx, cy = spot
                    break
        if chosen is None:
            cx, cy = cx0, cy0
            chosen = first_fit or (ab or name, None, 5, 90 if (y1 - y0) > (x1 - x0) else 0)
        nm, dm, size, rot = chosen
        if dm:
            gap = size * 0.62 / PT
            ax.text(cx, cy + gap, nm.upper(), ha="center", va="center", fontsize=size,
                    fontproperties=_FONT_B, zorder=6)
            ax.text(cx, cy - gap, dm, ha="center", va="center", fontsize=size * 0.82,
                    fontproperties=_FONT, zorder=6)
        else:
            ax.text(cx, cy, nm.upper(), ha="center", va="center", fontsize=size,
                    fontproperties=_FONT_B, rotation=rot, zorder=6)

    # ------------------------------------------------------------------ dimensions
    def dim_line(p0, p1, y, text, horizontal=True, ext_from=None, size=7):
        if horizontal:
            ax.plot([p0, p1], [y, y], color=INK, lw=0.6, zorder=2)
            for p in (p0, p1):
                ax.plot([p - 0.35, p + 0.35], [y - 0.35, y + 0.35], color=INK, lw=0.9)
                if ext_from is not None:
                    ax.plot([p, p], [ext_from, y + 0.4], color=INK, lw=0.4)
            if text:
                ax.text((p0 + p1) / 2, y + 0.3, text, ha="center", va="bottom", fontsize=size,
                        fontproperties=_FONT)
        else:
            ax.plot([y, y], [p0, p1], color=INK, lw=0.6, zorder=2)
            for p in (p0, p1):
                ax.plot([y - 0.35, y + 0.35], [p - 0.35, p + 0.35], color=INK, lw=0.9)
                if ext_from is not None:
                    ax.plot([ext_from, y - 0.4], [p, p], color=INK, lw=0.4)
            if text:
                ax.text(y - 0.3, (p0 + p1) / 2, text, ha="right", va="center", fontsize=size,
                        rotation=90, fontproperties=_FONT)

    # chain dimensions along the north and west walls, overall dims outside them
    north = sorted({(round(rect(r)[0], 3), round(rect(r)[2], 3)) for r in rooms
                    if any(abs(side_segment(r, "N").c - D) < TOL for _ in FP.exterior_parts(r, "N"))})
    for a, b in north:
        lbl = fmt_ft(b - a) if (b - a) * PT >= _text_wh(fmt_ft(b - a), 6)[0] + 4 else None
        dim_line(a, b, D + 1.6, lbl, True, ext_from=D + 0.3, size=6)
    dim_line(0, W, D + 4.2, fmt_ft(W), True, ext_from=D + 2.2, size=8)
    west = sorted({(round(rect(r)[1], 3), round(rect(r)[3], 3)) for r in rooms
                   if any(abs(side_segment(r, "W").c) < TOL for _ in FP.exterior_parts(r, "W"))})
    for a, b in west:
        lbl = fmt_ft(b - a) if (b - a) * PT >= _text_wh(fmt_ft(b - a), 6)[0] + 4 else None
        dim_line(a, b, -1.6, lbl, False, ext_from=-0.3, size=6)
    dim_line(0, D, -4.4, fmt_ft(D), False, ext_from=-2.2, size=8)

    # ------------------------------------------------------------------ title block
    sqft = FP.area
    ttl = title or layout.get("title") or "Floor Plan"
    meta = layout.get("meta", {})
    if rep.garage_sqft or rep.other_unconditioned_sqft or not FP.is_rect:
        head = f"{ttl}  \u2022  {rep.living_sqft:,.0f} SQ FT LIVING"
        if rep.garage_sqft:
            head += f"  \u2022  GARAGE {rep.garage_sqft:,.0f} SQ FT"
        sub = (f"{'L-shaped' if len(FP.rects) == 2 else 'Rectangular' if FP.is_rect else 'Composite'} footprint, "
               f"overall {fmt_ft(W)} x {fmt_ft(D)}  \u2022  Living {rep.living_sqft:,.0f} sq ft")
        if rep.garage_sqft:
            sub += f"  \u2022  Garage {rep.garage_sqft:,.0f} sq ft"
        if rep.other_unconditioned_sqft:
            sub += f"  \u2022  Garage storage {rep.other_unconditioned_sqft:,.0f} sq ft"
        sub += f"  \u2022  Total under roof {sqft:,.0f} sq ft"
    else:
        head = f"{ttl}  \u2022  {sqft:,.0f} SQ FT"
        sub = f"Footprint {fmt_ft(W)} x {fmt_ft(D)}  \u2022  Total area {sqft:,.0f} sq ft (gross)"
    ax.text(0, -3.2, head, ha="left", va="top", fontsize=13, fontproperties=_FONT_B)
    if "seed" in meta:
        sub += f"  \u2022  seed {meta['seed']}"
    ax.text(0, -5.6, sub, ha="left", va="top", fontsize=8, fontproperties=_FONT)
    # scale bar
    y = -10.0
    marks = [0, 2.5, 5, 10, 20]
    for i, (a, b) in enumerate(zip(marks, marks[1:])):
        box(a, y, b, y + 0.6, fc=INK if i % 2 == 0 else "white", ec=INK, lw=0.6, z=3)
    for m in marks:
        ax.text(m, y - 0.4, f"{m:g}'", ha="center", va="top", fontsize=6.5, fontproperties=_FONT)
    ax.text(21, y + 0.3, "SCALE (FEET)", ha="left", va="center", fontsize=6.5, fontproperties=_FONT)
    # north arrow
    nx, ny = W - 1.5, -9.8
    ax.add_patch(Polygon([[nx, ny + 3], [nx - 1.0, ny], [nx, ny + 0.8]], closed=True, fc=INK, ec=INK, lw=0.6))
    ax.add_patch(Polygon([[nx, ny + 3], [nx + 1.0, ny], [nx, ny + 0.8]], closed=True, fc="white", ec=INK, lw=0.6))
    ax.text(nx, ny + 3.3, "N", ha="center", va="bottom", fontsize=9, fontproperties=_FONT_B)

    outs = []
    for fmt in formats:
        path = f"{out_base}.{fmt}"
        fig.savefig(path, dpi=dpi, facecolor="white")
        outs.append(path)
    plt.close(fig)
    return outs
