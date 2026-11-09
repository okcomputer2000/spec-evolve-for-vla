import dataclasses
import json
import math
import re
import unicodedata
from typing import Any


ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


def identifier(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid identifier: {value!r}")
    return value


def timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Time must be a finite nonnegative number of seconds")
    return float(value)


def text(value, limit=12000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Expected nonempty bounded text")
    return value.strip()


def normalize(value):
    return " ".join(unicodedata.normalize("NFKC", text(value)).casefold().split()).rstrip(".。!?！？")


@dataclasses.dataclass(frozen=True)
class Fact:
    predicate: str
    value: bool | None
    at_s: float
    source: str
    basis: str
    valid_for_s: float | None = None
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.predicate)
        timestamp(self.at_s)
        text(self.source, 200)
        text(self.basis)
        if self.value is not None and type(self.value) is not bool:
            raise ValueError("A predicate value is bool or unknown")
        if self.valid_for_s is not None:
            timestamp(self.valid_for_s)
        if not isinstance(self.evidence_ids, (tuple, list)) or len(self.evidence_ids) > 32:
            raise ValueError("Fact evidence must be a bounded list of source IDs")
        for name in self.evidence_ids:
            identifier(name)
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))


@dataclasses.dataclass(frozen=True)
class Event:
    name: str
    subject: str
    at_s: float
    completed: bool = True
    source: str = "execution_report"
    description: str | None = None

    def __post_init__(self):
        identifier(self.name)
        identifier(self.subject)
        timestamp(self.at_s)
        text(self.source, 200)
        if self.description is not None:
            text(self.description)
        if type(self.completed) is not bool:
            raise ValueError("completed must be bool")


@dataclasses.dataclass(frozen=True)
class Scene:
    at_s: float
    description: str
    objects: tuple[dict, ...] = ()
    uncertainties: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    source: str = "structured_text"
    interpretation_required: bool = True
    visual_report: dict | None = None

    def __post_init__(self):
        timestamp(self.at_s)
        text(self.description, 32000)
        text(self.source, 200)
        if type(self.interpretation_required) is not bool:
            raise ValueError("interpretation_required must be bool")
        if len(self.objects) > 64 or len(self.images) > 3:
            raise ValueError("Scene exceeds object or image budget")
        object_ids = [identifier(item["id"]) for item in self.objects]
        if len(set(object_ids)) != len(object_ids):
            raise ValueError("Duplicate scene object identifiers")
        if self.visual_report is not None:
            from .vision import validate_report
            object.__setattr__(self, "visual_report", validate_report(self.visual_report))


@dataclasses.dataclass(frozen=True)
class ActionRequest:
    task: str
    action: str
    at_s: float
    bindings: dict[str, str] = dataclasses.field(default_factory=dict)
    phase: str = "pre"
    execution_id: str | None = None
    context: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        text(self.task)
        text(self.action)
        timestamp(self.at_s)
        if not isinstance(self.context, dict) or len(json.dumps(self.context, allow_nan=False)) > 32000:
            raise ValueError("Action context must be a bounded JSON object")
        if self.phase not in {"pre", "hold", "post"}:
            raise ValueError("phase must be pre, hold or post")
        if len(self.bindings) > 16:
            raise ValueError("Too many object bindings")
        for key, value in self.bindings.items():
            identifier(key)
            identifier(value)

    def key(self):
        return (normalize(self.task), normalize(self.action), tuple(sorted(self.bindings.items())))


@dataclasses.dataclass(frozen=True)
class Verdict:
    value: str
    reasons: tuple[str, ...]
    version: int
    contract_id: str | None = None
    evidence: tuple[dict, ...] = ()
    counterexamples: tuple[dict, ...] = ()
    pending: bool = False
    scope: str = "Relative to the declared model and its observed or LLM-inferred premises."

    @property
    def allowed(self):
        return self.value == "safe"

    def as_dict(self):
        return dataclasses.asdict(self) | {"allowed": self.allowed}


@dataclasses.dataclass(frozen=True)
class Update:
    at_s: float
    kind: str
    data: Any
    sequence: int
