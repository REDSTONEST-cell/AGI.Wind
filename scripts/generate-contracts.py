"""Generate one wire schema; TS is derived from this, never maintained separately."""
import json
from pathlib import Path
from pydantic.json_schema import models_json_schema
from agi_wind.core import types, events

root = Path(__file__).resolve().parents[1]
(root / "packages/contracts").mkdir(parents=True, exist_ok=True)
models = [getattr(types, name) for name in ("Actor", "Scope", "ModelRef", "PersonaSnapshot", "ContentPart", "SourceRef", "ContextBundle", "RuntimeRequest", "RuntimeDelta", "MemoryHit", "ChatRequest", "AcceptedRequest", "RuntimeCapabilities")]
models += [events.AudioSegment, events.AvatarCue, events.StreamEvent]
_, schema = models_json_schema([(model, "validation") for model in models], title="AGI.Wind protocol 1.0")
schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
schema["type"] = "object"
schema["properties"] = {model.__name__: {"$ref": "#/$defs/" + model.__name__} for model in models}
(root / "packages/contracts/schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
