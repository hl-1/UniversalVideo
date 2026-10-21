"""One registry for parsing, recall keywords, semantic scoring and UI choices."""
from __future__ import annotations

import json
from pathlib import Path


REGISTRY = json.loads((Path(__file__).parents[3] / "skills/scene-playlist/categories.json").read_text())
SCENES = tuple(REGISTRY["scenes"])
ALIASES = REGISTRY["aliases"]


def normalize_scene(value):
    value = str(value).strip()
    folded = value.casefold()
    for scene in SCENES:
        if folded == scene.casefold() or folded in {v.casefold() for v in ALIASES.get(scene, [])}:
            return scene
    return value


def infer_scene(need):
    # Never turn a negated theme (e.g. 不要爱情) into a positive scene.
    import re
    positive = re.sub(r"(?:不要|排除|不想|不听)[^，,。；;]*", "", need).casefold()
    matches = [scene for scene in SCENES if any(
        term.casefold() in positive for term in (scene, *ALIASES.get(scene, [])))]
    return matches[0] if len(matches) == 1 else None
