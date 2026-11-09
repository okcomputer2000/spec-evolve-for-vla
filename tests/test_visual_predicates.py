import copy
import json
import pathlib
import tempfile
import threading
import time
import unittest
from collections import deque

from safety_guard import ActionRequest, SafetyGuard, Scene
from safety_guard.catalog import CatalogStore, empty_catalog, validate_candidate
from safety_guard.local import LocalModel


def request(at_s=0):
    return ActionRequest("insert plug", "insert plug into outlet", at_s, {"effector": "gripper", "target": "outlet"})


def proposal():
    definitions = []
    for name, subject, question, condition in (
        ("gripper_wet", "gripper", "Is the robot gripper wet?", True),
        ("target_electrical", "outlet", "Is the target an electrical outlet?", {"ref": "gripper_wet"}),
    ):
        definitions.append({"id": name, "subject": subject, "kind": "state", "description": question,
            "default": None, "default_basis": "Requires actual visual evidence", "valid_for_s": None,
            "evaluation": {"method": "visual_question", "question": question, "yes_value": True,
                "when": condition, "query_interval_s": 1, "true_hold_s": 300,
                "false_hold_s": 1, "max_frame_age_s": 2}})
    return {"mode": "extend", "contract_id": "insert_plug", "predicates": definitions,
        "event_rules": [], "time_rules": [], "inferred_facts": [],
        "contract": {"id": "insert_plug", "task": request().task, "action": request().action,
            "bindings": request().bindings, "description": "Scripted visual-predicate integration fixture",
            "pre": {"op": "not", "args": [{"op": "and", "args": [
                {"ref": "gripper_wet"}, {"ref": "target_electrical"}]}]},
            "hold": True, "post": True, "coverage": "Synthetic integration only",
            "rationale": "Not a physical risk model or a benchmark result"},
        "rationale": "Hand-authored engineering fixture"}


class ScriptedVisualBackend:
    def __init__(self, wet_answers=("yes",)):
        self.answers = deque(wet_answers)
        self.calls = []

    def answer(self, question, scene):
        self.calls.append({"question": question, "at_s": scene["at_s"]})
        answer = self.answers.popleft() if question == "Is the robot gripper wet?" else "yes"
        return {"answer": answer, "backend": "scripted_unit_fixture", "evidence": "Synthetic response"}


class VisualPredicateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.backend = ScriptedVisualBackend()
        CatalogStore(self.directory.name).commit(proposal(), request(), {"source": "scripted_unit_fixture"})
        self.guard = SafetyGuard(self.directory.name, predicate_backend=self.backend)
        self.addCleanup(self.guard.close)
        self.guard.predicate_worker.min_interval_s = 0
        self.guard.observe_scene(Scene(0, "Robot camera frame", images=("immutable_fixture.jpg",), interpretation_required=False))

    def settle(self):
        self.assertTrue(self.guard.wait_idle(5))

    def state(self):
        return self.guard.snapshot()["state"]["gripper_wet"]

    def start(self):
        self.assertEqual(self.guard.check(request()).value, "unknown")
        self.settle()

    def test_conditional_query_runs_after_wetness_without_another_check(self):
        self.start()
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(self.guard.check(request()).value, "unsafe")

    def test_dry_gripper_skips_electrical_query(self):
        self.backend.answers = deque(["no"])
        self.start()
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(self.guard.check(request()).value, "safe")

    def test_fresh_positive_keeps_querying_and_renews_from_capture_time(self):
        self.backend.answers = deque(["yes", "yes"])
        self.start()
        self.guard.observe_frame(10, ["new_immutable_frame.jpg"])
        self.settle()
        self.assertEqual(self.state()["at_s"], 10)
        self.assertEqual(self.state()["valid_for_s"], 300)
        self.assertEqual(self.guard.check(request(309)).value, "unsafe")
        self.assertEqual(self.guard.check(request(310)).value, "unknown")
        self.assertIsNone(self.state()["value"])

    def test_negative_and_unknown_observations_do_not_clear_positive_hold(self):
        self.backend.answers = deque(["yes", "no", "unknown"])
        self.start()
        for at_s in (10, 20):
            self.guard.observe_frame(at_s, [f"immutable_{at_s}.jpg"])
            self.settle()
            self.assertIs(self.state()["value"], True)
            self.assertEqual(self.state()["at_s"], 0)
        self.guard.check(request(300))
        self.assertIsNone(self.state()["value"])

    def test_same_frame_and_stale_frame_cannot_renew_hold(self):
        self.start()
        self.guard.check(request(1))
        self.guard.check(request(10))
        self.settle()
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(self.state()["at_s"], 0)

    def test_new_frames_do_not_fill_semantic_scene_history(self):
        for at_s in range(1, 160):
            self.guard.observe_frame(at_s, [f"immutable_{at_s}.jpg"])
        self.assertEqual(len(self.guard._scenes), 1)
        self.assertEqual(self.guard._scene_revision, 0)

    def test_delayed_positive_after_expiration_still_uses_capture_time(self):
        self.start()
        job = {"id": "delayed", "predicate": "gripper_wet", "scene": {"at_s": 250},
               "definition": copy.deepcopy(self.guard.catalog["predicates"]["gripper_wet"])}
        self.guard.check(request(310))
        result = self.guard._publish_visual(job, {"answer": "yes", "backend": "scripted_unit_fixture"})
        self.assertEqual(result, "published")
        self.assertEqual(self.state()["at_s"], 250)
        self.assertIs(self.state()["value"], True)
        self.guard.check(request(550))
        self.assertIsNone(self.state()["value"])

    def test_older_result_cannot_override_newer_observation(self):
        self.backend.answers = deque(["yes", "yes"])
        self.start()
        self.guard.observe_frame(10, ["new.jpg"])
        self.settle()
        job = {"id": "old", "predicate": "gripper_wet", "scene": {"at_s": 0},
               "definition": self.guard.catalog["predicates"]["gripper_wet"]}
        self.assertEqual(self.guard._publish_visual(job, {"answer": "yes"}), "newer_state_exists")
        self.assertEqual(self.state()["at_s"], 10)

    def test_synchronous_check_does_not_wait_for_visual_backend(self):
        started, release = threading.Event(), threading.Event()
        original = self.backend.answer
        def answer(question, scene):
            started.set()
            if not release.wait(5):
                raise TimeoutError("Test release missing")
            return original(question, scene)
        self.backend.answer = answer
        self.addCleanup(release.set)
        before = time.perf_counter()
        self.assertTrue(self.guard.check(request()).pending)
        self.assertLess(time.perf_counter() - before, 0.5)
        self.assertTrue(started.wait(2))
        release.set()
        self.settle()

    def test_visual_values_cannot_come_from_text_or_static_defaults(self):
        for mode in ("default", "text_fact"):
            candidate = proposal()
            if mode == "default":
                candidate["predicates"][0]["default"] = True
            else:
                candidate["inferred_facts"] = [{"predicate": "gripper_wet", "value": True,
                    "at_s": 0, "basis": "Text-only guess", "valid_for_s": 300}]
            with self.assertRaises(ValueError):
                validate_candidate(candidate, empty_catalog(), request())

    def test_cyclic_query_conditions_are_rejected(self):
        candidate = proposal()
        candidate["predicates"][0]["evaluation"]["when"] = {"ref": "target_electrical"}
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            validate_candidate(candidate, empty_catalog(), request())

    def test_timer_cannot_clear_visual_wetness_as_a_drying_guarantee(self):
        candidate = proposal()
        candidate["time_rules"] = [{"id": "assume_dry", "anchor": "gripper_wet", "anchor_value": True,
            "after_s": 300, "when": True, "effects": [{"predicate": "gripper_wet", "value": False}],
            "rationale": "Invalid assumption that time guarantees dryness"}]
        with self.assertRaisesRegex(ValueError, "expiration becomes unknown"):
            validate_candidate(candidate, empty_catalog(), request())

    def test_enabled_local_model_gets_an_isolated_visual_backend(self):
        model = LocalModel(use_visual_predicates=True, vision_model="separate_local_vision")
        with tempfile.TemporaryDirectory() as directory, SafetyGuard(directory, model) as guard:
            self.assertIsNotNone(guard.predicate_worker)
            self.assertIsNot(guard.predicate_worker.backend.model, model)
            self.assertIsNot(guard.predicate_worker.backend.model.calls, model.calls)

    def test_query_audit_retains_capture_time_and_backend(self):
        self.start()
        records = [json.loads(path.read_text(encoding="utf-8"))
                   for path in (pathlib.Path(self.directory.name) / "visual_queries").glob("*.json")]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["scene"]["at_s"] == 0 for record in records))
        self.assertTrue(all(record["result"]["backend"] == "scripted_unit_fixture" for record in records))
