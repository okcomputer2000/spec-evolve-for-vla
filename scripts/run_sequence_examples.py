import argparse
import hashlib
import json
import pathlib
import subprocess
import sys


def summarize(path):
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    checks = [record for record in records if record["type"] == "check"]
    last = checks[-1]
    final = last.get("after_enrichment", last["initial_verdict"])
    summary = records[-1]
    return {"following_action": final, "recorded_events": [record["line"] for record in records if record["type"] == "event"],
            "event_backed_evidence": [entry for entry in final["evidence"] if entry.get("source") == "catalog_event_rule"],
            "background_elapsed_s": sum(status["elapsed_s"] for status in summary["worker_status"]),
            "worker_status": summary["worker_status"],
            "matched_expected_final": final["value"] == "unsafe"}


def main():
    parser = argparse.ArgumentParser(description="Run only the two local history-dependent safety examples")
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    parser.add_argument("--case", choices=["tap_socket", "trash_water", "all"], default="all")
    parser.add_argument("--config", default="llm_local_config.json")
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()
    root = pathlib.Path(__file__).resolve().parents[1]
    cases = {"tap_socket": "tap_then_socket.jsonl", "trash_water": "trash_then_water.jsonl"}
    reports = {}
    for name, filename in cases.items():
        if args.case != "all" and args.case != name:
            continue
        directory = args.output_dir.resolve() / name
        replay = directory / "replay.jsonl"
        if not args.summarize_only:
            if replay.exists():
                raise ValueError("Use a fresh output directory or --summarize-only; previous results are preserved")
            subprocess.run([sys.executable, "-u", "-m", "safety_guard", "replay", str(root / "examples" / filename),
                "--local", "--wait", "--timeout", "300", "--config", str(pathlib.Path(args.config).resolve()),
                "--catalog", str(directory / "catalog"), "--output", str(replay)], cwd=root, check=True)
        report = summarize(replay)
        report["input_file"] = str(root / "examples" / filename)
        report["input_sha256"] = hashlib.sha256((root / "examples" / filename).read_bytes()).hexdigest()
        reports[name] = report
        print(f"{name}: {report['following_action']['value']}")
    destination = args.output_dir.resolve() / f"summary_{args.case}.json"
    destination.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(report["matched_expected_final"] for report in reports.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
