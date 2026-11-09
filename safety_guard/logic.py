import dataclasses
import json

import rtamt
import z3

from .types import identifier


def references(expression, depth=0):
    if depth > 16:
        raise ValueError("Expression nesting exceeds budget")
    if type(expression) is bool:
        return set()
    if not isinstance(expression, dict):
        raise ValueError("Expressions must use the restricted JSON AST")
    if set(expression) == {"ref"}:
        return {identifier(expression["ref"])}
    if set(expression) != {"op", "args"}:
        raise ValueError("Invalid expression fields")
    operation, arguments = expression["op"], expression["args"]
    if operation not in {"and", "or", "not", "implies"} or not isinstance(arguments, list):
        raise ValueError("Unsupported expression operator")
    if not 1 <= len(arguments) <= 32 or operation == "not" and len(arguments) != 1 or operation == "implies" and len(arguments) != 2:
        raise ValueError("Invalid expression arity")
    if len(json.dumps(expression)) > 16000:
        raise ValueError("Expression exceeds size budget")
    return set().union(*(references(argument, depth + 1) for argument in arguments))


def compile_z3(expression, symbols, context):
    if type(expression) is bool:
        return z3.BoolVal(expression, ctx=context)
    if "ref" in expression:
        return symbols[expression["ref"]]
    arguments = [compile_z3(argument, symbols, context) for argument in expression["args"]]
    return {"and": z3.And, "or": z3.Or, "not": z3.Not, "implies": z3.Implies}[expression["op"]](*arguments)


@dataclasses.dataclass(frozen=True)
class Proof:
    value: str
    evidence: tuple[dict, ...]
    counterexample: dict


def prove(expression, values, timeout_ms=50):
    names = references(expression)
    context = z3.Context()
    symbols = {name: z3.Bool(name, ctx=context) for name in names}
    solver = z3.Solver(ctx=context)
    solver.set(timeout=timeout_ms)
    evidence = []
    tracked = {}
    for name in sorted(names):
        fact = values.get(name)
        if fact is not None:
            evidence.append(dict(fact))
            if type(fact.get("value")) is bool:
                marker = z3.FreshBool("premise", ctx=context)
                solver.assert_and_track(symbols[name] == fact["value"], marker)
                tracked[str(marker)] = name
    compiled = compile_z3(expression, symbols, context)
    solver.push()
    solver.add(z3.Not(compiled))
    negative = solver.check()
    if negative == z3.unsat:
        supporting = {tracked[str(marker)] for marker in solver.unsat_core()}
        return Proof("safe", tuple(dict(values[name]) for name in sorted(supporting)), {})
    counterexample = {name: z3.is_true(solver.model().eval(symbol, model_completion=True)) for name, symbol in symbols.items()} if negative == z3.sat else {}
    solver.pop()
    solver.add(compiled)
    positive = solver.check()
    if positive == z3.unsat:
        supporting = {tracked[str(marker)] for marker in solver.unsat_core()}
        return Proof("unsafe", tuple(dict(values[name]) for name in sorted(supporting)), counterexample)
    return Proof("unknown", tuple(evidence), counterexample)


def stl(expression):
    if type(expression) is bool:
        return "(guard_constant >= 0)" if expression else "(guard_constant < 0)"
    if "ref" in expression:
        return f"({expression['ref']} >= 0)"
    arguments = [stl(argument) for argument in expression["args"]]
    if expression["op"] == "not":
        return f"(not {arguments[0]})"
    return "(" + f" {expression['op']} ".join(arguments) + ")"


def make_monitor(expression):
    if "guard_constant" in references(expression):
        raise ValueError("guard_constant is reserved for the STL compiler")
    monitor = rtamt.StlDenseTimeSpecification()
    for name in sorted(references(expression) | {"guard_constant"}):
        monitor.declare_var(name, "float")
    monitor.spec = stl(expression)
    monitor.parse()
    return monitor


def monitor_trace(expression, trace):
    if not trace:
        return {"value": "unknown", "reason": "empty_execution_trace"}
    monitor = make_monitor(expression)
    names = references(expression)
    unknown_times = []
    definite_violations = []
    points = {}
    for at_s, values in trace:
        result = prove(expression, values).value
        if result == "unsafe":
            definite_violations.append(at_s)
        if result == "unknown":
            unknown_times.append(at_s)
        else:
            points[at_s] = {name: {"value": values.get(name, {}).get("value", True) is not False} for name in names}
    arguments = [[name, [[at_s, 1.0 if name == "guard_constant" or values[name]["value"] else -1.0] for at_s, values in sorted(points.items())]] for name in sorted(names | {"guard_constant"})]
    robustness = monitor.evaluate(*arguments) if points else []
    violation_times = sorted(set(definite_violations + [point[0] for point in robustness if point[1] < 0]))
    return {"value": "unsafe" if violation_times else "unknown" if unknown_times else "safe",
            "violation_times": violation_times, "unknown_times": unknown_times,
            "robustness": robustness, "stl": stl(expression),
            "scope": "RTAMT evaluation of supplied piecewise-constant state reports; unobserved physical behavior is not certified."}
