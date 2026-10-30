"""Prompt text that teaches any LLM to write a layout this tool can validate and render."""

LLM_PROMPT = r'''You are designing a single-storey house floor plan. Output ONLY a JSON object
(no prose) in exactly this schema. All lengths are in feet.

COORDINATES: origin (0,0) is the SOUTH-WEST (bottom-left) corner of the footprint.
x grows east (right), y grows north (up). The street/front is the SOUTH wall (y = 0).

{
  "version": 1,
  "title": "3 Bedroom / 2 Bath",
  "footprint": {"width": W, "depth": D},            // rectangle, OR one of:
  //  "footprint": {"rects": [{"x":0,"y":0,"w":40,"d":30}, {"x":40,"y":0,"w":22,"d":22}]}   (L = 2 rects)
  //  "footprint": {"polygon": [[0,0],[62,0],[62,22],[40,22],[40,30],[0,30]]}          (rectilinear only)
  "rooms": [
    {"id": "bed1", "name": "Bedroom 1", "type": "bedroom",
     "x": 0, "y": 18, "w": 12, "d": 12,              // south-west corner + size
     "target": {"area": 144}}                        // optional, informational
  ],
  "doors": [
    {"from": "hall", "to": "bed1", "type": "door", "width": 2.67,
     "offset": 0.5,            // feet from the START (west or south end) of the wall the rooms share
     "hinge": "start",         // "start" or "end" jamb
     "swing_into": "bed1"},    // which room the door swings into
    {"from": "living", "to": "kitchen", "type": "opening", "width": 6},   // cased opening, no leaf
    {"from": "bed1", "to": "closet-1", "type": "sliding", "width": 4},    // closet slider
    {"from": "living", "to": "exterior", "wall": "S", "width": 3, "offset": 2},  // front door
    {"from": "garage", "to": "exterior", "wall": "S", "type": "garage", "width": 16, "offset": 3},  // overhead door
    {"from": "garage", "to": "laundry", "width": 3}                      // man door into the house
  ],
  "windows": [
    {"room": "bed1", "wall": "N", "width": 3, "offset": 4}   // offset from the west/south end of that room wall
  ]
}

ROOM TYPES: bedroom, bathroom, half_bath, kitchen, living, dining, hallway, closet,
walk_in_closet, laundry, pantry, entry, foyer, family, office, storage, garage, mudroom, other.
A room may carry "conditioned": false (e.g. garage storage); garages are always reported
separately from living square footage.

HARD RULES (the validator rejects the plan otherwise):
1. Every room is an axis-aligned rectangle fully inside the footprint (for an L, not in the notch).
2. Rooms never overlap and together cover the whole footprint (no gaps):
   the sum of room areas must equal the footprint area. Use a hallway or closets to fill leftovers.
3. A door may only join two rooms that share a wall; width <= length of the shared wall;
   0 <= offset <= shared wall length - width. Exterior doors must sit on an exterior wall.
4. At least one exterior door; every room reachable from an exterior door through doors/openings.
5. Bedrooms must have a door to a hallway, living, family, entry or foyer (not only via another room).
   Bathrooms open off circulation or a bedroom (ensuite).
6. Windows only on exterior walls, must fit the wall and must not overlap a door. On an L, a room
   wall that is half against the garage/other wing is exterior only on the outside part; the
   window or door (offset .. offset+width) must lie entirely on that part.
8. Garages: type "garage", sizes about 12x22 (1 car), 22x22 (2), 32x22 (3); overhead doors use
   "type": "garage" on an exterior wall (9' single, 16' double). The man door goes to a laundry/
   mudroom, kitchen or hallway - never directly into a bedroom.
7. Minimum sizes: bedroom >= 70 sq ft and >= 7' each way (aim for 10'x11' or more),
   bathroom >= 5' x 7', hallway >= 3' wide (use 3.5-4'), closet >= 2' deep.

TIPS: put a hallway band across the house, public rooms (living/kitchen/dining) on the
south side and bedrooms/baths on the north side, so every bedroom touches the hall.
Snap all coordinates to 0.5 ft. Give every bedroom a window and a closet.
'''
