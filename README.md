# floorplan-gen

Generate accurate, buildable-looking 2D house floor plans from **structured layout data**:
real room counts, real dimensions, doors that connect rooms that actually touch, and
correctly spelled labels. Unlike diffusion image models, nothing is hallucinated: a layout
is JSON, it is validated, and only then drawn with code.

```
spec (beds/baths/sqft/seed) --> generate.py --> layout JSON --> validate.py --> render.py --> PNG + SVG
                                      LLM- or hand-written layout JSON --^
```

## Install (Ubuntu 24.04, Python 3.12)

```bash
sudo apt update && sudo apt install -y python3-venv     # once; Ubuntu ships python3.12
cd ~/floorplan-gen
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt                          # just matplotlib
```

## Run

```bash
source ~/floorplan-gen/.venv/bin/activate && cd ~/floorplan-gen

# generate from a spec -> plan.json, plan.png, plan.svg
python floorplan.py --beds 3 --baths 2 --sqft 1600 --seed 1 --out plan
python floorplan.py --beds 3 --baths 2 --sqft 1600 --seed 2 --out plan_b   # different valid layout
python floorplan.py --beds 4 --baths 3 --sqft 2400 --seed 1 --out big
python floorplan.py --beds 3 --baths 2.5 --sqft 1900 --seed 3 --out half  # .5 = powder room

# attached garage and L-shaped footprints (--sqft is always the LIVING area; garage reported separately)
python floorplan.py --beds 3 --baths 2 --sqft 1600 --seed 1 --garage 1 --out g1          # rect, 1-car
python floorplan.py --beds 3 --baths 2 --sqft 1800 --seed 1 --shape L --garage 2 --out l2  # garage = L leg
python floorplan.py --beds 4 --baths 3 --sqft 2600 --seed 1 --shape L --garage 3 --garage-side left --out l3
python floorplan.py --beds 3 --baths 2 --sqft 1700 --seed 2 --shape L --out lfam          # L leg = family room

# render a layout written by hand or by an LLM (validated first; refuses to draw invalid ones)
python floorplan.py --layout examples/cottage_2bd.json --out cottage

# validate only (exit code 1 on errors) - try the deliberately broken example
python floorplan.py --layout examples/broken_layout.json --validate-only

# print a prompt that teaches any LLM the schema and rules
python floorplan.py --llm-prompt > prompt.txt
```

Options: `--no-laundry`, `--title "My House"`, `--dpi 300`,
`--garage 0|1|2|3` (cars; 12'x22', 22'x22', 32'x22'), `--shape rect|L`, `--garage-side left|right`.

Garage / L behaviour:
- `--shape rect --garage N`: the garage column runs the full house depth at the hallway end of the
  house (deep houses get a deeper garage plus unconditioned garage storage behind it), so the
  footprint stays a rectangle.
- `--shape L --garage N`: the garage is the L leg - flush with the front, leaving a >= 6' notch at the
  rear (it projects forward instead when the house is shallow).
- `--shape L` without a garage: the L leg is a family room opening widely onto the living room.
- `--garage-side` picks the side; by default the garage goes at the hallway end. The house is
  mirrored if needed so the man door always opens into the laundry/mudroom, kitchen or hallway
  (laundry is moved next to the garage when possible). Overhead doors (16' double, 9' single) are drawn
  on the front wall with the raised-door outline dashed; there is also a 3' service door on the
  garage side wall.
- The drawing title and the CLI report living sq ft and garage sq ft (and garage storage) separately.

## Layout JSON schema (feet; origin = south-west corner, x east, y north, front = south)

```jsonc
{
  "version": 1,
  "title": "3 Bedroom / 2 Bath",
  "footprint": {"width": 52, "depth": 30},          // rectangle, or for an L / any rectilinear shape:
  // "footprint": {"rects": [{"x":0,"y":0,"w":40,"d":30}, {"x":40,"y":0,"w":22,"d":22}]}
  // "footprint": {"polygon": [[0,0],[62,0],[62,22],[40,22],[40,30],[0,30]]}
  "rooms": [
    {"id": "bedroom-1", "name": "Bedroom 1", "type": "bedroom",
     "x": 0, "y": 18, "w": 12, "d": 12, "target": {"area": 150}}       // target optional (w/d or area)
  ],
  "doors": [
    {"from": "hall", "to": "bedroom-1", "type": "door", "width": 2.67,
     "offset": 0.5, "hinge": "start", "swing_into": "bedroom-1"},      // offset along the shared wall
    {"from": "living-room", "to": "dining", "type": "opening", "width": 8},
    {"from": "bedroom-1", "to": "closet", "type": "sliding", "width": 5},
    {"from": "living-room", "to": "exterior", "wall": "S", "width": 3, "offset": 4},
    {"from": "garage", "to": "exterior", "wall": "S", "type": "garage", "width": 16, "offset": 3},
    {"from": "garage", "to": "laundry", "width": 3}
  ],
  "windows": [{"room": "bedroom-1", "wall": "N", "width": 3, "offset": 4.5}]
}
```

Room types: bedroom, bathroom, half_bath, kitchen, living, dining, hallway, closet,
walk_in_closet, laundry, pantry, entry, foyer, family, office, storage, garage, mudroom, other.
Doors may reference rooms by `id` or by unique `name`. If `offset` is omitted the door is centred.
Door types: `door`, `opening`, `sliding`, `garage` (overhead, exterior wall of a garage only).
Rooms may set `"conditioned": false` (counted with the garage, not living area). With `rects`/`polygon`
footprints, `width`/`depth` are informational (bounding box). Polygons must be rectilinear.

## LLM hook (Python API)

```python
from fpgen import load_layout, validate_layout, render_layout, generate_layout
layout = load_layout("llm_output.json")       # also strips ```json fences
report = validate_layout(layout)              # -> ValidationReport(errors, warnings)
if report.ok:
    render_layout(layout, "out/plan")         # writes out/plan.png and out/plan.svg
else:
    print(report.format())                    # feed these messages back to the LLM to fix
```

The validator checks: schema, footprint shape (overlapping footprint rects, non-rectilinear
polygons), rooms inside the (possibly L-shaped) footprint, overlaps (with the overlapping box), uncovered
gaps (with a location), door/shared-wall geometry, door collisions, windows on exterior walls,
exterior doors/windows lying on the truly exterior part of a wall (L shapes), overhead doors only on
garages, no bedroom door into a garage, reachability from an exterior door, bedrooms off a hallway/living area, bathrooms off
circulation or a bedroom (ensuite), code-style minimum sizes (errors) and realistic sizes,
proportions, egress windows and target-area drift (warnings).

## How the generator works

Deterministic "zoned strip subdivision" (no ML): a 4' hallway band splits the footprint into a
public band (living / kitchen / dining) and a private band (bedrooms, baths, closets, laundry); the
living room spans its band plus the hall depth. Rooms are bundled into column units (bedroom +
closet column, primary bedroom + walk-in/bath column, bath + closet column ...), each stacked from
the hallway to the exterior wall so every room touches circulation and bedrooms get an exterior
window. Column widths come from a 1-D allocation respecting minimum sizes, snapped to 6".
Groups that don't fit overflow to the other band. Hundreds of candidates (aspect ratio, band
depth, overflow, optional office / eat-in kitchen) are scored for proportions and target areas;
the best one that passes the validator wins. The seed chooses the structural variant (kitchen
front or back, living room left or right, room/group order, mirroring, front-door position).

## Files

- `floorplan.py` - CLI
- `fpgen/generate.py` - layout generator (rectangular house body)
- `fpgen/wings.py` - attached garage / L-shaped wing
- `fpgen/validate.py` - validator / LLM hook
- `fpgen/geometry.py` - geometry + `Footprint` (rects / polygon, exterior wall parts)
- `fpgen/render.py`, `fpgen/fixtures.py` - matplotlib renderer (walls, doors, windows, fixtures, labels, dims)
- `fpgen/prompt.py` - LLM prompt text
- `examples/` - hand-written valid layouts (rect cottage, L + garage) and deliberately broken ones
- `samples/` - example outputs

## HTTP service

The command line above still works on its own and does not need a database or an API key.
To serve plans over HTTP, use Python 3.12 on this computer (Docker is not required) and follow
[docs/service.md](docs/service.md). With the virtualenv active and the variables from `.env` exported:

```bash
alembic upgrade head
```

Then run the API and the single worker in two terminals:

```bash
uvicorn fpgen.api:app --host 127.0.0.1 --port 8000 --workers 1
python -m fpgen.worker
```

`GET /health` needs no key. Every other route needs `Authorization: Bearer $FLOORPLAN_API_KEY`.
Import [docs/floor-plan-api.postman_collection.json](docs/floor-plan-api.postman_collection.json)
and set `base_url` and `api_key`. Poll a job every two seconds until it succeeds or fails; polling
does not submit the job again.

## Known limitations

- Rooms are rectangles. The validator/renderer accept any rectilinear footprint (L, T, U ...), but the
  generator only makes a rectangle, rectangle + full-depth garage, or an L (one wing at one end).
  No angled walls, porches, second storeys or stairs; no detached garages or side-entry garage doors.
- The garage/house wall is drawn as a normal interior wall (no fire-rated separation detail).
- One layout family (central hallway "bar" plan). Large homes get long hallways; it is a
  schematic/concept plan, not construction documents (no structure, plumbing stacks,
  wall-thickness-accurate net dimensions, code review or energy/egress verification).
- Room dimensions are centre-line dimensions; footprint area is gross (outside of walls).
- Fixtures (tub, toilet, vanity, counters, range, fridge, washer/dryer) are placed greedily and
  are skipped when a room is too tight; furniture is not drawn.
- Very small houses for the room count are rejected (roughly < 350 + 220*beds + 50*baths sq ft).
