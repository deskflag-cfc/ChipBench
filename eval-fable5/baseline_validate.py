#!/usr/bin/env python3
"""
Baseline validation: for each gen/debug task, feed the golden reference (ref.sv,
module renamed to TopModule) as the DUT and confirm it passes its own testbench.
For each refmodel task, confirm the ground-truth Verilog compiles under Verilator
as RefModule. Problems whose own golden answer can't establish a valid baseline
are not scorable and define the excluded set.

Usage: python3 baseline_validate.py <category>   # gen | debug | refmodel
"""
import json, os, re, subprocess, sys

REPO = "/Users/deskflag/chipbench"
EVAL = os.path.join(REPO, "eval-fable5")
MANIFEST = os.path.join(EVAL, "manifest.jsonl")


def docker_exec(cmd, timeout=90):
    full = ["docker", "exec", "chipbench-runner", "bash", "-c", cmd]
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return -1, (e.stdout or "") + (e.stderr or "") + "\nTIMEOUT"


def baseline_gen_debug(task):
    ref_abs = os.path.join(REPO, task["ref_sv"])
    with open(ref_abs) as f:
        code = f.read()
    code = re.sub(r'module\s+RefModule', "module TopModule", code, count=1)
    gdir = os.path.join(EVAL, "baseline_gen")
    os.makedirs(gdir, exist_ok=True)
    sample = os.path.join(gdir, f"{task['task_id']}.sv")
    with open(sample, "w") as f:
        f.write(code + "\n")
    rel = os.path.relpath(sample, REPO)
    wd = f"/tmp/base/{task['task_id']}"
    cmd = f'''mkdir -p {wd} && cd {wd} && \
iverilog -Wall -Winfloop -Wno-timescale -g2012 -s tb -o sim \
 /workspace/verilogeval/{rel} "/workspace/verilogeval/{task['test_sv']}" "/workspace/verilogeval/{task['ref_sv']}" 2>&1 && \
timeout 30 ./sim 2>&1'''
    rc, out = docker_exec(cmd, timeout=60)
    m = re.search(r'Mismatches:\s*(\d+)\s*in\s*(\d+)\s*samples', out)
    ok = rc == 0 and m and int(m.group(1)) == 0 and int(m.group(2)) > 0
    return ok, out[-400:]


def baseline_refmodel(task):
    gdir = os.path.join(EVAL, "baseline_ref")
    os.makedirs(gdir, exist_ok=True)
    ref = os.path.join(gdir, f"{task['task_id']}_ref.sv")
    with open(ref, "w") as f:
        f.write(task["ref_sv_content"])
    rel = os.path.relpath(ref, REPO)
    wd = f"/tmp/baseref/{task['task_id']}"
    # Verilator lint/compile of the reference as RefModule
    cmd = f'''mkdir -p {wd} && cd {wd} && \
verilator --cc "/workspace/verilogeval/{rel}" --top-module RefModule \
 -Wno-fatal -Wno-WIDTH -Wno-UNUSED -Wno-UNDRIVEN -Wno-UNOPTFLAT -Wno-DECLFILENAME 2>&1 && \
echo VERILATOR_OK'''
    rc, out = docker_exec(cmd, timeout=60)
    ok = rc == 0 and "VERILATOR_OK" in out
    return ok, out[-400:]


def main():
    cat = sys.argv[1]
    with open(MANIFEST) as f:
        tasks = [json.loads(l) for l in f if json.loads(l)["category"] == cat]
    passed, failed = [], []
    for t in tasks:
        if cat == "refmodel":
            ok, log = baseline_refmodel(t)
        else:
            ok, log = baseline_gen_debug(t)
        (passed if ok else failed).append(t["task_id"])
        if not ok:
            print(f"FAIL BASELINE: {t['task_id']}")
            print("   " + log.replace("\n", "\n   ")[-300:])
    print(f"\n[{cat}] baseline PASS={len(passed)} FAIL={len(failed)} total={len(tasks)}")
    out = os.path.join(EVAL, f"baseline_{cat}.json")
    json.dump({"passed": passed, "failed": failed}, open(out, "w"), indent=2)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
