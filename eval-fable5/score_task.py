#!/usr/bin/env python3
"""
Given a task_id (from manifest.jsonl) and the raw LLM completion text (read
from stdin or a file), extract code, write the DUT file(s), run the scoring
command inside the `chipbench-runner` container, and record PASS/FAIL to
eval-fable5/results/<task_id>.json

Usage:
    python3 score_task.py <task_id> <response_file>
"""
import json
import os
import re
import shlex
import subprocess
import sys

REPO = "/Users/deskflag/chipbench"
EVAL_DIR = os.path.join(REPO, "eval-fable5")
MANIFEST = os.path.join(EVAL_DIR, "manifest.jsonl")


def load_task(task_id):
    with open(MANIFEST) as f:
        for line in f:
            t = json.loads(line)
            if t["task_id"] == task_id:
                return t
    raise ValueError(f"task not found: {task_id}")


def extract_verilog_code(content):
    """Mirror scripts/sv-generate's non-code-complete extraction: prefer
    [BEGIN]/[DONE] markers, fall back to backtick fences, else raw text."""
    lines = content.splitlines()
    found_code_lines = []
    found_start = False
    found_end = False
    for line in lines:
        if not found_start:
            if line.strip() == "[BEGIN]":
                found_start = True
            elif line.lstrip().startswith("[BEGIN]"):
                found_code_lines.append(line.lstrip().replace("[BEGIN]", ""))
                found_start = True
        elif found_start and not found_end:
            if line.strip() == "[DONE]":
                found_end = True
            elif line.rstrip().endswith("[DONE]"):
                found_code_lines.append(line.rstrip().replace("[DONE]", ""))
                found_end = True
            else:
                found_code_lines.append(line)

    if found_start and found_end:
        code = "\n".join(found_code_lines).strip()
        # Models sometimes obey [BEGIN]/[DONE] while also adding a Markdown
        # fence inside those markers. The marker path takes precedence above,
        # so strip that outer fence explicitly before handing code to Icarus.
        code_lines = code.splitlines()
        if (code_lines and
                re.fullmatch(r'```(?:verilog|systemverilog|sv)?',
                             code_lines[0].strip(), re.IGNORECASE)):
            code_lines = code_lines[1:]
            if code_lines and code_lines[-1].strip() == "```":
                code_lines = code_lines[:-1]
            code = "\n".join(code_lines).strip()
        return code

    # fallback: backtick fences
    m = re.search(r'```(?:verilog|systemverilog|sv)?\s*\n(.*?)```', content, re.DOTALL)
    if m:
        return m.group(1).strip()

    return content.strip()


def extract_python_code(content):
    m = re.search(r'```(?:python)?\s*\n(.*?)```', content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return content.strip()


def extract_cxxrtl_code(content):
    m = re.search(r'```(?:cpp|c\+\+|cxx|cc)?\s*\n(.*?)```', content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return content.strip()


def _extract_defines(test_sv_abs):
    """Return the `define lines from a module's testbench (needed so cpu_ip
    references, which use those macros, compile standalone under Verilator)."""
    if not os.path.exists(test_sv_abs):
        return ""
    defines = []
    with open(test_sv_abs) as f:
        for line in f:
            if re.match(r'\s*`define\b', line):
                defines.append(line.rstrip("\n"))
    return "\n".join(defines)


def docker_exec(cmd, timeout=60):
    # Enforce the timeout inside the container too. Killing only the host-side
    # `docker exec` client can otherwise leave compilers/simulators running and
    # colliding with a retry in the same work directory.
    wrapped = (f"timeout --signal=TERM --kill-after=5 {timeout}s "
               f"bash -lc {shlex.quote(cmd)}")
    full = ["docker", "exec", "chipbench-runner", "bash", "-lc", wrapped]
    try:
        r = subprocess.run(full, capture_output=True, text=True,
                           timeout=timeout + 15)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        def text(value):
            return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")
        return -1, text(e.stdout), text(e.stderr) + "\nTIMEOUT"


def score_gen_or_debug(task, code):
    workdir = f"/tmp/score/{task['task_id']}"
    os.makedirs(os.path.join(EVAL_DIR, "generated"), exist_ok=True)
    sample_path = os.path.join(EVAL_DIR, "generated", f"{task['task_id']}.sv")

    # ensure module name is TopModule (some completions may omit exact name)
    code2 = code
    if "module TopModule" not in code2:
        code2 = re.sub(r'module\s+\w+', "module TopModule", code2, count=1)

    with open(sample_path, "w") as f:
        f.write(code2 + "\n")

    ref_sv = task["ref_sv"]
    test_sv = task["test_sv"]
    rel_sample = os.path.relpath(sample_path, REPO)

    cmd = f'''
mkdir -p {workdir} && cd {workdir} && \
iverilog -Wall -Winfloop -Wno-timescale -g2012 -s tb -o sim \
  /workspace/verilogeval/{rel_sample} \
  "/workspace/verilogeval/{test_sv}" \
  "/workspace/verilogeval/{ref_sv}" 2>&1 && \
timeout 30 ./sim 2>&1
'''
    rc, out, err = docker_exec(cmd, timeout=60)
    combined = out + err

    m = re.search(r'Mismatches:\s*(\d+)\s*in\s*(\d+)\s*samples', combined)
    if rc == 0 and m and int(m.group(1)) == 0 and int(m.group(2)) > 0:
        return True, combined
    return False, combined


def score_refmodel(task, code):
    """Score a generated Python or CXXRTL reference model against the module's
    golden Verilog ref.sv using crosslang_verify. The module's `define macros
    (from its testbench) are prepended so macro-dependent refs (cpu_ip) compile
    standalone under Verilator."""
    lang = task["lang"]
    workdir = f"/tmp/score/{task['task_id']}"
    gen_dir = os.path.join(EVAL_DIR, "generated")
    os.makedirs(gen_dir, exist_ok=True)

    # Build a self-contained ref.sv: defines from testbench + original ref.
    ref_abs = os.path.join(REPO, task["ref_sv"])
    test_abs = os.path.join(REPO, task["test_sv"])
    with open(ref_abs) as f:
        ref_body = f.read()
    defines = _extract_defines(test_abs)
    ref_path = os.path.join(gen_dir, f"{task['task_id']}_ref.sv")
    with open(ref_path, "w") as f:
        if defines:
            f.write(defines + "\n\n")
        f.write(ref_body)
    rel_ref = os.path.relpath(ref_path, REPO)

    # Python module names must be valid identifiers (no hyphens etc.), because
    # crosslang_verify imports the DUT by its filename stem.
    safe_id = re.sub(r'[^0-9A-Za-z_]', '_', task['task_id'])

    if lang == "python":
        dut_path = os.path.join(gen_dir, f"{safe_id}_dut.py")
        with open(dut_path, "w") as f:
            f.write(code)
        dut_flag = f'--dut-py "/workspace/verilogeval/{os.path.relpath(dut_path, REPO)}"'
        pass_re = r"Python:\s*PASS"
    else:  # cxxrtl
        dut_path = os.path.join(gen_dir, f"{task['task_id']}_dut.cc")
        with open(dut_path, "w") as f:
            f.write(code)
        dut_flag = f'--dut-cc "/workspace/verilogeval/{os.path.relpath(dut_path, REPO)}"'
        pass_re = r"CXXRTL:\s*PASS"

    cmd = f'''
mkdir -p {workdir} && \
cd /workspace/verilogeval/Tool_Box/crosslang_verify && \
python3 main.py "/workspace/verilogeval/{rel_ref}" {dut_flag} -w {workdir} 2>&1
'''
    rc, out, err = docker_exec(cmd, timeout=120)
    combined = out + err
    passed = rc == 0 and re.search(pass_re, combined) is not None
    return passed, combined


def main():
    task_id = sys.argv[1]
    response_file = sys.argv[2]
    with open(response_file) as f:
        raw = f.read()

    task = load_task(task_id)

    if task["category"] in ("gen", "debug"):
        code = extract_verilog_code(raw)
        passed, log = score_gen_or_debug(task, code)
    elif task["category"] == "refmodel":
        code = (extract_python_code(raw) if task["lang"] == "python"
                else extract_cxxrtl_code(raw))
        passed, log = score_refmodel(task, code)
    else:
        raise ValueError(task["category"])

    result = {
        "task_id": task_id,
        "category": task["category"],
        "dataset": task["dataset"],
        "problem": task["problem"],
        "passed": passed,
        "log_tail": log[-2000:],
    }
    os.makedirs(os.path.join(EVAL_DIR, "results"), exist_ok=True)
    with open(os.path.join(EVAL_DIR, "results", f"{task_id}.json"), "w") as f:
        json.dump(result, f, indent=2)

    print(f"{'PASS' if passed else 'FAIL'} {task_id}")


if __name__ == "__main__":
    main()
