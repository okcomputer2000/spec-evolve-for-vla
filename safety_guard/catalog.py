import copy
import hashlib
import json
import os
import pathlib
import threading

from .logic import make_monitor, prove, references
from .predicates import validate_evaluation_graph
from .types import Fact, identifier, normalize, text, timestamp


def empty_catalog():
    return {"version": 0, "predicates": {}, "event_rules": {}, "time_rules": {}, "contracts": {}, "mappings": {}}


def mapping_key(task, action, bindings):
    return json.dumps([normalize(task), normalize(action), sorted(bindings.items())], ensure_ascii=False)


def ensure_fields(value, fields, optional=()):
    if not isinstance(value, dict) or not set(fields) <= set(value) or set(value) - set(fields) - set(optional):
        raise ValueError(f"Expected fields {sorted(fields)}")


def validate_candidate(candidate, catalog, request):
    ensure_fields(candidate, {"mode", "contract_id", "predicates", "event_rules", "time_rules", "contract", "inferred_facts", "rationale"})
    if candidate["mode"] not in {"reuse", "extend", "unresolved"}:
        raise ValueError("Unsupported resolution mode")
    text(candidate["rationale"])
    updated = copy.deepcopy(catalog)
    for group, limit in (("predicates", 64), ("event_rules", 64), ("time_rules", 32)):
        entries = candidate[group]
        if not isinstance(entries, list) or len(entries) > limit:
            raise ValueError("Candidate exceeds definition budget")
        identifiers = [identifier(entry["id"]) for entry in entries]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate candidate definition identifier")
        for entry in entries:
            if entry["id"] in updated[group] and entry != updated[group][entry["id"]]:
                raise ValueError(f"Existing definition cannot be silently replaced: {entry['id']}")
            updated[group][entry["id"]] = copy.deepcopy(entry)
    if len(updated["predicates"]) > 128 or len(updated["contracts"]) > 256 or len(updated["event_rules"]) > 256 or len(updated["time_rules"]) > 128:
        raise ValueError("Catalog exceeds configured runtime budget")
    predicates = set(updated["predicates"])
    def expression(value):
        missing = references(value) - predicates
        if missing:
            raise ValueError(f"Undeclared predicate references: {sorted(missing)}")
    def effects(entries):
        if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
            raise ValueError("Rules require bounded effects")
        seen = set()
        for entry in entries:
            ensure_fields(entry, {"predicate", "value"})
            if entry["predicate"] not in predicates:
                raise ValueError(f"Undeclared effect predicate: {entry['predicate']}. Declare a state predicate or remove the effect.")
            if type(entry["value"]) is not bool:
                raise ValueError(f"Effect value must be Boolean: {entry['predicate']}")
            if entry["predicate"] in seen:
                raise ValueError(f"Duplicate effect for predicate: {entry['predicate']}")
            seen.add(entry["predicate"])
            if updated["predicates"][entry["predicate"]].get("kind", "state") != "state":
                kind = updated["predicates"][entry["predicate"]]["kind"]
                raise ValueError(f"Event/time effects may update only state predicates: {entry['predicate']} has kind={kind}. "
                                 "Remove this effect; if it describes a physical transition, use a separate state predicate.")
    for definition in updated["predicates"].values():
        ensure_fields(definition, {"id", "subject", "description", "default", "default_basis", "valid_for_s"}, {"evidence_ids", "kind", "evaluation"})
        if definition.get("kind", "state") not in {"state", "proposal", "disposition"}:
            raise ValueError("Invalid predicate kind")
        identifier(definition["subject"])
        text(definition["description"])
        text(definition["default_basis"])
        if definition["default"] is not None and type(definition["default"]) is not bool:
            raise ValueError("Predicate default must be bool or null")
        if definition["valid_for_s"] is not None:
            timestamp(definition["valid_for_s"])
        if definition.get("evaluation") and definition["default"] is not None:
            raise ValueError("Visual predicates must have unknown defaults")
    validate_evaluation_graph(updated["predicates"])
    for rule in updated["event_rules"].values():
        ensure_fields(rule, {"id", "event", "subject", "when", "effects", "rationale"}, {"evidence_ids"})
        identifier(rule["event"])
        identifier(rule["subject"])
        text(rule["rationale"])
        expression(rule["when"])
        effects(rule["effects"])
    for rule in updated["time_rules"].values():
        ensure_fields(rule, {"id", "anchor", "anchor_value", "after_s", "when", "effects", "rationale"}, {"evidence_ids"})
        if rule["anchor"] not in predicates or type(rule["anchor_value"]) is not bool or not 0 < timestamp(rule["after_s"]) <= 31536000:
            raise ValueError("Invalid timer anchor or interval")
        if updated["predicates"][rule["anchor"]].get("kind", "state") != "state":
            raise ValueError(f"Timer anchors require actual state: {rule['anchor']} is not a state predicate")
        text(rule["rationale"])
        expression(rule["when"])
        effects(rule["effects"])
        if any(updated["predicates"][effect["predicate"]].get("evaluation") for effect in rule["effects"]):
            raise ValueError("Visual hold expiration becomes unknown; timers cannot assign visual truth values")
    changed = {effect["predicate"] for group in ("event_rules", "time_rules")
               for rule in updated[group].values() for effect in rule["effects"]}
    for name in changed:
        if updated["predicates"][name]["default"] is not None:
            raise ValueError(f"Mutable predicate {name} must have null default; place observed initial values in inferred_facts at their actual timestamp")
    if candidate["mode"] == "unresolved":
        if any(candidate[group] for group in ("predicates", "event_rules", "time_rules", "inferred_facts")) or candidate["contract"] is not None or candidate["contract_id"] is not None:
            raise ValueError("Unresolved response cannot mutate knowledge")
        return updated
    if candidate["mode"] == "reuse":
        if candidate["contract"] is not None or candidate["contract_id"] not in updated["contracts"]:
            raise ValueError("Reuse requires an existing contract")
        contract = updated["contracts"][candidate["contract_id"]]
        if contract["bindings"] != request.bindings or normalize(contract["task"]) != normalize(request.task):
            raise ValueError("Reused contract has incompatible task or object bindings")
    else:
        contract = candidate["contract"]
        ensure_fields(contract, {"id", "task", "action", "bindings", "description", "pre", "hold", "post", "coverage", "rationale"})
        identifier(contract["id"])
        if candidate["contract_id"] != contract["id"]:
            raise ValueError("Contract ID mismatch")
        if contract["bindings"] != request.bindings or normalize(contract["task"]) != normalize(request.task) or normalize(contract["action"]) != normalize(request.action):
            raise ValueError("Contract must bind to the exact request")
        if contract["id"] in updated["contracts"] and contract != updated["contracts"][contract["id"]]:
            raise ValueError("Existing contracts cannot be replaced")
        for key in ("description", "coverage", "rationale"):
            try:
                text(contract[key])
            except ValueError as error:
                raise ValueError(f"contract.{key}: {error}") from error
        for phase in ("pre", "hold", "post"):
            expression(contract[phase])
            make_monitor(contract[phase])
        for phase in ("pre", "post"):
            conjunction = {"op": "and", "args": [contract[phase], contract["hold"]]}
            if prove(conjunction, {}).value == "unsafe":
                raise ValueError(f"Unsatisfiable {phase}/hold contract")
        defaults = {name: {"value": definition["default"]} for name, definition in updated["predicates"].items()
                    if definition["default"] is not None and name not in changed}
        if prove({"op": "and", "args": [contract["pre"], contract["hold"]]}, defaults).value == "unsafe":
            raise ValueError("Contract is blocked by its unchanging defaults; use null for dynamic predicates and put current observations in inferred_facts, with appropriate event/time rules")
        updated["contracts"][contract["id"]] = copy.deepcopy(contract)
        if len(updated["contracts"]) > 256:
            raise ValueError("Catalog exceeds contract budget")
    if not isinstance(candidate["inferred_facts"], list) or len(candidate["inferred_facts"]) > 64:
        raise ValueError("Too many inferred facts")
    fact_ids = set()
    for entry in candidate["inferred_facts"]:
        ensure_fields(entry, {"predicate", "value", "at_s", "basis", "valid_for_s"}, {"evidence_ids"})
        Fact(**entry, source="llm_inference")
        if updated["predicates"].get(entry["predicate"], {}).get("evaluation") and entry["value"] is not None:
            raise ValueError("A visual predicate must be evaluated from a frame, not assigned by text inference")
        if entry["predicate"] not in predicates or entry["predicate"] in fact_ids or entry["at_s"] > request.at_s:
            raise ValueError("Invalid, duplicate or future inferred fact")
        fact_ids.add(entry["predicate"])
    updated["mappings"][mapping_key(request.task, request.action, request.bindings)] = contract["id"]
    updated["version"] = catalog["version"] + 1
    return updated


class CatalogStore:
    def __init__(self, directory):
        self.directory = pathlib.Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._writer = threading.Lock()
        self._current = empty_catalog()
        self._generation = 0
        pointer = self.directory / "current.json"
        if pointer.exists():
            reference = json.loads(pointer.read_text(encoding="utf-8"))
            target = self.directory / reference["file"]
            if target.parent != self.directory or target.name != f"catalog_{reference['version']:06d}.json":
                raise ValueError("Invalid catalog pointer")
            raw = target.read_bytes()
            if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
                raise ValueError("Catalog hash mismatch")
            loaded = json.loads(raw)
            if set(loaded) != set(self._current) or loaded["version"] != reference["version"]:
                raise ValueError("Invalid saved catalog")
            self._current = loaded
            self._generation = reference.get("generation", loaded["version"])

    @property
    def current(self):
        return self._current

    @property
    def generation(self):
        return self._generation

    def commit(self, candidate, request, provenance):
        with self._writer:
            updated, generation = self._stage(candidate, request, provenance)
            if candidate["mode"] == "unresolved":
                return self._current["version"]
            self._activate(updated["version"], generation)
            return updated["version"]

    def stage(self, candidate, request, provenance):
        with self._writer:
            return self._stage(candidate, request, provenance)

    def _stage(self, candidate, request, provenance):
        updated = validate_candidate(candidate, self._current, request)
        if candidate["mode"] == "unresolved":
            return updated, self._generation
        version = max([self._current["version"]] + [int(path.stem.split("_")[1]) for path in self.directory.glob("catalog_*.json")]) + 1
        updated["version"] = version
        target = self.directory / f"catalog_{version:06d}.json"
        raw = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with target.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        audit = self.directory / f"provenance_{version:06d}.json"
        with audit.open("x", encoding="utf-8") as stream:
            json.dump(provenance, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        return updated, self._generation

    def activate(self, version, expected_generation=None):
        with self._writer:
            self._activate(version, expected_generation)

    def _activate(self, version, expected_generation):
        if expected_generation is not None and expected_generation != self._generation:
            raise ValueError("Catalog changed while the candidate was staged; resolve again against the active catalog")
        target = self.directory / f"catalog_{int(version):06d}.json"
        raw = target.read_bytes()
        loaded = json.loads(raw)
        if set(loaded) != set(self._current) or loaded["version"] != int(version):
            raise ValueError("Invalid catalog version")
        reference = {"version": int(version), "generation": self._generation + 1,
                     "file": target.name, "sha256": hashlib.sha256(raw).hexdigest()}
        temporary = self.directory / "current.pending"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(reference, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.directory / "current.json")
        self._current = loaded
        self._generation = reference["generation"]
