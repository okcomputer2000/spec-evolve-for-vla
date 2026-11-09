import re

from .logic import prove


def decompose_question(sample):
    question = sample["question"].strip()
    numbered = re.findall(r"(?:^|\n)\s*(\d+)[.)、]\s*(.*?)(?=\n\s*\d+[.)、]|\Z)", question, re.S)
    grouped = numbered and all(re.fullmatch(r"\d+(?:\s*[,、]\s*\d+)*", option.strip()) for option in sample["options"])
    if grouped:
        numbers = [int(number) for number, _ in numbered]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("Numbered action list must be contiguous")
        actions = [value.strip() for _, value in numbered]
        choices = [[int(value) - 1 for value in re.findall(r"\d+", option)] for option in sample["options"]]
        context = question.split("\n", 1)[0]
    else:
        actions = list(sample["options"])
        choices = [[index] for index in range(len(actions))]
        context = question
    if any(len(set(choice)) != len(choice) or any(index < 0 or index >= len(actions) for index in choice) for choice in choices):
        raise ValueError("Invalid original answer-option mapping")
    negative = bool(re.search(r"\b(?:not advisable|unsafe|not safe|inappropriate)\b|不可取|不安全", context, re.I))
    return {"context": context, "actions": actions, "choices": choices,
            "target": "unsafe" if negative else "safe", "grouped": bool(grouped)}


def reconstruct_answer(decomposition, verdicts):
    values = {f"action_{index}": {"predicate": f"action_{index}",
              "value": True if verdict == "safe" else False if verdict == "unsafe" else None,
              "basis": "Formal action-safety verdict, not the dataset answer"}
              for index, verdict in enumerate(verdicts)}
    proofs = []
    for choice in decomposition["choices"]:
        arguments = []
        for index in range(len(decomposition["actions"])):
            expression = {"ref": f"action_{index}"}
            should_be_safe = (index in choice) == (decomposition["target"] == "safe")
            arguments.append(expression if should_be_safe else {"op": "not", "args": [expression]})
        proofs.append(prove({"op": "and", "args": arguments}, values).value)
    entailed = [index for index, value in enumerate(proofs) if value == "safe"]
    return {"prediction": entailed[0] if len(entailed) == 1 else None, "option_proofs": proofs,
            "method": "Exact action-safety pattern matched to original options; ambiguity remains an abstention."}
