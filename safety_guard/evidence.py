import hashlib
import json

from .types import identifier


def source_id(entry):
    raw = json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return "src_" + hashlib.sha256(raw).hexdigest()[:24]


def index_evidence(packet):
    request = packet["request"]
    evidence = {
        "request_task": {"at_s": request["at_s"], "source": "task_text", "text": request["task"]},
        "request_action": {"at_s": request["at_s"], "source": "proposed_action", "text": request["action"]},
    }
    if request.get("context"):
        evidence["request_context"] = {"at_s": request["at_s"], "source": "action_context",
                                       "text": json.dumps(request["context"], ensure_ascii=False)}
    scenes = packet.get("scene_history", [])
    for index, scene in enumerate(scenes):
        prefix = f"scene_{index}"
        evidence[prefix] = {"at_s": scene["at_s"], "source": scene["source"], "text": scene["description"]}
        for object_index, entry in enumerate(scene["objects"]):
            evidence[f"{prefix}_object_{object_index}"] = {
                "at_s": scene["at_s"], "source": scene["source"], "text": json.dumps(entry, ensure_ascii=False)}
    for update in packet.get("timeline", []):
        evidence[f"{update['kind']}_{update['sequence']}"] = {
            "at_s": update["at_s"], "source": update["data"]["source"],
            "text": json.dumps(update["data"], ensure_ascii=False)}
    for report in packet.get("visual_reports", []):
        prefix = report["evidence_prefix"]
        for observation in report["observations"]:
            evidence[f"{prefix}_{observation['id']}"] = {
                "at_s": report["at_s"], "source": report["source"], "text": observation["text"],
                "certainty": observation["certainty"], "image_sources": report.get("image_sources", [])}
    indexed = {}
    for locator, entry in evidence.items():
        name = source_id(entry)
        indexed[name] = entry | {"locator": locator}
    return indexed


def validate_sources(candidate, evidence, catalog=None):
    for group in ("predicates", "event_rules", "time_rules", "inferred_facts"):
        for entry in candidate[group]:
            if group != "inferred_facts" and catalog and catalog[group].get(entry["id"]) == entry:
                continue
            names = entry.get("evidence_ids", [])
            if not isinstance(names, (list, tuple)) or len(names) > 32 or len(set(names)) != len(names):
                raise ValueError("Invalid or duplicate evidence references")
            for name in names:
                identifier(name)
                if name not in evidence:
                    raise ValueError(f"Unknown textual evidence reference: {name}")
            if group == "inferred_facts" and entry["value"] is not None:
                if "evidence_ids" in entry and not names:
                    raise ValueError("A definite inferred fact must cite supporting evidence; absence does not establish false")
                if any(evidence[name].get("certainty") == "uncertain" for name in names):
                    raise ValueError("An uncertain visual statement cannot ground a definite Boolean; retain null or use independent evidence")
                if entry["at_s"] < max([0] + [evidence[name]["at_s"] for name in names]):
                    raise ValueError("Inferred fact predates its supporting evidence")
