import copy
import json

from .types import identifier, text


def validate_report(report):
    if not isinstance(report, dict) or not {"description", "objects", "observations", "uncertainties"} <= set(report):
        raise ValueError("Visual report requires description, objects, observations and uncertainties")
    text(report["description"], 20000)
    if not isinstance(report["objects"], list) or len(report["objects"]) > 64:
        raise ValueError("Visual report exceeds object budget")
    object_ids = set()
    for entry in report["objects"]:
        name = identifier(entry["id"])
        if name in object_ids:
            raise ValueError("Duplicate visual object identifier")
        object_ids.add(name)
        text(entry["name"])
        for field in ("visible_states", "relations", "uncertainties"):
            if not isinstance(entry[field], list) or len(entry[field]) > 64:
                raise ValueError("Invalid visual object detail list")
            for detail in entry[field]:
                text(detail, 2000)
    if not isinstance(report["observations"], list) or len(report["observations"]) > 128:
        raise ValueError("Visual report exceeds observation budget")
    seen = set()
    for observation in report["observations"]:
        if set(observation) != {"id", "text", "object_ids", "certainty"}:
            raise ValueError("Invalid visual observation fields")
        name = identifier(observation["id"])
        if name in seen:
            raise ValueError("Duplicate visual observation identifier")
        seen.add(name)
        text(observation["text"], 2000)
        if not isinstance(observation["object_ids"], list) or not set(observation["object_ids"]) <= object_ids:
            raise ValueError("Visual observation references an unknown object")
        if observation["certainty"] not in {"visible", "uncertain"}:
            raise ValueError("Visual observations must separate visibility from uncertainty")
    if not isinstance(report["uncertainties"], list) or len(report["uncertainties"]) > 64:
        raise ValueError("Invalid visual uncertainties")
    for uncertainty in report["uncertainties"]:
        text(uncertainty, 2000)
    if len(json.dumps(report, ensure_ascii=False)) > 48000:
        raise ValueError("Visual report exceeds text budget")
    return copy.deepcopy(report)
