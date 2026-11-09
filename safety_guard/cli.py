import argparse
import json
import pathlib
import time

from .local import LocalModel
from .runtime import SafetyGuard
from .types import ActionRequest, Event, Fact, Scene


class FileProposal:
    def __init__(self, path):
        self.candidate = json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))

    def resolve(self, packet):
        return self.candidate


def replay(args):
    model = LocalModel.from_config(args.config) if args.local else FileProposal(args.proposal_file) if args.proposal_file else None
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with SafetyGuard(args.catalog, model) as guard, output.open("w", encoding="utf-8") as stream:
        for number, line in enumerate(pathlib.Path(args.input).read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            entry = json.loads(line)
            kind, payload = entry["type"], entry.get("data", {})
            record = {"line": number, "type": kind, "backend": "local_ollama" if args.local else "scripted_fixture" if model else "formal_only"}
            started = time.perf_counter()
            if kind == "scene":
                guard.observe_scene(Scene(**payload))
            elif kind == "frame":
                guard.observe_frame(payload["at_s"], payload["images"])
            elif kind == "fact":
                guard.observe_fact(Fact(**payload))
            elif kind == "event":
                guard.record_event(Event(**payload))
            elif kind in {"check", "begin"}:
                request = ActionRequest(**payload)
                verdict = guard.begin(entry["execution_id"], request) if kind == "begin" else guard.check(request)
                record["initial_verdict"] = verdict.as_dict()
                record["fast_call_s"] = time.perf_counter() - started
                if args.wait and verdict.pending:
                    deadline = time.monotonic() + args.timeout
                    rounds = []
                    while verdict.pending and time.monotonic() < deadline:
                        completed = guard.wait_idle(max(0, deadline - time.monotonic()))
                        verdict = guard.check(request)
                        rounds.append({"background_completed": completed, "verdict": verdict.as_dict()})
                        if not completed:
                            break
                    record["background_completed"] = all(item["background_completed"] for item in rounds)
                    record["enrichment_rounds"] = rounds
                    record["after_enrichment"] = verdict.as_dict()
                    if kind == "begin" and verdict.allowed:
                        record["after_enrichment"] = guard.begin(entry["execution_id"], request).as_dict()
            elif kind == "finish":
                record["result"] = guard.finish(entry["execution_id"], payload["at_s"])
            else:
                raise ValueError(f"Unsupported record type: {kind}")
            record["elapsed_s"] = time.perf_counter() - started
            record["snapshot"] = guard.snapshot()
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            if "initial_verdict" in record:
                final = record.get("after_enrichment", record["initial_verdict"])
                print(f"line {number}: {record['initial_verdict']['value']} -> {final['value']} ({record['elapsed_s']:.2f}s)")
        if args.wait:
            guard.wait_idle(args.timeout)
        stream.write(json.dumps({"type": "summary", "worker_status": list(guard.worker.status) if guard.worker else [],
                                 "predicate_status": list(guard.predicate_worker.status) if guard.predicate_worker else [],
                                 "snapshot": guard.snapshot()}, ensure_ascii=False) + "\n")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Asynchronous local semantic safety guard")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("replay", help="Replay a small chronological JSONL episode")
    command.add_argument("input")
    command.add_argument("--output", required=True)
    command.add_argument("--catalog", required=True, help="Versioned knowledge directory; use a fresh directory for independent runs")
    backend = command.add_mutually_exclusive_group()
    backend.add_argument("--local", action="store_true")
    backend.add_argument("--proposal-file", help="Explicitly scripted proposal for deterministic engineering demos")
    command.add_argument("--config", default="llm_local_config.json")
    command.add_argument("--wait", action="store_true", help="Offline replay only: wait for asynchronous enrichment")
    command.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    return replay(args)
