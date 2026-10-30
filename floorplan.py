#!/usr/bin/env python3
"""floorplan.py - generate, validate and render 2D house floor plans from structured data.

Examples:
  python floorplan.py --beds 3 --baths 2 --sqft 1600 --seed 1 --out plan
  python floorplan.py --beds 3 --baths 2 --sqft 1800 --shape L --garage 2 --out plan_l
  python floorplan.py --layout my_layout.json --out plan
  python floorplan.py --layout llm_output.json --validate-only
  python floorplan.py --llm-prompt > prompt.txt      # instructions to give an LLM
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from fpgen import generate_layout, load_layout, render_layout, validate_layout
from fpgen.prompt import LLM_PROMPT


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_argument_group("input (choose one)")
    src.add_argument("--layout", help="render/validate an existing layout JSON (hand- or LLM-written)")
    src.add_argument("--beds", type=int, help="number of bedrooms (1-6)")
    ap.add_argument("--baths", type=float, default=2, help="bathrooms, e.g. 2 or 2.5 (default 2)")
    ap.add_argument("--sqft", type=float, default=1600, help="target gross square footage (default 1600)")
    ap.add_argument("--seed", type=int, default=1, help="layout variant (default 1)")
    ap.add_argument("--no-laundry", action="store_true", help="omit the laundry room")
    ap.add_argument("--garage", type=int, choices=[0, 1, 2, 3], default=0,
                    help="attached garage: 0 (none), 1, 2 or 3 cars (12x22, 22x22, 32x22 ft)")
    ap.add_argument("--shape", choices=["rect", "L"], default="rect",
                    help="footprint: rect (default) or L (garage, or a family room, forms the L leg)")
    ap.add_argument("--garage-side", choices=["left", "right"],
                    help="side for the garage / L leg (default: garage at the hallway end, family-room wing at the living-room end)")
    ap.add_argument("--out", default="plan", help="output base path; writes OUT.png, OUT.svg, OUT.json")
    ap.add_argument("--title", help="title text for the drawing")
    ap.add_argument("--dpi", type=int, default=170, help="PNG resolution (default 170)")
    ap.add_argument("--validate-only", action="store_true", help="only validate --layout, do not render")
    ap.add_argument("--llm-prompt", action="store_true", help="print a prompt that teaches an LLM the layout schema")
    a = ap.parse_args(argv)

    if a.llm_prompt:
        print(LLM_PROMPT)
        return 0
    if a.layout:
        try:
            layout = load_layout(a.layout)
        except (OSError, json.JSONDecodeError) as e:
            print(f"ERROR: cannot read {a.layout}: {e}", file=sys.stderr)
            return 2
    elif a.beds is not None:
        try:
            layout = generate_layout(a.beds, a.baths, a.sqft, a.seed, laundry=False if a.no_laundry else None,
                                     garage=a.garage, shape=a.shape, garage_side=a.garage_side)
        except (ValueError, RuntimeError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
    else:
        ap.error("give either --layout FILE or --beds N (see --help)")

    report = validate_layout(json.loads(json.dumps(layout)))
    print(report.format())
    if report.ok and (report.garage_sqft or report.other_unconditioned_sqft):
        print(f"Living area {report.living_sqft:,.0f} sq ft; garage {report.garage_sqft:,.0f} sq ft"
              + (f"; garage storage {report.other_unconditioned_sqft:,.0f} sq ft"
                 if report.other_unconditioned_sqft else ""))
    if a.validate_only:
        return 0 if report.ok else 1
    if not report.ok:
        print("Not rendering an invalid layout. Fix the errors above.", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    if not a.layout:
        with open(a.out + ".json", "w", encoding="utf-8") as fh:
            json.dump(layout, fh, indent=2)
        print(f"wrote {a.out}.json")
    for p in render_layout(layout, a.out, dpi=a.dpi, title=a.title):
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
