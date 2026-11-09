import unittest

from safety_guard.benchmark import decompose_question, reconstruct_answer


class BenchmarkTests(unittest.TestCase):
    def test_grouped_answers_reconstruct_from_each_action(self):
        sample = {"question": "Which actions are safe?\n1. wash.\n2. contaminate.\n3. dry.\n",
                  "options": ["1, 2", "1, 3", "2, 3"]}
        decomposition = decompose_question(sample)
        self.assertEqual(decomposition["actions"], ["wash.", "contaminate.", "dry."])
        self.assertEqual(reconstruct_answer(decomposition, ["safe", "unsafe", "safe"])["prediction"], 1)

    def test_negative_question_selects_unsafe_action(self):
        decomposition = decompose_question({"question": "Which action is not advisable?",
                                             "options": ["cut off power", "ride a lift during fire", "call for help"]})
        self.assertEqual(reconstruct_answer(decomposition, ["safe", "unsafe", "safe"])["prediction"], 1)

    def test_unknown_and_multiple_safe_actions_do_not_force_an_answer(self):
        decomposition = decompose_question({"question": "Which action is more safe?", "options": ["A", "B"]})
        self.assertIsNone(reconstruct_answer(decomposition, ["safe", "unknown"])["prediction"])
        self.assertIsNone(reconstruct_answer(decomposition, ["safe", "safe"])["prediction"])


if __name__ == "__main__":
    unittest.main()
