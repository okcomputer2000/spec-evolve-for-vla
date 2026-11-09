import json
import pathlib
import tempfile
import threading
import unittest

import httpx
from PIL import Image

from safety_guard import ActionRequest, Event, Fact, SafetyGuard, Scene
from safety_guard.catalog import CatalogStore, empty_catalog, validate_candidate
from safety_guard.local import LocalModel
from safety_guard.logic import monitor_trace, prove
from safety_guard.state import StateEngine
from safety_guard.types import Update


FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "examples" / "wet_socket_contract.json"


def candidate():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def request(at_s=2, action="将插头插入插座", phase="pre", execution_id=None):
    return ActionRequest("插插座", action, at_s, {"effector": "hand", "target": "socket"}, phase, execution_id)


def catalog():
    return validate_candidate(candidate(), empty_catalog(), request())


class ScriptedModel:
    def __init__(self, gate=None, transform=None):
        self.gate = gate
        self.transform = transform
        self.started = threading.Event()
        self.calls = []

    def resolve(self, packet):
        self.started.set()
        self.calls.append(packet)
        if self.gate is not None:
            if not self.gate.wait(5):
                raise TimeoutError("Test gate timed out")
        return self.transform(candidate(), packet) if self.transform else candidate()


class StateTests(unittest.TestCase):
    def setUp(self):
        self.engine = StateEngine(catalog())
        self.sequence = 0

    def event(self, name, at_s, completed=True):
        self.sequence += 1
        self.engine.apply(Update(at_s, "event", Event(name, "hand", at_s, completed), self.sequence))

    def value(self, name="hand_wet"):
        return self.engine.values[name]["value"]

    def test_command_is_not_completed_event(self):
        self.event("water_contact", 1)
        self.event("drying_started", 2, completed=False)
        self.engine.advance(100)
        self.assertIs(self.value(), True)

    def test_timer_requires_actual_drying_and_rewet_cancels(self):
        self.event("water_contact", 1)
        self.engine.advance(40)
        
        self.assertIs(self.value(), True)
        self.event("drying_started", 41)
        self.event("water_contact", 45)
        self.engine.advance(55)
        self.assertIs(self.value(), True)

    def test_repeated_anchor_restarts_timer(self):
        self.event("water_contact", 1)
        self.event("drying_started", 2)
        self.event("drying_started", 8)
        self.engine.advance(12)
        self.assertIs(self.value(), True)
        self.engine.advance(18)
        self.assertIs(self.value(), False)

    def test_incoming_event_at_deadline_precedes_timer(self):
        self.event("water_contact", 1)
        self.event("drying_started", 2)
        self.event("water_contact", 12)
        self.engine.advance(12)
        self.assertIs(self.value(), True)

    def test_expiration_is_unknown_and_recorded_at_deadline(self):
        self.engine.apply(Update(1, "fact", Fact("hand_wet", True, 1, "observation", "visible water", 3), 1))
        self.engine.advance(20)
        self.assertIsNone(self.value())
        self.assertTrue(any(at_s == 4 and values["hand_wet"]["value"] is None for at_s, values in self.engine.trace))

    def test_uncertain_guard_does_not_silently_skip_possible_change(self):
        self.engine.catalog["event_rules"]["water_wets_hand"]["when"] = {"ref": "drying_active"}
        self.engine.apply(Update(0, "fact", Fact("hand_wet", False, 0, "observation", "dry"), 1))
        self.event("water_contact", 1)
        self.assertIsNone(self.value())

    def test_conflicting_reports_are_unknown(self):
        self.engine.apply(Update(1, "fact", Fact("hand_wet", True, 1, "observer_a", "water"), 1))
        self.engine.apply(Update(1, "fact", Fact("hand_wet", False, 1, "observer_b", "dry"), 2))
        self.assertIsNone(self.value())

    def test_guard_does_not_use_fact_expiring_at_event_timestamp(self):
        self.engine.catalog["event_rules"]["start_controlled_drying"]["when"] = {"ref": "hand_wet"}
        self.engine.apply(Update(1, "fact", Fact("hand_wet", True, 1, "observation", "Wet", 3), 1))
        self.event("drying_started", 4)
        self.assertIsNone(self.value("drying_active"))

    def test_inference_cannot_overwrite_same_time_observation(self):
        updates = [Update(1, "fact", Fact("hand_wet", True, 1, "observation", "wet"), 1),
                   Update(1, "fact", Fact("hand_wet", False, 1, "llm_inference", "assumed dry"), 2)]
        self.engine.rebuild(catalog(), updates, 2)
        self.assertIs(self.value(), True)


class LogicTests(unittest.TestCase):
    def test_three_values(self):
        expression = {"op": "not", "args": [{"ref": "wet"}]}
        self.assertEqual(prove(expression, {}).value, "unknown")
        self.assertEqual(prove(expression, {"wet": {"value": True}}).value, "unsafe")
        self.assertEqual(prove(expression, {"wet": {"value": False}}).value, "safe")

    def test_rtamt_does_not_lose_violation_at_duplicate_timestamp(self):
        trace = [(0, {"wet": {"value": False}}), (1, {"wet": {"value": True}}), (1, {"wet": {"value": False}})]
        report = monitor_trace({"op": "not", "args": [{"ref": "wet"}]}, trace)
        self.assertEqual(report["value"], "unsafe")
        self.assertIn(1, report["violation_times"])

    def test_rtamt_single_sample_and_unknown_gap(self):
        self.assertEqual(monitor_trace(True, [(0, {})])["value"], "safe")
        report = monitor_trace({"ref": "wet"}, [(0, {"wet": {"value": True}}), (2, {})])
        self.assertEqual(report["value"], "unknown")

    def test_unknown_boolean_expression_is_not_executed(self):
        with self.assertRaises(ValueError):
            prove({"python": "__import__('os').system('echo no')"}, {})


class CatalogTests(unittest.TestCase):
    def test_events_cannot_turn_proposal_content_or_dispositions_into_world_state(self):
        for kind in ("proposal", "disposition"):
            proposal = candidate()
            next(entry for entry in proposal["predicates"] if entry["id"] == "hand_wet")["kind"] = kind
            with self.assertRaisesRegex(ValueError, "only state predicates"):
                validate_candidate(proposal, empty_catalog(), request())
            proposal = candidate()
            next(entry for entry in proposal["predicates"] if entry["id"] == "socket_live")["kind"] = kind
            self.assertEqual(validate_candidate(proposal, empty_catalog(), request())["predicates"]["socket_live"]["kind"], kind)

    def test_timer_cannot_start_from_an_unexecuted_proposal_or_disposition(self):
        for kind in ("proposal", "disposition"):
            proposal = candidate()
            proposal["event_rules"] = []
            next(entry for entry in proposal["predicates"] if entry["id"] == "drying_active")["kind"] = kind
            with self.assertRaisesRegex(ValueError, "Timer anchors require actual state"):
                validate_candidate(proposal, empty_catalog(), request())

    def test_reject_dynamic_default_and_dead_default(self):
        proposal = candidate()
        proposal["predicates"][0]["default"] = True
        with self.assertRaisesRegex(ValueError, "Mutable predicate"):
            validate_candidate(proposal, empty_catalog(), request())
        proposal = candidate()
        proposal["predicates"].append({"id": "ready", "subject": "hand", "description": "Ready",
            "default": False, "default_basis": "Unsupported initial guess", "valid_for_s": None})
        proposal["contract"]["pre"] = {"ref": "ready"}
        with self.assertRaisesRegex(ValueError, "blocked by its unchanging defaults"):
            validate_candidate(proposal, empty_catalog(), request())

    def test_reject_undeclared_reference(self):
        proposal = candidate()
        proposal["contract"]["pre"] = {"ref": "invented"}
        with self.assertRaisesRegex(ValueError, "Undeclared"):
            validate_candidate(proposal, empty_catalog(), request())

    def test_reject_contradictory_contract(self):
        proposal = candidate()
        proposal["contract"]["pre"] = {"ref": "hand_wet"}
        proposal["contract"]["hold"] = {"op": "not", "args": [{"ref": "hand_wet"}]}
        with self.assertRaisesRegex(ValueError, "Unsatisfiable"):
            validate_candidate(proposal, empty_catalog(), request())

    def test_semantic_alias_reuses_compatible_contract(self):
        proposal = {"mode": "reuse", "contract_id": "insert_socket", "predicates": [], "event_rules": [],
                    "time_rules": [], "contract": None, "inferred_facts": [], "rationale": "Same bound skill"}
        updated = validate_candidate(proposal, catalog(), request(action="把插头接上"))
        self.assertEqual(len(updated["mappings"]), 2)

    def test_rollback_does_not_reuse_version_number(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CatalogStore(directory)
            self.assertEqual(store.commit(candidate(), request(), {}), 1)
            self.assertEqual(store.commit(candidate(), request(), {}), 2)
            store.activate(1)
            self.assertEqual(store.commit(candidate(), request(), {}), 3)
            self.assertEqual(CatalogStore(directory).current["version"], 3)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.guard = None

    def tearDown(self):
        if self.guard:
            self.guard.close(5)
        self.directory.cleanup()

    def create(self, model):
        self.guard = SafetyGuard(self.directory.name, model)
        return self.guard

    def test_completed_action_description_is_preserved_for_semantic_mapping(self):
        model = ScriptedModel()
        guard = self.create(model)
        description = "Execution report: the same hand touched flowing water and retains water droplets."
        guard.record_event(Event("water_contact", "hand", 1, description=description))
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        self.assertEqual(model.calls[0]["timeline"][0]["data"]["description"], description)
        self.assertEqual(guard.check(request(3)).value, "unsafe")

    def test_uninterpreted_completed_event_invalidates_cached_interpretation(self):
        gate = threading.Event()
        model = ScriptedModel()
        guard = self.create(model)
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        guard.observe_fact(Fact("hand_wet", False, 3, "observation", "Dry"))
        self.assertTrue(guard.check(request(3)).allowed)
        model.gate = gate
        guard.record_event(Event("new_contact_event", "hand", 4, description="New unmodeled contact"))
        verdict = guard.check(request(4))
        self.assertEqual(verdict.value, "unknown")
        self.assertIn("uninterpreted_event_or_fact", verdict.reasons)
        gate.set()
        self.assertTrue(guard.wait_idle(5))

    def test_unknown_is_immediate_and_background_replays_newer_event(self):
        gate = threading.Event()
        def transform(proposal, packet):
            proposal["inferred_facts"] = [{"predicate": "hand_wet", "value": False, "at_s": 1,
                                           "basis": "Captured dry hand", "valid_for_s": None}]
            return proposal
        model = ScriptedModel(gate, transform)
        guard = self.create(model)
        initial = guard.check(request())
        self.assertEqual(initial.value, "unknown")
        self.assertTrue(initial.pending)
        self.assertTrue(model.started.wait(1))
        self.assertFalse(gate.is_set())
        guard.record_event(Event("water_contact", "hand", 3))
        gate.set()
        self.assertTrue(guard.wait_idle(5))
        self.assertEqual(guard.check(request(4)).value, "unsafe")
        self.assertEqual(guard.snapshot()["state"]["hand_wet"]["at_s"], 3)
        self.assertEqual(guard.worker.status[-1]["status"], "installed")

    def test_mapping_reuse_and_fast_checks_do_not_call_model(self):
        model = ScriptedModel()
        guard = self.create(model)
        guard.record_event(Event("water_contact", "hand", 1))
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        count = len(model.calls)
        for at_s in range(3, 8):
            self.assertEqual(guard.check(request(at_s)).value, "unsafe")
        self.assertEqual(len(model.calls), count)

    def test_hold_violation_latches_after_state_recovers(self):
        guard = self.create(ScriptedModel())
        guard.record_event(Event("water_contact", "hand", 1))
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        guard.record_event(Event("drying_started", "hand", 3))
        self.assertTrue(guard.begin("run", request(14)).allowed)
        guard.record_event(Event("water_contact", "hand", 15))
        guard.record_event(Event("drying_started", "hand", 16))
        guard.record_event(Event("plug_inserted", "socket", 27))
        report = guard.finish("run", 28)
        self.assertEqual(report["verdict"]["value"], "unsafe")
        self.assertEqual(report["hold_monitor"]["value"], "unsafe")

    def test_postcondition_does_not_assume_command_completed(self):
        guard = self.create(ScriptedModel())
        guard.record_event(Event("water_contact", "hand", 1))
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        guard.record_event(Event("drying_started", "hand", 3))
        self.assertTrue(guard.begin("run", request(14)).allowed)
        report = guard.finish("run", 15)
        self.assertEqual(report["verdict"]["value"], "unknown")

    def test_execution_with_semantic_alias_keeps_original_request(self):
        def transform(proposal, packet):
            if packet["request"]["action"] == "把插头接上":
                return {"mode": "reuse", "contract_id": "insert_socket", "predicates": [], "event_rules": [],
                        "time_rules": [], "contract": None, "inferred_facts": [], "rationale": "Equivalent action"}
            return proposal
        guard = self.create(ScriptedModel(transform=transform))
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        guard.observe_fact(Fact("hand_wet", False, 3, "observation", "Dry"))
        self.assertEqual(guard.begin("alias", request(4, action="把插头接上")).value, "unknown")
        self.assertTrue(guard.wait_idle(5))
        self.assertTrue(guard.begin("alias", request(5, action="把插头接上")).allowed)
        guard.record_event(Event("plug_inserted", "socket", 6))
        self.assertEqual(guard.finish("alias", 7)["verdict"]["value"], "safe")

    def test_invalid_candidate_is_repaired_once_then_not_repeated_forever(self):
        def transform(proposal, packet):
            proposal["contract"]["pre"] = {"ref": "not_declared"}
            return proposal
        model = ScriptedModel(transform=transform)
        guard = self.create(model)
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        self.assertEqual(len(model.calls), 2)

        self.assertEqual(guard.snapshot()["version"], 0)
        self.assertFalse(guard.check(request(3)).pending)
        self.assertEqual(len(model.calls), 2)

    def test_grounding_fills_missing_values_before_solver_decision(self):
        class GroundingModel(ScriptedModel):
            def ground(self, packet):
                return {"facts": [{"predicate": "hand_wet", "value": True, "at_s": 1,
                                   "basis": "Scene at t=1 explicitly describes wet hand", "valid_for_s": None}],
                        "rationale": "Grounded current predicate, not a safety verdict"}
        guard = self.create(GroundingModel())
        guard.observe_scene(Scene(1, "Hand has visible water on its surface"))
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        self.assertEqual(guard.check(request(3)).value, "unsafe")
        self.assertEqual(guard.snapshot()["state"]["hand_wet"]["source"], "llm_inference")

    def test_history_budget_returns_unknown_without_silent_data_loss(self):
        guard = self.create(None)
        guard.max_updates = 1
        guard.record_event(Event("water_contact", "hand", 1))
        with self.assertRaises(ValueError):
            guard.record_event(Event("water_contact", "hand", 2))
        self.assertEqual(guard.check(request(3)).value, "unknown")

    def test_new_scene_cannot_silently_reuse_old_semantics(self):
        gate = threading.Event()
        model = ScriptedModel()
        guard = self.create(model)
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        guard.observe_fact(Fact("hand_wet", False, 3, "observation", "Dry hand"))
        self.assertTrue(guard.check(request(3)).allowed)
        model.gate = gate
        guard.observe_scene(Scene(4, "New scene: nearby water and changed hand condition"))
        verdict = guard.check(request(4))
        self.assertEqual(verdict.value, "unknown")
        self.assertIn("uninterpreted_scene_or_action_context", verdict.reasons)
        gate.set()
        self.assertTrue(guard.wait_idle(5))

    def test_candidate_action_context_is_not_ignored_by_cached_mapping(self):
        model = ScriptedModel()
        guard = self.create(model)
        guard.check(request())
        self.assertTrue(guard.wait_idle(5))
        changed = ActionRequest("插插座", "将插头插入插座", 3,
            {"effector": "hand", "target": "socket"}, context={"trajectory": [[0, 1], [2, 3]], "formal_geometry": "unknown"})
        self.assertEqual(guard.check(changed).value, "unknown")
        self.assertTrue(guard.wait_idle(5))


class LocalTests(unittest.TestCase):
    def test_repair_grammar_excludes_effects_on_proposal_predicates(self):
        captured = []
        def respond(http_request):
            captured.append(json.loads(http_request.content))
            return httpx.Response(200, json={"done_reason": "stop", "message": {"content": "{\"mode\":\"unresolved\"}"}})
        rejected = candidate()
        next(entry for entry in rejected["predicates"] if entry["id"] == "hand_wet")["kind"] = "proposal"
        model = LocalModel(num_ctx=32768, transport=httpx.MockTransport(respond))
        model.resolve({"request": {}, "catalog": empty_catalog(), "validation_feedback": [{
            "error": "Event/time effects may update only state predicates: hand_wet has kind=proposal",
            "rejected_candidate": rejected}]})
        for group in ("event_rules", "time_rules"):
            allowed = captured[0]["format"]["properties"][group]["items"]["properties"]["effects"]["items"]["properties"]["predicate"]["enum"]
            self.assertNotIn("hand_wet", allowed)
            self.assertIn("drying_active", allowed)

    def test_image_adapter_sends_image_to_separate_vision_stage(self):
        captured = []
        def respond(http_request):
            captured.append(json.loads(http_request.content))
            answer = {"description": "A test image", "objects": [], "observations": [], "uncertainties": ["Test image only"]}
            return httpx.Response(200, json={"done_reason": "stop", "message": {"content": json.dumps(answer)}})
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "image.png"
            Image.new("RGB", (8, 8), "white").save(path)
            model = LocalModel(vision_model="vision-only", vision_assistant_prefill="<think>\n</think>\n", transport=httpx.MockTransport(respond))
            description = model.describe({"task": "test"}, [path])
            self.assertEqual(captured[0]["model"], "vision-only")
            self.assertEqual(len(captured[0]["messages"][1]["images"]), 1)
            self.assertNotIn(str(path), captured[0]["messages"][1]["content"])
            self.assertEqual(captured[0]["messages"][-1], {"role": "assistant", "content": "<think>\n</think>\n"})
            self.assertEqual(len(description["image_sources"][0]["sha256"]), 64)

    def test_reject_remote_endpoint(self):
        for endpoint in ("https://example.com", "http://10.0.0.1:11434", "http://127.0.0.1@evil.example", "http://127.0.0.1:11434/path"):
            with self.assertRaises(ValueError):
                LocalModel(base_url=endpoint)

    def test_structured_output_uses_local_ollama_without_proxy(self):
        captured = []
        def respond(http_request):
            captured.append(json.loads(http_request.content))
            proposal = candidate()
            contract = proposal["contract"]
            contract["pre_hazard"] = contract.pop("pre")["args"][0]
            contract["hold_hazard"] = contract.pop("hold")["args"][0]
            for field in ("task", "action", "bindings"):
                contract.pop(field)
            return httpx.Response(200, json={"done_reason": "stop", "message": {"content": json.dumps(proposal)}})
        model = LocalModel(num_ctx=32768, transport=httpx.MockTransport(respond))
        answer = model.resolve({"request": {"task": "插插座", "action": "插插座", "bindings": {"effector": "hand", "target": "socket"}}})
        self.assertEqual(answer["mode"], "extend")
        self.assertEqual(answer["contract"]["task"], "插插座")
        self.assertEqual(answer["contract"]["action"], "插插座")
        self.assertEqual(answer["contract"]["bindings"], {"effector": "hand", "target": "socket"})
        self.assertEqual(answer["contract"]["pre"], candidate()["contract"]["pre"])
        self.assertEqual(prove(answer["contract"]["pre"], {"hand_wet": {"value": True}, "socket_live": {"value": True}}).value, "unsafe")
        self.assertEqual(prove(answer["contract"]["pre"], {"hand_wet": {"value": False}, "socket_live": {"value": True}}).value, "safe")
        self.assertFalse(captured[0]["think"])
        self.assertIn("$defs", captured[0]["format"])
        self.assertEqual(len(model.calls), 1)

    def test_context_overflow_is_explicit(self):
        model = LocalModel(num_ctx=100)
        with self.assertRaisesRegex(ValueError, "no evidence was truncated"):
            model.resolve({"long_context": "a" * 1000})

    def test_grounding_schema_requires_exact_predicate_keys(self):
        captured = []
        def respond(http_request):
            captured.append(json.loads(http_request.content))
            answer = {"values": {"hand_wet": {"value": True, "at_s": 1, "basis": "Visible water", "valid_for_s": None}},
                      "rationale": "Scene evidence"}
            return httpx.Response(200, json={"done_reason": "stop", "message": {"content": json.dumps(answer)}})
        model = LocalModel(transport=httpx.MockTransport(respond))
        result = model.ground({"predicates": [{"id": "hand_wet"}]})
        properties = captured[0]["format"]["properties"]["values"]["properties"]
        self.assertEqual(set(properties), {"hand_wet"})
        self.assertIs(result["facts"][0]["value"], True)


if __name__ == "__main__":
    unittest.main()
