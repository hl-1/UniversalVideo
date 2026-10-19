from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.core.paths import ROOT_DIR as PROJECT_ROOT
from backend.modules.music.agent.models import CreateRun, ParsedNeed, RunResult


ROOT = PROJECT_ROOT / "backend" / "skills" / "scene-playlist"
REGISTRY = {"scene-playlist": ROOT}


def load(name="scene-playlist"):
    root = REGISTRY[name]
    paths = [root / "SKILL.md", root / "categories.json"]
    contents = [p.read_bytes() for p in paths]
    schemas = {"input": CreateRun.model_json_schema(), "parsed": ParsedNeed.model_json_schema(),
               "result": RunResult.model_json_schema()}
    schema = json.dumps(schemas, sort_keys=True).encode()
    return {"text": contents[0].decode("utf-8"), "categories": json.loads(contents[1]),
            "schema": ParsedNeed.model_json_schema(),
            "schemas": schemas,
            "version": hashlib.sha256(b"\x00".join(contents + [schema])).hexdigest()}
