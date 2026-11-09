import argparse
import hashlib
import json
import pathlib
import subprocess
import sys


def summarize(directory):
    records = [json.loads(line) for line in (directory / "replay.jsonl").read_text(encoding="utf-8").splitlines()]
    inputs = [json.loads(line) for line in (directory / "input.jsonl").read_text(encoding="utf-8").splitlines()]
    checks = []
    for record in records:
        if record["type"] != "check":
            continue
        request = inputs[record["line"] - 1]["data"]
        verdict = record.get("after_enrichment", record["initial_verdict"])
        version = verdict["version"]
        catalog_path = directory / "catalog" / f"catalog_{version:06d}.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
        contract = catalog.get("contracts", {}).get(verdict.get("contract_id"))
        checks.append({"task": request["task"], "at_s": request["at_s"],
                       "initial": record["initial_verdict"]["value"], "verdict": verdict,
                       "elapsed_s": record["elapsed_s"], "snapshot": record["snapshot"],
                       "contract": contract, "event_rules": catalog.get("event_rules", {}),
                       "time_rules": catalog.get("time_rules", {})})
    jobs = [json.loads(path.read_text(encoding="utf-8")) for path in (directory / "catalog" / "jobs").glob("*.json")]
    summary = {"checks": checks, "jobs": [{key: job.get(key) for key in
               ("job_id", "request", "status", "error", "elapsed_s", "version")} for job in jobs],
               "scope": "Synthetic text execution reports; no robot, VLA execution, or measured wetness."}
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Run and preserve a small natural-language task sequence")
    parser.add_argument("--input", type=pathlib.Path, default=pathlib.Path("examples/wash_tasks_raw.jsonl"))
    parser.add_argument("--config", type=pathlib.Path, default=pathlib.Path("llm_local_config.json"))
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    directory = args.output_dir.resolve()
    if not args.summarize_only:
        directory.mkdir(parents=True, exist_ok=False)
        input_path, config_path = args.input.resolve(), args.config.resolve()
        (directory / "input.jsonl").write_bytes(input_path.read_bytes())
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if config.get("tokenizer_path"):
            config["tokenizer_path"] = str((config_path.parent / config["tokenizer_path"]).resolve())
        (directory / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sources = list((root / "safety_guard").glob("*.py")) + [pathlib.Path(__file__), input_path, config_path]
        manifest = {str(path.relative_to(root)) if path.is_relative_to(root) else str(path):
                    hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        result = subprocess.run([sys.executable, "-u", "-m", "safety_guard", "replay", str(directory / "input.jsonl"),
            "--local", "--wait", "--timeout", "900", "--config", str(directory / "config.json"),
            "--catalog", str(directory / "catalog"), "--output", str(directory / "replay.jsonl")], cwd=root)
        if result.returncode:
            return result.returncode
    summary = summarize(directory)
    for check in summary["checks"]:
        print(f"{check['task']}: {check['initial']} -> {check['verdict']['value']} ({check['elapsed_s']:.1f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
