#!/usr/bin/env python3
"""
Score any raw completion that doesn't yet have a result. Safe to run repeatedly
(idempotent) and concurrently with generation. Scoring concurrency is bounded
only by the container; we use a modest thread pool.
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_task as st

EVAL = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(EVAL, "raw")
RES = os.path.join(EVAL, "results")
SCORE_WORKERS = 6

tasks_by_id = {t["task_id"]: t for t in
               (json.loads(l) for l in open(os.path.join(EVAL, "manifest.jsonl")))}


def score_one(task_id):
    raw_path = os.path.join(RAW, f"{task_id}.txt")
    try:
        raw = open(raw_path).read()
    except FileNotFoundError:
        return task_id, "NO_RAW"
    task = tasks_by_id.get(task_id)
    if task is None:
        return task_id, "NO_TASK"
    try:
        if task["category"] in ("gen", "debug"):
            code = st.extract_verilog_code(raw)
            passed, log = st.score_gen_or_debug(task, code)
        else:
            code = (st.extract_python_code(raw) if task["lang"] == "python"
                    else st.extract_cxxrtl_code(raw))
            passed, log = st.score_refmodel(task, code)
    except Exception as e:
        return task_id, f"ERR:{e}"
    result = {
        "task_id": task_id, "category": task["category"], "dataset": task["dataset"],
        "problem": task["problem"], "lang": task.get("lang"),
        "passed": passed, "log_tail": log[-2000:],
    }
    with open(os.path.join(RES, f"{task_id}.json"), "w") as f:
        json.dump(result, f, indent=2)
    return task_id, ("PASS" if passed else "FAIL")


def pending():
    raws = {f[:-4] for f in os.listdir(RAW) if f.endswith(".txt")}
    done = {f[:-5] for f in os.listdir(RES) if f.endswith(".json")}
    return sorted(raws - done)


def main():
    todo = pending()
    if not todo:
        print("nothing pending")
        return 0
    print(f"scoring {len(todo)} pending", flush=True)
    n_pass = 0
    with ThreadPoolExecutor(max_workers=SCORE_WORKERS) as ex:
        futs = {ex.submit(score_one, tid): tid for tid in todo}
        for fut in as_completed(futs):
            tid, status = fut.result()
            if status == "PASS":
                n_pass += 1
            print(f"  {status:8s} {tid}", flush=True)
    print(f"pass this pass: {n_pass}/{len(todo)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
