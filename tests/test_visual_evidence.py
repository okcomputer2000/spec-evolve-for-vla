import copy
import json
import pathlib
import tempfile
import unittest

import httpx
from PIL import Image
from tokenizers import Tokenizer, models, pre_tokenizers

from safety_guard import ActionRequest, SafetyGuard, Scene
from safety_guard.evidence import index_evidence, validate_sources
from safety_guard.local import LocalModel
from safety_guard.predicates import LocalVisualQuestions
from safety_guard.types import Fact


def report(certainty="visible"):
    return {"description": "Water droplets cover the hand surface. The socket power state is not visible.",
            "objects": [{"id": "hand", "name": "hand", "visible_states": ["water droplets"],
                         "relations": ["near socket"], "uncertainties": []}],
            "observations": [{"id": "water_droplets", "text": "Water droplets cover the hand surface.",
                              "object_ids": ["hand"], "certainty": certainty}],
            "uncertainties": ["Electrical power cannot be established from this image."]}


class VisualEvidenceTests(unittest.TestCase):
    def test_short_visual_question_is_isolated_and_occlusion_remains_unknown(self):
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"done_reason": "stop", "message": {"content": json.dumps({
                "answer": "yes", "visibility": "occluded", "evidence": "The robot gripper is occluded."})}})
        model = LocalModel(vision_model="separate_local_vision", transport=httpx.MockTransport(respond))
        model.calls.append({"stage": "unrelated_text_inference"})
        backend = LocalVisualQuestions(model)
        with tempfile.TemporaryDirectory() as directory:
            image = pathlib.Path(directory) / "synthetic_adapter_fixture.png"
            Image.new("RGB", (8, 8), "black").save(image)
            result = backend.answer("Is the robot gripper wet?", {"images": [str(image)]})
        self.assertEqual(result["answer"], "unknown")
        self.assertEqual(model.calls, [{"stage": "unrelated_text_inference"}])
        self.assertEqual(len(result["model_calls"]), 1)
        self.assertEqual(captured[0]["model"], "separate_local_vision")
        self.assertEqual(captured[0]["options"]["num_predict"], 256)
        self.assertEqual(json.loads(captured[0]["messages"][1]["content"]), {"question": "Is the robot gripper wet?"})
        self.assertEqual(len(result["image_sources"][0]["sha256"]), 64)

    def test_text_thinking_setting_does_not_enable_vision_thinking(self):
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"done_reason": "stop", "message": {"content": "{}"}})
        model = LocalModel(think=True, vision_model="vision-only", transport=httpx.MockTransport(respond))
        model._chat("text", {}, {}, model.model)
        model._chat("vision", {}, {}, model.vision_model, images=["encoded_test_image"])
        self.assertTrue(captured[0]["think"])
        self.assertFalse(captured[1]["think"])
        self.assertEqual(captured[1]["options"]["num_predict"], 8192)

    def test_failed_local_call_keeps_inputs_for_diagnosis(self):
        def respond(request):
            raise httpx.ReadTimeout("local inference timed out", request=request)
        model = LocalModel(transport=httpx.MockTransport(respond))
        packet = {"evidence": "original scene"}
        with self.assertRaises(httpx.ReadTimeout):
            model._chat("Inspect evidence.", packet, {}, model.model)
        self.assertEqual(model.calls[0]["packet"], packet)
        packet["evidence"] = "later changed context"
        self.assertEqual(model.calls[0]["packet"]["evidence"], "original scene")
        self.assertIn("ReadTimeout", model.calls[0]["error"])
        self.assertGreaterEqual(model.calls[0]["elapsed_s"], 0)

    def test_token_counting_preserves_full_evidence_and_rejects_real_overflow(self):
        captured = []
        def respond(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"done_reason": "stop", "message": {"content": "{}"}})
        tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "tokenizer.json"
            tokenizer.save(str(path))
            model = LocalModel(num_ctx=4096, max_output_tokens=1024, tokenizer_path=path,
                               transport=httpx.MockTransport(respond))
            packet = {"evidence": "observation " * 600}
            model._chat("Read all evidence.", packet, {}, model.model)
            self.assertEqual(json.loads(captured[0]["messages"][1]["content"]), packet)
            self.assertEqual(model.calls[0]["budget_method"], "local_model_tokenizer_plus_768_template_margin")
            with self.assertRaisesRegex(ValueError, "no evidence was truncated"):
                model._chat("Read all evidence.", {"evidence": "observation " * 4000}, {}, model.model)
            self.assertEqual(len(captured), 1)

    def test_repair_feedback_preserves_hazard_polarity(self):
        model = LocalModel()
        candidate = {"contract": {"pre": {"op": "not", "args": [{"ref": "harm_exposure"}]},
                                   "hold": {"op": "not", "args": [False]}}}
        feedback = model.feedback_candidate(candidate)
        self.assertEqual(feedback["contract"]["pre_hazard"], {"ref": "harm_exposure"})
        self.assertIs(feedback["contract"]["hold_hazard"], False)
        self.assertNotIn("pre", feedback["contract"])
        self.assertIn("pre", candidate["contract"])

    def test_text_reasoner_does_not_silently_receive_images(self):
        for vision_model in (None, "qwen3.5:9b"):
            model = LocalModel(vision_model=vision_model)
            with self.assertRaisesRegex(ValueError, "separate vision_model"):
                model.describe({}, ["nonexistent.png"])

    def test_external_vision_text_is_formal_evidence(self):
        fixture = pathlib.Path(__file__).resolve().parents[1] / "examples" / "wet_socket_contract.json"
        proposal = json.loads(fixture.read_text(encoding="utf-8"))
        class TextModel:
            def resolve(self, packet):
                self.packet = copy.deepcopy(packet)
                source = next(name for name, entry in packet["evidence_index"].items()
                              if entry.get("locator") == "visual_0_water_droplets")
                answer = copy.deepcopy(proposal)
                answer["inferred_facts"] = [{"predicate": "hand_wet", "value": True, "at_s": 1,
                    "basis": "Independent visual model described water droplets, not a power measurement.",
                    "valid_for_s": None, "evidence_ids": [source]}]
                return answer
        model = TextModel()
        with tempfile.TemporaryDirectory() as directory, SafetyGuard(directory, model) as guard:
            guard.observe_scene(Scene(1, "External perception snapshot", visual_report=report()))
            guard.observe_scene(Scene(2, "Later frame has no new visual information"))
            request = ActionRequest("插插座", "将插头插入插座", 2, {"effector": "hand", "target": "socket"})
            self.assertEqual(guard.check(request).value, "unknown")
            self.assertTrue(guard.wait_idle(5))
            verdict = guard.check(request)
            self.assertEqual(verdict.value, "unsafe")
            wet = next(entry for entry in verdict.evidence if entry["predicate"] == "hand_wet")
            self.assertTrue(wet["evidence_ids"])
            self.assertEqual(model.packet["visual_reports"][0]["at_s"], 1)

    def test_uncertain_visual_statement_cannot_become_definite_fact(self):
        evidence = {"visible_source": {"at_s": 3, "certainty": "uncertain"}}
        candidate = {"predicates": [], "event_rules": [], "time_rules": [], "inferred_facts": [
            {"predicate": "hand_wet", "value": True, "at_s": 3, "evidence_ids": ["visible_source"]}]}
        with self.assertRaisesRegex(ValueError, "uncertain visual"):
            validate_sources(candidate, evidence)
        candidate["inferred_facts"][0]["value"] = None
        validate_sources(candidate, evidence)

    def test_false_is_not_established_by_an_empty_source_list(self):
        candidate = {"predicates": [], "event_rules": [], "time_rules": [], "inferred_facts": [
            {"predicate": "gate_closed", "value": False, "at_s": 0, "evidence_ids": []}]}
        with self.assertRaisesRegex(ValueError, "must cite supporting evidence"):
            validate_sources(candidate, {})
        candidate["inferred_facts"][0]["value"] = None
        validate_sources(candidate, {})

    def test_source_ids_follow_content_and_cannot_be_backdated(self):
        packet = {"request": {"task": "inspect", "action": "touch", "at_s": 3}}
        first = index_evidence(packet)
        packet["request"]["action"] = "wash"
        second = index_evidence(packet)
        first_action = next(name for name, entry in first.items() if entry["locator"] == "request_action")
        second_action = next(name for name, entry in second.items() if entry["locator"] == "request_action")
        self.assertNotEqual(first_action, second_action)
        candidate = {"predicates": [], "event_rules": [], "time_rules": [], "inferred_facts": [
            {"predicate": "wet", "value": True, "at_s": 1, "evidence_ids": [first_action]}]}
        with self.assertRaisesRegex(ValueError, "predates"):
            validate_sources(candidate, first)
        self.assertEqual(Fact("wet", True, 3, "observation", "water", evidence_ids=[first_action]).evidence_ids,
                         (first_action,))


if __name__ == "__main__":
    unittest.main()
