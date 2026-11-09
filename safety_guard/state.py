import dataclasses
import json

from .evidence import source_id
from .logic import prove, references


class StateEngine:
    def __init__(self, catalog=None):
        self.values = {}
        self.at_s = 0.0
        self._serial = 0
        self._fired = set()
        self.trace = []
        self.catalog = catalog or {"version": 0, "predicates": {}, "event_rules": {}, "time_rules": {}}
        for definition in self.catalog["predicates"].values():
            self._write(definition["id"], definition["default"], 0.0, "catalog_default",
                        definition["default_basis"], definition["valid_for_s"], definition.get("evidence_ids", ()))
        self._record()

    def _write(self, predicate, value, at_s, source, basis, valid_for_s=None, evidence_ids=()):
        self._serial += 1
        self.values[predicate] = {"predicate": predicate, "value": value, "at_s": at_s,
                                  "source": source, "basis": basis, "valid_for_s": valid_for_s,
                                  "revision": self._serial, "catalog_version": self.catalog["version"],
                                  "evidence_ids": tuple(sorted(set(evidence_ids)))}

    def _effects(self, selected, uncertain, at_s, source, evidence_ids=()):
        grouped = {}
        for certain, rules in ((True, selected), (False, uncertain)):
            for rule in rules:
                support = set(evidence_ids) | set(rule.get("evidence_ids", []))
                for name in references(rule["when"]) | ({rule["anchor"]} if "anchor" in rule else set()):
                    support.update(self.values.get(name, {}).get("evidence_ids", ()))
                for effect in rule["effects"]:
                    grouped.setdefault(effect["predicate"], []).append((effect["value"], rule["id"], certain, support))
        for name, entries in grouped.items():
            definite = {entry[0] for entry in entries if entry[2]}
            possible = {entry[0] for entry in entries}
            if not definite:
                possible.add(self.values.get(name, {}).get("value"))
            support = set().union(*(entry[3] for entry in entries))
            if not definite:
                support.update(self.values.get(name, {}).get("evidence_ids", ()))
            value = next(iter(possible)) if len(possible) == 1 else None
            self._write(name, value, at_s, source if value is not None else "uncertain_or_conflicting_rules",
                        ";".join(entry[1] for entry in entries), evidence_ids=support)

    def _advance_deadlines(self, at_s, include_endpoint):
        transitions = 0
        while True:
            timers = []
            for rule in self.catalog["time_rules"].values():
                anchor = self.values.get(rule["anchor"])
                if anchor is None or anchor["value"] is not rule["anchor_value"]:
                    continue
                token = (rule["id"], anchor["revision"])
                if token not in self._fired:
                    timers.append((anchor["at_s"] + rule["after_s"], token, rule))
            expirations = [(fact["at_s"] + fact["valid_for_s"], name) for name, fact in self.values.items()
                           if fact["value"] is not None and fact["valid_for_s"] is not None]
            deadlines = [item[0] for item in timers] + [item[0] for item in expirations]
            if not deadlines:
                break
            deadline = min(deadlines)
            if deadline > at_s or not include_endpoint and deadline == at_s:
                break
            transitions += 1
            if transitions > 256 or len(self.trace) > 8192:
                raise ValueError("State transition budget exceeded; start a new episode")
            for expiry, name in expirations:
                if expiry == deadline:
                    fact = self.values[name]
                    self._write(name, None, deadline, "expired", fact["basis"], evidence_ids=fact.get("evidence_ids", ()))
                    self.values[name]["expired_observation_at_s"] = fact["at_s"]
            selected, uncertain = [], []
            for due, token, rule in timers:
                if due != deadline:
                    continue
                self._fired.add(token)
                anchor = self.values[rule["anchor"]]
                if anchor["revision"] != token[1] or anchor["value"] is not rule["anchor_value"]:
                    continue
                result = prove(rule["when"], self.values).value
                if result == "safe":
                    selected.append(rule)
                elif result == "unknown":
                    uncertain.append(rule)
            self._effects(selected, uncertain, deadline, "catalog_timer_rule")
            self.at_s = deadline
            self._record()
        self.at_s = at_s

    def advance(self, at_s):
        if at_s < self.at_s:
            raise ValueError("State clock cannot move backwards")
        self._advance_deadlines(at_s, True)

    def _record(self):
        self.trace.append((self.at_s, {name: dict(fact) for name, fact in self.values.items()}))

    def apply(self, update):
        if update.at_s < self.at_s:
            raise ValueError("Late updates require chronological replay")
        self._advance_deadlines(update.at_s, False)
        self.at_s = update.at_s
        expired = False
        for name, previous in list(self.values.items()):
            if previous["value"] is not None and previous["valid_for_s"] is not None and previous["at_s"] + previous["valid_for_s"] <= update.at_s:
                self._write(name, None, update.at_s, "expired", previous["basis"], evidence_ids=previous.get("evidence_ids", ()))
                self.values[name]["expired_observation_at_s"] = previous["at_s"]
                expired = True
        if expired:
            self._record()
        if update.kind == "fact":
            fact = update.data
            if fact.predicate in self.catalog["predicates"]:
                previous = self.values.get(fact.predicate)
                inferred = fact.source == "llm_inference"
                derived_previous = previous and previous["source"] in {"catalog_event_rule", "catalog_timer_rule", "uncertain_or_conflicting_rules"}
                if inferred and derived_previous:
                    return
                explicit_previous = previous and previous["source"] not in {"catalog_default", "llm_inference", "expired"}
                if inferred and explicit_previous and previous["at_s"] >= fact.at_s and (previous["value"] is not None or previous["source"] == "conflicting_facts"):
                    return
                value, source, basis = fact.value, fact.source, fact.basis
                if explicit_previous and not derived_previous and not inferred and previous["at_s"] == fact.at_s and previous["value"] is not fact.value:
                    value, source = None, "conflicting_facts"
                    basis = previous["basis"] + "; " + fact.basis
                support = set(fact.evidence_ids)
                if source == "conflicting_facts":
                    support.update(previous.get("evidence_ids", ()))
                self._write(fact.predicate, value, fact.at_s, source, basis, fact.valid_for_s, support)
        elif update.kind == "event" and update.data.completed:
            event = update.data
            selected, uncertain = [], []
            for rule in self.catalog["event_rules"].values():
                if rule["event"] != event.name or rule["subject"] != event.subject:
                    continue
                result = prove(rule["when"], self.values).value
                if result == "safe":
                    selected.append(rule)
                elif result == "unknown":
                    uncertain.append(rule)
            event_source = source_id({"at_s": update.at_s, "source": event.source,
                                      "text": json.dumps(dataclasses.asdict(event), ensure_ascii=False)})
            self._effects(selected, uncertain, update.at_s, "catalog_event_rule", (event_source,))
        self._record()

    def rebuild(self, catalog, updates, at_s):
        self.__init__(catalog)
        for update in sorted((item for item in updates if item.at_s <= at_s), key=lambda item: (item.at_s, item.sequence)):
            self.apply(update)
        self.advance(at_s)
        return self.values
