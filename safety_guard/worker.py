import copy
import dataclasses
import json
import pathlib
import queue
import threading
import time
import uuid
from collections import deque

from .catalog import validate_candidate
from .evidence import index_evidence, validate_sources
from .logic import prove, references
from .state import StateEngine
from .types import Event, Fact, Update
from .vision import validate_report


class SemanticWorker:
    def __init__(self, store, model, install, directory, capacity=8, repair_attempts=1):
        self.store = store
        self.model = model
        self.install = install
        self.directory = pathlib.Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jobs = queue.Queue(maxsize=capacity)
        self.repair_attempts = repair_attempts
        self.pending = set()
        self.attempted = set()
        self.status = deque(maxlen=64)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="semantic-enrichment", daemon=True)
        self.thread.start()

    def submit(self, request, packet, revision):
        key, token = request.key(), (request.key(), revision)
        with self._lock:
            if self._stop.is_set():
                return "closed"
            if key in self.pending:
                return "pending"
            if token in self.attempted:
                return "already_attempted"
            if len(self.attempted) >= 2048:
                return "episode_budget_exhausted"
            try:
                self.jobs.put_nowait((request, copy.deepcopy(packet), token))
            except queue.Full:
                return "queue_full"
            self.pending.add(key)
            self.attempted.add(token)
            return "queued"

    def _ground_candidate(self, candidate, request, packet, record):
        if not hasattr(self.model, "ground"):
            return candidate
        extended = validate_candidate(candidate, self.store.current, request)
        contract = extended["contracts"][candidate["contract_id"]]
        expression = {"op": "and", "args": [contract[request.phase], contract["hold"]]}
        updates = [Update(entry["at_s"], entry["kind"],
            (Fact if entry["kind"] == "fact" else Event)(**entry["data"]), entry["sequence"])
            for entry in packet["timeline"]]
        sequence = max([0] + [entry.sequence for entry in updates])
        updates += [Update(entry["at_s"], "fact", Fact(**entry, source="llm_inference"), sequence + index + 1)
                    for index, entry in enumerate(candidate["inferred_facts"])]
        engine = StateEngine()
        engine.rebuild(extended, updates, request.at_s)
        proof = prove(expression, engine.values)
        missing = sorted(name for name in references(expression)
                         if engine.values.get(name, {}).get("value") is None and not extended["predicates"][name].get("evaluation"))
        if proof.value != "unknown" or not missing:
            return candidate
        grounding = self.model.ground({"request": packet["request"], "scene": packet["scene"],
            "scene_history": packet["scene_history"], "visual_report": packet.get("visual_report"),
            "visual_reports": packet.get("visual_reports", []), "evidence_index": packet["evidence_index"],
            "catalog": extended, "diagnostics": packet["diagnostics"],
            "timeline": packet["timeline"], "current_state": engine.values,
            "predicates": [extended["predicates"][name] for name in missing],
            "formal_obligation": expression, "hypothetical_counterexample_not_observation": proof.counterexample})
        record["grounding"] = grounding
        if not isinstance(grounding, dict) or set(grounding) != {"facts", "rationale"} or not isinstance(grounding["facts"], list):
            raise ValueError("Invalid grounding response")
        names = [entry["predicate"] for entry in grounding["facts"]]
        if len(names) != len(set(names)) or set(names) != set(missing):
            raise ValueError("Grounding must cover exactly the requested unknown predicates")
        merged = {entry["predicate"]: entry for entry in candidate["inferred_facts"]}
        merged.update({entry["predicate"]: entry for entry in grounding["facts"]})
        candidate = copy.deepcopy(candidate)
        candidate["inferred_facts"] = list(merged.values())
        validate_candidate(candidate, self.store.current, request)
        validate_sources(candidate, packet["evidence_index"], self.store.current)
        record["installed_candidate"] = candidate
        return candidate

    def _process(self, request, packet, token):
        job_id = uuid.uuid4().hex
        started = time.perf_counter()
        audit = {"job_id": job_id, "request": dataclasses.asdict(request), "packet": packet,
                 "attempts": [], "status": "error"}
        call_start = 0 if hasattr(self.model, "acknowledge_calls") else len(getattr(self.model, "calls", []))
        try:
            packet["visual_reports"] = []
            for index, scene in enumerate(packet.get("scene_history", [])):
                if scene.get("visual_report") is not None:
                    report = validate_report(scene["visual_report"])
                    report["source"] = "external_visual_description"
                elif scene.get("images") and not getattr(self.model, "use_visual_predicates", False):
                    report = validate_report(self.model.describe({"scene": scene}, scene["images"]))
                else:
                    continue
                packet["visual_reports"].append(report | {"at_s": scene["at_s"], "evidence_prefix": f"visual_{index}"})
            packet["visual_report"] = packet["visual_reports"][-1] if packet["visual_reports"] else None
            packet["evidence_index"] = index_evidence(packet)
            feedback = []
            for attempt in range(self.repair_attempts + 1):
                proposal_packet = packet | {"catalog": copy.deepcopy(self.store.current), "validation_feedback": feedback}
                record = {}
                audit["attempts"].append(record)
                try:
                    candidate = self.model.resolve(proposal_packet)
                    record["candidate"] = candidate
                    validate_candidate(candidate, self.store.current, request)
                    validate_sources(candidate, packet["evidence_index"], self.store.current)
                    if candidate["mode"] != "unresolved":
                        candidate = self._ground_candidate(candidate, request, packet, record)
                except Exception as error:
                    record["rejected"] = str(error)
                    rejected = record.get("candidate")
                    if hasattr(self.model, "feedback_candidate"):
                        rejected = self.model.feedback_candidate(rejected)
                    feedback.append({"error": str(error), "rejected_candidate": rejected,
                                     "rejected_grounding": record.get("grounding")})
                    if attempt == self.repair_attempts:
                        raise
                    continue
                if candidate["mode"] == "unresolved":
                    audit["status"] = "unresolved"
                    audit["reason"] = candidate["rationale"]
                    break
                catalog, generation = self.store.stage(candidate, request, {"job_id": job_id, "candidate": candidate,
                    "evidence_index": packet["evidence_index"],
                    "captured_at_s": request.at_s, "catalog_at_capture": packet["catalog"]["version"],
                    "claim": "Structure/consistency checked; semantic truth remains an LLM assumption."})
                version = catalog["version"]
                audit["staged_version"] = version
                self.install(catalog, candidate, request, packet,
                             publish=lambda: self.store.activate(version, expected_generation=generation))
                audit["status"], audit["version"] = "installed", version
                break
        except Exception as error:
            audit["error"] = f"{type(error).__name__}: {error}"
        finally:
            audit["elapsed_s"] = time.perf_counter() - started
            audit["model_calls"] = getattr(self.model, "calls", [])[call_start:]
            try:
                (self.directory / f"{job_id}.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
                if hasattr(self.model, "acknowledge_calls"):
                    self.model.acknowledge_calls(call_start + len(audit["model_calls"]))
            except OSError as error:
                audit["audit_error"] = str(error)
            with self._lock:
                self.status.append({key: value for key, value in audit.items() if key not in {"packet", "attempts", "model_calls"}})
                self.pending.discard(request.key())

    def _run(self):
        while not self._stop.is_set() or not self.jobs.empty():
            try:
                request, packet, token = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._process(request, packet, token)
            finally:
                self.jobs.task_done()

    def wait_idle(self, timeout_s=180):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if not self.pending:
                    return True
            self._stop.wait(0.02)
        return False

    def close(self, timeout_s=1):
        self._stop.set()
        self.thread.join(timeout_s)
        return not self.thread.is_alive()
