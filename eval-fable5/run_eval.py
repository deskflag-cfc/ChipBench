#!/usr/bin/env python3
"""
Run the Fable 5 (xhigh, pass@1) evaluation over the manifest.

- Generation via `claude -p --model claude-fable-5 --effort xhigh`, with the
  benchmark's own system message, tools disabled, single turn.
- Inference concurrency capped at 5 (per the spec).
- Scoring happens immediately after each generation (no cap), inside the worker.
- Resumable: a task with an existing results/<id>.json AND generated raw file is
  skipped. Raw responses are always stored so any subset can be re-scored free.
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_task as st

REPO = "/Users/deskflag/chipbench"
EVAL = os.path.join(REPO, "eval-fable5")
MANIFEST = os.path.join(EVAL, "manifest.jsonl")
RAW_DIR = os.path.join(EVAL, "raw")
RESULTS_DIR = os.path.join(EVAL, "results")
GEN_CONCURRENCY = 5
GEN_TIMEOUT = 900          # seconds per claude -p call (xhigh can be slow)
MAX_GEN_RETRIES = 3

os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)


def load_tasks():
    with open(MANIFEST) as f:
        return [json.loads(l) for l in f if l.strip()]


def already_done(task_id):
    return (os.path.exists(os.path.join(RESULTS_DIR, f"{task_id}.json"))
            and os.path.exists(os.path.join(RAW_DIR, f"{task_id}.txt")))


def generate(task):
    """Call claude -p for one task; return raw completion text or None."""
    raw_path = os.path.join(RAW_DIR, f"{task['task_id']}.txt")
    for attempt in range(1, MAX_GEN_RETRIES + 1):
        try:
            proc = subprocess.run(
                ["claude", "-p", task["user_prompt"],
                 "--system-prompt", task["system_msg"],
                 "--model", "claude-fable-5",
                 "--effort", "xhigh",
                 "--tools", "",
                 "--output-format", "json"],
                capture_output=True, text=True, timeout=GEN_TIMEOUT,
                cwd="/tmp",
            )
            if proc.returncode != 0:
                sys.stderr.write(f"[gen rc={proc.returncode}] {task['task_id']} attempt {attempt}\n{proc.stderr[-400:]}\n")
                time.sleep(5 * attempt)
                continue
            data = json.loads(proc.stdout)
            if data.get("is_error"):
                sys.stderr.write(f"[gen api_error] {task['task_id']} attempt {attempt}: {data.get('api_error_status')}\n")
                time.sleep(5 * attempt)
                continue
            result = data.get("result", "")
            if not result.strip():
                sys.stderr.write(f"[gen empty] {task['task_id']} attempt {attempt}\n")
                time.sleep(5 * attempt)
                continue
            meta = {
                "cost_usd": data.get("total_cost_usd"),
                "num_turns": data.get("num_turns"),
                "output_tokens": data.get("usage", {}).get("output_tokens"),
                "model": list(data.get("modelUsage", {}).keys()),
            }
            with open(raw_path, "w") as f:
                f.write(result)
            with open(os.path.join(RAW_DIR, f"{task['task_id']}.meta.json"), "w") as f:
                json.dump(meta, f)
            return result
        except subprocess.TimeoutExpired:
            sys.stderr.write(f"[gen timeout] {task['task_id']} attempt {attempt}\n")
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[gen jsonerr] {task['task_id']} attempt {attempt}: {e}\n")
        time.sleep(5 * attempt)
    return None


def score(task, raw):
    if task["category"] in ("gen", "debug"):
        code = st.extract_verilog_code(raw)
        passed, log = st.score_gen_or_debug(task, code)
    else:
        code = (st.extract_python_code(raw) if task["lang"] == "python"
                else st.extract_cxxrtl_code(raw))
        passed, log = st.score_refmodel(task, code)
    result = {
        "task_id": task["task_id"],
        "category": task["category"],
        "dataset": task["dataset"],
        "problem": task["problem"],
        "passed": passed,
        "log_tail": log[-2000:],
    }
    with open(os.path.join(RESULTS_DIR, f"{task['task_id']}.json"), "w") as f:
        json.dump(result, f, indent=2)
    return passed


def worker(task):
    tid = task["task_id"]
    t0 = time.time()
    raw = generate(task)
    if raw is None:
        return tid, "GEN_FAILED", time.time() - t0
    try:
        passed = score(task, raw)
    except Exception as e:
        return tid, f"SCORE_ERROR: {e}", time.time() - t0
    return tid, "PASS" if passed else "FAIL", time.time() - t0


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None  # optional category filter
    tasks = load_tasks()
    if only:
        tasks = [t for t in tasks if t["category"] == only]
    todo = [t for t in tasks if not already_done(t["task_id"])]
    print(f"Total in scope: {len(tasks)} | already done: {len(tasks)-len(todo)} | to run: {len(todo)}", flush=True)

    done = 0
    with ThreadPoolExecutor(max_workers=GEN_CONCURRENCY) as ex:
        futs = {ex.submit(worker, t): t["task_id"] for t in todo}
        for fut in as_completed(futs):
            tid, status, dt = fut.result()
            done += 1
            print(f"[{done}/{len(todo)}] {status:12s} {tid}  ({dt:.0f}s)", flush=True)

    print("DONE", flush=True)


if __name__ == "__main__":
    main()
