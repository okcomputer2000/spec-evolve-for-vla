import copy
import unittest

from safety_guard.evidence import index_evidence
from safety_guard.local import LocalModel


def packet():
    value = {"request": {"task": "future task", "action": "future action", "at_s": 20},
             "catalog": {"event_rules": {}}, "timeline": [
                 {"at_s": 10, "kind": "event", "sequence": 1, "data": {
                     "name": "record_001", "subject": "hand", "at_s": 10, "completed": True,
                     "source": "execution_report", "description": "An activity has completed."}}],
             "scene_history": [{"at_s": 0, "description": "Initial scene.", "objects": [{"id": "hand"}],
                                "source": "scene"},
                               {"at_s": 15, "description": "A later observation.", "objects": [], "source": "scene"}]}
    value["evidence_index"] = index_evidence(value)
    return value


class CompletedEventTests(unittest.TestCase):
    def test_reading_excludes_future_task_and_observations_and_is_cached(self):
        model = LocalModel(interpret_completed_events=True)
        captured = []

        def respond(system, supplied, schema, selected_model):
            captured.append(copy.deepcopy(supplied))
            return {"changes": [], "rationale": "No supported persistent change."}

        model._chat = respond
        original = packet()
        first = model._interpret_completed_events(original)
        changed_request = copy.deepcopy(original)
        changed_request["request"]["action"] = "a different future action"
        changed_request["evidence_index"] = index_evidence(changed_request)
        second = model._interpret_completed_events(changed_request)
        self.assertEqual(first, second)
        self.assertEqual(len(captured), 1)
        self.assertNotIn("request", captured[0])
        self.assertEqual([scene["at_s"] for scene in captured[0]["scene_history"]], [0])
        self.assertTrue(all(entry["at_s"] <= 10 for entry in captured[0]["evidence_index"].values()))
        self.assertTrue(all(entry["source"] != "proposed_action" for entry in captured[0]["evidence_index"].values()))
        self.assertEqual(original["catalog"], {"event_rules": {}})

    def test_known_rules_and_uncompleted_commands_do_not_trigger_reading(self):
        model = LocalModel(interpret_completed_events=True)

        def unexpected(*arguments):
            self.fail("A known or uncompleted event must not trigger semantic reading")

        model._chat = unexpected
        supplied = packet()
        supplied["timeline"][0]["data"]["completed"] = False
        self.assertEqual(model._interpret_completed_events(supplied), [])
        supplied["timeline"][0]["data"]["completed"] = True
        supplied["catalog"]["event_rules"]["known"] = {"event": "record_001", "subject": "hand"}
        self.assertEqual(model._interpret_completed_events(supplied), [])

    def test_unresolved_reading_cannot_claim_definite_state(self):
        model = LocalModel(interpret_completed_events=True)
        supplied = packet()
        event_source = next(name for name, entry in supplied["evidence_index"].items() if entry["locator"] == "event_1")
        model._chat = lambda *arguments: {"changes": [{"subject": "hand", "state_description": "A physical state.",
            "value": True, "basis_type": "unresolved", "basis": "Insufficient evidence.",
            "evidence_ids": [event_source]}], "rationale": "Unresolved."}
        with self.assertRaisesRegex(ValueError, "retain null"):
            model._interpret_completed_events(supplied)


if __name__ == "__main__":
    unittest.main()
