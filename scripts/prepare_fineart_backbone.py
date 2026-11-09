import argparse
import concurrent.futures
import hashlib
import json
import pathlib
import threading
import urllib.request


REPOSITORY = "lerobot/FineART-VLA-subtask-midtrained"
REVISION = "b6cf4faf2630f971dd390bc32365e9d9c330da8e"
PREFIX = "model.paligemma_with_expert.paligemma."


def fetch_range(url, start, end):
    request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(request, timeout=120) as response:
        if response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {start}-{end}/"):
            raise ValueError("Server did not return the requested checkpoint byte range")
        result = response.read(end - start + 2)
    if len(result) != end - start + 1:
        raise ValueError("Incomplete checkpoint byte range")
    return result


def main():
    parser = argparse.ArgumentParser(description="Download the unchanged FineART language/vision weights, without its action expert")
    parser.add_argument("--output-dir", required=True, type=pathlib.Path)
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/model.safetensors"
    header_size = int.from_bytes(fetch_range(url, 0, 7), "little")
    original = json.loads(fetch_range(url, 8, header_size + 7))
    selected = {name.removeprefix(PREFIX): entry.copy() for name, entry in original.items() if name.startswith(PREFIX)}
    ordered = sorted(selected.values(), key=lambda entry: entry["data_offsets"][0])
    regions = []
    weight_bytes = 0
    for entry in ordered:
        start, end = entry["data_offsets"]
        if regions and regions[-1][1] == start:
            regions[-1][1] = end
        else:
            regions.append([start, end, weight_bytes])
        entry["data_offsets"] = [weight_bytes, weight_bytes + end - start]
        weight_bytes += end - start
    if not regions:
        raise ValueError("No language/vision weights found")
    header = json.dumps(selected, separators=(",", ":")).encode("utf-8")
    header += b" " * (-len(header) % 8)
    checkpoint = directory / "model.safetensors"
    state_path = directory / "download.json"
    state = {"repository": REPOSITORY, "revision": REVISION, "prefix_removed": PREFIX,
             "weight_bytes": weight_bytes, "completed_chunks": {}}
    if state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        if any(previous[key] != state[key] for key in ("repository", "revision", "prefix_removed", "weight_bytes")):
            raise ValueError("Existing download belongs to different weights")
        state = previous
    else:
        if checkpoint.exists():
            raise ValueError("Refusing to overwrite a checkpoint without a resume manifest")
        with checkpoint.open("xb") as stream:
            stream.write(len(header).to_bytes(8, "little") + header)
            stream.truncate(8 + len(header) + weight_bytes)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    chunk_size = 32 * 1024 * 1024
    chunks = [(start + offset, target + offset, min(chunk_size, end - start - offset))
              for start, end, target in regions for offset in range(0, end - start, chunk_size)]
    lock = threading.Lock()

    def download(index):
        if str(index) in state["completed_chunks"]:
            return
        source, target, count = chunks[index]
        error = None
        for attempt in range(3):
            try:
                content = fetch_range(url, header_size + 8 + source, header_size + 7 + source + count)
                break
            except Exception as caught:
                error = caught
        else:
            raise error
        with checkpoint.open("r+b") as stream:
            stream.seek(8 + len(header) + target)
            stream.write(content)
        with lock:
            state["completed_chunks"][str(index)] = hashlib.sha256(content).hexdigest()
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
            temporary.replace(state_path)
            if len(state["completed_chunks"]) % 8 == 0 or len(state["completed_chunks"]) == len(chunks):
                print(f"Downloaded {len(state['completed_chunks'])}/{len(chunks)} chunks", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(download, range(len(chunks))))
    tokenizer_url = "https://storage.googleapis.com/big_vision/paligemma_tokenizer.model"
    tokenizer = directory / "tokenizer.model"
    if not tokenizer.exists():
        with urllib.request.urlopen(tokenizer_url, timeout=60) as response:
            tokenizer.write_bytes(response.read())
    state["tokenizer_url"] = tokenizer_url
    state["tokenizer_sha256"] = hashlib.sha256(tokenizer.read_bytes()).hexdigest()
    state["complete"] = True
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print("FineART language/vision weights downloaded unchanged; no training performed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
