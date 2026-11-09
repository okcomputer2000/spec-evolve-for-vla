import copy
import dataclasses
import json
import threading
import time

from .catalog import CatalogStore, mapping_key
from .logic import monitor_trace, prove, references
from .predicates import LocalVisualQuestions, PredicateWorker
from .state import StateEngine
from .types import ActionRequest, Fact, Update, Verdict, timestamp
from .worker import SemanticWorker


def conjunction(*expressions):
    return {"op": "and", "args": list(expressions)}


class SafetyGuard:
    def __init__(self, directory, model=None, max_updates=2048, predicate_backend=None):
        self.store = CatalogStore(directory)
        self.catalog = copy.deepcopy(self.store.current)
        self.engine = StateEngine(self.catalog)
        self.max_updates = max_updates
        self._updates = []
        self._sequence = 0
        self._observation_revision = 0
        self._scene = None
        self._scenes = []
        self._scene_revision = 0
        self._uninterpreted_update_revision = 0
        self._resolved_contexts = {}
        self._clock = 0.0
        self._fault = None
        self._lock = threading.RLock()
        self._active = {}
        self._visual_demands = set()
        if predicate_backend is None and getattr(model, "use_visual_predicates", False):
            predicate_backend = LocalVisualQuestions(model)
        self.predicate_worker = PredicateWorker(predicate_backend, self._publish_visual, self.store.directory / "visual_queries",
            on_complete=self._refresh_visual) if predicate_backend else None
        self.worker = SemanticWorker(self.store, model, self._install, self.store.directory / "jobs") if model else None

    def _advance(self, at_s):
        timestamp(at_s)
        if at_s < self._clock:
            raise ValueError("External timestamps must be nondecreasing; delayed model evidence is replayed internally")
        self._clock = at_s
        try:
            self.engine.advance(at_s)
            self._audit_active()
        except ValueError as error:
            self._fault = str(error)

    def _append(self, kind, data):
        with self._lock:
            if data.at_s < self._clock:
                raise ValueError("External updates must be supplied chronologically")
            if len(self._updates) >= self.max_updates:
                self._fault = "Episode history budget exceeded; no history was silently discarded"
                raise ValueError(self._fault)
            self._sequence += 1
            update = Update(data.at_s, kind, copy.deepcopy(data), self._sequence)
            self._updates.append(update)
            self._observation_revision += 1
            if kind == "event" and data.completed and not any(rule["event"] == data.name and rule["subject"] == data.subject
                    for rule in self.catalog["event_rules"].values()):
                self._uninterpreted_update_revision += 1
            elif kind == "fact" and data.predicate not in self.catalog["predicates"]:
                self._uninterpreted_update_revision += 1
            self._clock = data.at_s
            try:
                self.engine.apply(update)
                self._audit_active()
            except ValueError as error:
                self._fault = str(error)

    def observe_fact(self, fact):
        self._append("fact", fact)

    def record_event(self, event):
        self._append("event", event)

    def observe_scene(self, scene):
        with self._lock:
            if len(self._scenes) >= 128:
                self._fault = "Scene history budget exceeded; start a new episode"
                raise ValueError(self._fault)
            self._advance(scene.at_s)
            previous = dataclasses.asdict(self._scene) if self._scene else None
            current = dataclasses.asdict(scene)
            if previous:
                previous.pop("at_s")
            current.pop("at_s")
            if scene.interpretation_required and (scene.images or current != previous):
                self._scene_revision += 1
            self._scene = copy.deepcopy(scene)
            self._scenes.append(self._scene)
            self._observation_revision += 1

    def _packet(self, request, diagnostics):
        return {"request": dataclasses.asdict(request), "catalog": copy.deepcopy(self.catalog),
                "scene": dataclasses.asdict(self._scene) if self._scene else None,
                "scene_history": [dataclasses.asdict(scene) for scene in self._scenes],
                "scene_revision": self._scene_revision,
                "uninterpreted_update_revision": self._uninterpreted_update_revision,
                "active_executions": {name: {"contract_id": execution["contract"]["id"],
                    "start_s": execution["start_s"], "hold_violation": execution["violation"],
                    "hold_was_unknown": execution["unknown"]} for name, execution in self._active.items()},
                "state": copy.deepcopy(self.engine.values),
                "timeline": [dataclasses.asdict(update) for update in self._updates],
                "diagnostics": diagnostics,
                "time_semantics": "Seconds from episode start; captured times are not response arrival times."}

    def observe_frame(self, at_s, images):
        with self._lock:
            if self._scene is None:
                raise ValueError("Register a scene before supplying frames")
            scene = dataclasses.replace(self._scene, at_s=at_s, images=tuple(images), interpretation_required=False)
            self._advance(at_s)
            self._scene = scene
            self._observation_revision += 1
            self._refresh_visual()

    def _refresh_visual(self):
        with self._lock:
            if self.predicate_worker:
                return self.predicate_worker.refresh(self._visual_demands, self.catalog, self.engine.values,
                    self._scene, self._clock)
            return False

    def _publish_visual(self, job, result):
        with self._lock:
            name, captured_at = job["predicate"], job["scene"]["at_s"]
            if self.catalog["predicates"].get(name) != job["definition"]:
                return "definition_changed"
            current = self.engine.values.get(name, {})
            observed_at = current.get("expired_observation_at_s", current.get("at_s", 0))
            if observed_at > captured_at:
                return "newer_state_exists"
            if result["answer"] == "unknown":
                return "unknown_observation_retains_unexpired_state"
            evaluation = job["definition"]["evaluation"]
            value = evaluation["yes_value"] if result["answer"] == "yes" else not evaluation["yes_value"]
            if not value and current.get("value") is True and current["at_s"] + evaluation["true_hold_s"] > captured_at:
                return "negative_observation_does_not_clear_positive_hold"
            duration = evaluation["true_hold_s"] if value else evaluation["false_hold_s"]
            if captured_at + duration <= self._clock:
                return "observation_already_expired"
            if len(self._updates) >= self.max_updates:
                self._fault = "Episode history budget exceeded"
                return "history_budget_exhausted"
            fact = Fact(name, value, captured_at, "visual_predicate", json.dumps({
                "query_id": job["id"], "question": evaluation["question"], "answer": result["answer"],
                "backend": result.get("backend"), "evidence": result.get("evidence"),
                "image_sources": result.get("image_sources", [])}, ensure_ascii=False), duration)
            sequence = self._sequence + 1
            updates = self._updates + [Update(captured_at, "fact", fact, sequence)]
            engine = StateEngine()
            engine.rebuild(self.catalog, updates, self._clock)
            active = copy.deepcopy(self._active)
            self._audit_active(replay=True, engine=engine, active=active)
            self.engine, self._updates, self._sequence = engine, updates, sequence
            self._active = active
            self._observation_revision += 1
            return "published"

    def _enqueue(self, request, diagnostics):
        if self.worker is None:
            return "no_local_model"
        revision = (self._observation_revision, self._context(request))
        return self.worker.submit(copy.deepcopy(request), self._packet(request, diagnostics), revision)

    def _unknown(self, reason, request, contract_id=None, evidence=(), counterexamples=()):
        status = self._enqueue(request, {"reason": reason, "evidence": evidence, "counterexamples": counterexamples})
        return Verdict("unknown", (reason, status), self.catalog["version"], contract_id,
                       tuple(evidence), tuple(counterexamples), status in {"queued", "pending"})

    def _context(self, request, scene_revision=None, update_revision=None):
        return (self._scene_revision if scene_revision is None else scene_revision,
                self._uninterpreted_update_revision if update_revision is None else update_revision,
                json.dumps(request.context, sort_keys=True, ensure_ascii=False, separators=(",", ":")))

    def check(self, request):
        if not self._lock.acquire(blocking=False):
            return Verdict("unknown", ("state_publication_in_progress",), self.catalog["version"], pending=True)
        try:
            self._advance(request.at_s)
            if self._fault:
                return Verdict("unknown", (self._fault,), self.catalog["version"])
            contract_id = self.catalog["mappings"].get(mapping_key(request.task, request.action, request.bindings))
            if contract_id is None:
                return self._unknown("unmapped_task_or_action_text", request)
            contract = self.catalog["contracts"][contract_id]
            if request.phase != "pre":
                execution = self._active.get(request.execution_id)
                if not execution or execution["key"] != request.key():
                    return Verdict("unknown", ("missing_or_mismatched_execution_context",), self.catalog["version"], contract_id)
                contract = execution["contract"]
                contract_id = contract["id"]
                if execution["violation"]:
                    return Verdict("unsafe", ("hold_condition_violated_during_execution",), self.catalog["version"],
                                   contract_id, tuple(execution["violation"]))
            context = self._context(request)
            expected = self._resolved_contexts.get((request.key(), contract_id), (0, 0, "{}"))
            if context != expected:
                reason = "uninterpreted_event_or_fact" if context[1] != expected[1] else "uninterpreted_scene_or_action_context"
                return self._unknown(reason, request, contract_id)
            expression = conjunction(contract[request.phase], contract["hold"]) if request.phase != "hold" else contract["hold"]
            evaluated = {name for name in references(expression) if self.catalog["predicates"][name].get("evaluation")}
            self._visual_demands = references(expression) | set().union(
                *(references(execution["contract"]["hold"]) for execution in self._active.values()))
            visual_pending = self._refresh_visual()
            proof = prove(expression, self.engine.values)
            if request.phase != "pre":
                execution = self._active[request.execution_id]
                if proof.value == "safe" and execution["unknown"]:
                    return self._unknown("hold_condition_was_unknown_during_execution", request, contract_id, proof.evidence)
            if proof.value == "unknown":
                missing = {name for name in references(expression) if self.engine.values.get(name, {}).get("value") is None}
                if missing and missing <= evaluated:
                    return Verdict("unknown", ("visual_predicates_pending" if visual_pending else "visual_predicates_need_fresh_evidence",),
                                   self.catalog["version"], contract_id, proof.evidence, (proof.counterexample,), visual_pending)
                return self._unknown("formal_condition_underdetermined", request, contract_id, proof.evidence, (proof.counterexample,))
            return Verdict(proof.value, ("formal_condition_entailed" if proof.value == "safe" else "formal_condition_refuted",),
                           self.catalog["version"], contract_id, proof.evidence, (proof.counterexample,) if proof.counterexample else ())
        finally:
            self._lock.release()

    def begin(self, execution_id, request):
        if request.phase != "pre":
            raise ValueError("begin requires a precondition request")
        with self._lock:
            if execution_id in self._active or len(self._active) >= 16:
                raise ValueError("Duplicate execution ID or active execution budget exceeded")
            verdict = self.check(request)
            if verdict.allowed:
                contract = copy.deepcopy(self.catalog["contracts"][verdict.contract_id])
                self._active[execution_id] = {"contract": contract, "key": request.key(), "start_s": request.at_s,
                    "request": copy.deepcopy(request),
                    "cursor": len(self.engine.trace), "trace": [(request.at_s, copy.deepcopy(self.engine.values))],
                    "violation": [], "unknown": False}
            return verdict

    def _audit_active(self, replay=False, engine=None, active=None):
        engine = self.engine if engine is None else engine
        active = self._active if active is None else active
        for execution in active.values():
            if replay:
                earlier = [values for at_s, values in engine.trace if at_s <= execution["start_s"]]
                initial = [(execution["start_s"], earlier[-1])] if earlier else []
                fresh = initial + [(at_s, values) for at_s, values in engine.trace if at_s > execution["start_s"]]
                execution["trace"] = []
            else:
                fresh = engine.trace[execution["cursor"]:]
            for at_s, values in fresh:
                if at_s < execution["start_s"]:
                    continue
                result = prove(execution["contract"]["hold"], values)
                execution["trace"].append((at_s, values))
                if result.value == "unsafe" and not execution["violation"]:
                    execution["violation"] = [{"at_s": at_s, "evidence": result.evidence}]
                if result.value == "unknown":
                    execution["unknown"] = True
            execution["cursor"] = len(engine.trace)

    def finish(self, execution_id, at_s):
        with self._lock:
            execution = self._active.get(execution_id)
            if execution is None:
                raise ValueError("No active execution")
            contract = execution["contract"]
            request = dataclasses.replace(execution["request"], at_s=at_s, phase="post", execution_id=execution_id)
            verdict = self.check(request)
            trace = execution["trace"] + [(at_s, copy.deepcopy(self.engine.values))]
            report = monitor_trace(contract["hold"], trace)
            if report["value"] == "unsafe":
                verdict = Verdict("unsafe", ("rtamt_hold_trace_violation",), self.catalog["version"], contract["id"],
                                  ({"trace_report": report},))
            elif report["value"] == "unknown" and verdict.allowed:
                verdict = Verdict("unknown", ("rtamt_hold_trace_incomplete",), self.catalog["version"], contract["id"],
                                  ({"trace_report": report},))
            del self._active[execution_id]
            return {"verdict": verdict.as_dict(), "hold_monitor": report}

    def _install(self, catalog, candidate, request, packet, publish=None):
        facts = [Fact(**entry, source="llm_inference") for entry in candidate["inferred_facts"]]
        with self._lock:
            if len(self._updates) + len(facts) > self.max_updates:
                raise ValueError("Inferred facts exceed episode history budget")
            sequence = self._sequence
            inferred_updates = [Update(fact.at_s, "fact", fact, sequence + index + 1) for index, fact in enumerate(facts)]
            updates = list(self._updates) + inferred_updates
            captured_sequence, captured_clock = self._sequence, self._clock
        engine = StateEngine()
        engine.rebuild(catalog, updates, captured_clock)
        with self._lock:
            later = [update for update in self._updates if update.sequence > captured_sequence]
            if len(self._updates) + len(facts) > self.max_updates:
                raise ValueError("Inferred facts exceed episode history budget after concurrent updates")
            for update in later:
                engine.apply(update)
            engine.advance(self._clock)
            sequence = self._sequence
            updates = list(self._updates) + [dataclasses.replace(update, sequence=sequence + index + 1)
                                            for index, update in enumerate(inferred_updates)]
            updates.sort(key=lambda update: (update.at_s, update.sequence))
            captured_sequence = max([0] + [entry["sequence"] for entry in packet["timeline"]])
            new_updates = [update for update in updates if update.sequence > captured_sequence]
            recognized = all(update.data.predicate in catalog["predicates"] if update.kind == "fact" else
                not update.data.completed or any(rule["event"] == update.data.name and rule["subject"] == update.data.subject
                    for rule in catalog["event_rules"].values()) for update in new_updates)
            update_revision = self._uninterpreted_update_revision if recognized else packet["uninterpreted_update_revision"]
            contexts = self._resolved_contexts | {(request.key(), candidate["contract_id"]): self._context(
                request, packet["scene_revision"], update_revision)}
            active = {name: execution.copy() for name, execution in self._active.items()}
            self._audit_active(replay=True, engine=engine, active=active)
            if publish is not None:
                publish()
            self.catalog, self.engine = catalog, engine
            self._updates, self._sequence = updates, sequence + len(inferred_updates)
            self._resolved_contexts, self._active = contexts, active
            contract = catalog["contracts"][candidate["contract_id"]]
            self._visual_demands |= references(conjunction(contract[request.phase], contract["hold"]))
            self._refresh_visual()

    def snapshot(self):
        with self._lock:
            return {"at_s": self._clock, "version": self.catalog["version"], "state": copy.deepcopy(self.engine.values),
                    "fault": self._fault, "active_executions": list(self._active)}

    def wait_idle(self, timeout_s=180):
        deadline = time.monotonic() + timeout_s
        semantic_idle = self.worker.wait_idle(timeout_s) if self.worker else True
        visual_idle = self.predicate_worker.wait_idle(max(0, deadline - time.monotonic())) if self.predicate_worker else True
        return semantic_idle and visual_idle

    def close(self, timeout_s=1):
        semantic_closed = self.worker.close(timeout_s) if self.worker else True
        visual_closed = self.predicate_worker.close(timeout_s) if self.predicate_worker else True
        return semantic_closed and visual_closed

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        self.close()
