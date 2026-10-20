"""Garden OS (garden_room) equipment: delegated work through the app's own CLI.

Nothing here adds a broker tool, model choice or phone permission. The note rides on the existing
seams: per-run backend instructions for ask_z0 delegation, and a one-time operating-context job for
the persistent liaison session.
"""

import os
from pathlib import Path

from .core import ROOT

GARDEN = Path(os.environ.get("Z0LIVE_GARDEN_ROOT", str(ROOT.parent / "garden_room")))
GARDEN_BRIEF = f"""Garden OS (garden_room) equipment, for Kevin's plant stands/shelves from Amazon. Paths are data.
When he sends or says an Amazon product link, or asks to add/show/compare a stand or for DIY stand ideas, run with
explicit workdir {GARDEN} and argv (never a shell string):
  python3 tools/equipment.py add URL [--status shortlisted] [--notes TEXT] --source z0live   (reads the page once, caches images/model)
  python3 tools/equipment.py share URL --source z0live        (remember it for the app's "add this stand")
  python3 tools/equipment.py list | update ID --status S | show ID | corner ID | ar ID | compare ID ID
  python3 tools/equipment.py ideas --ids ID ... [--constraints TEXT]   (one turbo DIY run; check with tools/equipment_ideas.py list)
show/corner/ar/compare only QUEUE a Garden OS phone action that runs while its voice is on; say queued, not done.
Report what the command printed: 'Amazon model' means Amazon's own 3D file at true scale; 'approximate from
dimensions' is a proxy built from the listed size. Never log in to Amazon or retry a bot wall."""
GARDEN_EQUIPMENT = GARDEN_BRIEF + """
This message only updates your operating context; do not start tasks or change files. Reply 'Garden equipment ready'."""
