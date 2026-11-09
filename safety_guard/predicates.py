import copy
import dataclasses
import json
import pathlib
import queue
import threading
import time
import uuid
from collections import deque

from .logic import prove, references
from .types import text, timestamp


def validate_evaluation(evaluation, predicates):
    fields = {"method", "question", "yes_value", "when", "query_interval_s", "true_hold_s", "false_hold_s", "max_frame_age_s"}
    if not isinstance(evaluation, dict) or set(evaluation) != fields or evaluation["method"] != "visual_question":
        raise ValueError("Invalid predicate evaluation descriptor")
    text(evaluation["question"], 1000)
    if type(evaluation["yes_value"]) is not bool:
        raise ValueError("Visual answer polarity must be Boolean")
    if references(evaluation["when"]) - set(predicates):
        raise ValueError("Visual query condition references an undeclared predicate")
    for name in ("query_interval_s", "true_hold_s", "false_hold_s", "max_frame_age_s"):
        if not 0 < timestamp(evaluation[name]) <= 86400:
            raise ValueError("Visual query intervals and hold durations must be positive and bounded")


def validate_evaluation_graph(predicates):
    visited, active = set(), set()

    def visit(name):
        if name in active:
            raise ValueError("Cyclic visual query conditions")
        if name in visited:
            return
        active.add(name)
        evaluation = predicates[name].get("evaluation")
        if evaluation:
            validate_evaluation(evaluation, predicates)
            for dependency in references(evaluation["when"]):
                visit(dependency)
        active.remove(name)
        visited.add(name)

    for name in predicates:
        visit(name)


class LocalVisualQuestions:
    def __init__(self, model):
        self.model = copy.copy(model)
        self.model.calls = []

    def answer(self, question, scene):
        started = len(self.model.calls)
        try:
            result = self.model.answer_visual_question(question, scene["images"])
            return result | {"model_calls": copy.deepcopy(self.model.calls[started:])}
        finally:
            self.model.acknowledge_calls(len(self.model.calls))


class FineARTVisualQuestions:
    def __init__(self, policy, tokenizer, camera_keys, model_id):
        if not callable(getattr(policy, "select_message", None)):
            raise ValueError("FineART backend requires a policy with a trained text generation interface")
        self.camera_keys = tuple(camera_keys)
        if not 1 <= len(self.camera_keys) <= 3 or len(set(self.camera_keys)) != len(self.camera_keys):
            raise ValueError("Supply one to three distinct, ordered robot camera keys")
        if set(self.camera_keys) - set(policy.config.image_features):
            raise ValueError("Camera keys do not match the pretrained policy")
        self.policy, self.tokenizer = policy, tokenizer
        self.model_id = text(model_id, 300)

    def answer(self, question, scene):
        import hashlib
        import io

        import torch
        from PIL import Image

        if len(scene["images"]) != len(self.camera_keys):
            raise ValueError("Each frame must provide the configured robot camera views in order")
        device = next(self.policy.parameters()).device
        prompt = (f"User: {text(question, 1000)} Answer only yes, no, or unknown. "
                  "Use unknown if the robot gripper or requested property is not observable. "
                  "Absence of visible droplets does not establish physical dryness.\nAssistant:")
        tokens = self.tokenizer(prompt, return_tensors="pt", add_special_tokens=True)
        limit = getattr(self.policy.config, "tokenizer_max_length", 200)
        if tokens["input_ids"].shape[-1] > limit:
            raise ValueError("Visual question exceeds the policy token budget; no text was truncated")
        batch = {"observation.language.tokens": tokens["input_ids"].to(device),
                 "observation.language.attention_mask": tokens["attention_mask"].bool().to(device)}
        sources = []
        for key, image_path in zip(self.camera_keys, scene["images"], strict=True):
            path = pathlib.Path(image_path)
            if path.stat().st_size > 12000000:
                raise ValueError("Robot frame exceeds 12 MB")
            raw = path.read_bytes()
            with Image.open(io.BytesIO(raw)) as picture:
                if max(picture.size) > 4096:
                    raise ValueError("Robot frame dimension exceeds 4096")
                picture = picture.convert("RGB")
                pixels = torch.frombuffer(bytearray(picture.tobytes()), dtype=torch.uint8)
                batch[key] = pixels.reshape(picture.height, picture.width, 3).permute(2, 0, 1).unsqueeze(0).float().to(device) / 255
            sources.append({"camera_key": key, "path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()})
        with torch.inference_mode():
            raw_answer = self.policy.select_message(batch, tokenizer=self.tokenizer, max_new_tokens=16, temperature=0.0)
        answer = raw_answer.strip().casefold().rstrip(".")
        if answer not in {"yes", "no", "unknown"}:
            answer = "unknown"
        return {"answer": answer, "raw_answer": raw_answer, "evidence": raw_answer or "No textual answer",
                "backend": self.model_id, "image_sources": sources,
                "capability": "Native FineART text interface; wetness accuracy has not been established"}


class PredicateWorker:
    def __init__(self, backend, publish, directory, min_interval_s=0.5, on_complete=None):
        self.backend = backend
        self.publish = publish
        self.on_complete = on_complete
        self.directory = pathlib.Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.min_interval_s = timestamp(min_interval_s)
        self._jobs = queue.Queue(maxsize=32)
        self._pending = set()
        self._last_capture = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_started = 0.0
        self.status = deque(maxlen=128)
        self.thread = threading.Thread(target=self._run, name="visual-predicate-queries", daemon=True)
        self.thread.start()

    def refresh(self, names, catalog, values, scene, at_s):
        ordered, visited = [], set()

        def visit(name):
            if name in visited:
                return
            visited.add(name)
            definition = catalog["predicates"].get(name, {})
            evaluation = definition.get("evaluation")
            if evaluation:
                for dependency in sorted(references(evaluation["when"])):
                    visit(dependency)
                ordered.append(name)

        for name in sorted(names):
            visit(name)
        if scene is None or not scene.images:
            return False
        captured = dataclasses.asdict(scene)
        for name in ordered:
            definition = catalog["predicates"][name]
            evaluation = definition["evaluation"]
            if prove(evaluation["when"], values).value != "safe":
                continue
            if not 0 <= at_s - scene.at_s <= evaluation["max_frame_age_s"]:
                continue
            with self._lock:
                previous = self._last_capture.get(name)
                interval = max(self.min_interval_s, evaluation["query_interval_s"])
                if self._stop.is_set() or name in self._pending or previous is not None and scene.at_s - previous < interval:
                    continue
                job = {"id": uuid.uuid4().hex, "predicate": name, "definition": copy.deepcopy(definition),
                       "catalog_version": catalog["version"], "scene": captured, "requested_at_s": at_s}
                try:
                    self._jobs.put_nowait(job)
                except queue.Full:
                    continue
                self._pending.add(name)
                self._last_capture[name] = scene.at_s
        with self._lock:
            return bool(self._pending & set(ordered))

    def _run(self):
        while not self._stop.is_set() or not self._jobs.empty():
            try:
                job = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            started = time.perf_counter()
            record = copy.deepcopy(job)
            try:
                delay = self.min_interval_s - (time.monotonic() - self._last_started)
                if delay > 0 and self._stop.wait(delay):
                    record["status"] = "cancelled"
                else:
                    self._last_started = time.monotonic()
                    result = self.backend.answer(job["definition"]["evaluation"]["question"], job["scene"])
                    if not isinstance(result, dict) or result.get("answer") not in {"yes", "no", "unknown"}:
                        raise ValueError("Visual backend must return yes, no or unknown")
                    record["result"] = result
                    record["publication"] = self.publish(job, result)
                    record["status"] = "completed"
            except Exception as error:
                record["status"], record["error"] = "error", f"{type(error).__name__}: {error}"
            finally:
                record["elapsed_s"] = time.perf_counter() - started
                try:
                    (self.directory / f"{job['id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as error:
                    record["audit_error"] = f"{type(error).__name__}: {error}"
                finally:
                    with self._lock:
                        self._pending.discard(job["predicate"])
                    if self.on_complete and not self._stop.is_set():
                        try:
                            self.on_complete()
                        except Exception as error:
                            record["refresh_error"] = f"{type(error).__name__}: {error}"
                    self.status.append({key: value for key, value in record.items()
                                        if key in {"id", "predicate", "status", "publication", "error", "audit_error", "refresh_error", "elapsed_s"}})
                    self._jobs.task_done()

    def wait_idle(self, timeout_s):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not self._jobs.unfinished_tasks:
                return True
            self._stop.wait(0.01)
        return not self._jobs.unfinished_tasks

    def close(self, timeout_s=1):
        self._stop.set()
        self.thread.join(timeout_s)
        return not self.thread.is_alive()
