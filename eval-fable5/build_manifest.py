#!/usr/bin/env python3
"""
Build the ChipBench paper task manifest for the Fable 5 (xhigh, pass@1) run.

Composition (matches the paper, 310 tasks total):
  - Verilog Generation: 44  (29 self-contained [30 minus the invalid mux] +
                             6 non-self-contained + 9 CPU IP)
  - Verilog Debugging: 178  (89 cases x {zero-shot, one-shot})
  - Reference Model Gen: 88 (the SAME 44 modules x {Python, CXXRTL};
                             SystemC excluded — placeholders, not verifiable)

Prompts are constructed the same way scripts/sv-generate builds spec-to-rtl
prompts (generation/debugging) and the same way Ref Model Gen uses
gen_{python,cxxrtl}_prompt.txt as the system message with the module spec as
the user message.
"""
import json, os, re

REPO = "/Users/deskflag/chipbench"

# The single invalid self-contained module (1-bit verification signals for a
# 2-bit mux_out port -> malformed testbench). Excluded from the paper's 44.
EXCLUDE_GEN = {"Prob000_Four-to-one_multiplexer"}

SYSTEM_MSG_SPEC_TO_RTL = "You are a Verilog RTL designer that only writes code using correct Verilog syntax.\n"
PROMPT_NO_EXPLAIN_SUFFIX = """
Enclose your code with [BEGIN] and [DONE]. Only output the code snippet
and do NOT output anything else.
"""


def build_spec_to_rtl_prompt(prompt_text, examples_prefix=None):
    full = ""
    if examples_prefix:
        full += examples_prefix
    full += "\nQuestion:\n" + prompt_text.strip() + "\n"
    full = full.rstrip() + "\n" + PROMPT_NO_EXPLAIN_SUFFIX
    full += "\nAnswer:\n"
    return full


def gen_module_list():
    """Return list of (dataset, problem) for the 44 generation modules."""
    base = os.path.join(REPO, "Verilog Gen")
    mods = []
    for dataset in ["dataset_self_contain", "dataset_not_self_contain", "dataset_cpu_ip"]:
        ddir = os.path.join(base, dataset)
        with open(os.path.join(ddir, "problems.txt")) as f:
            for line in f:
                prob = line.strip()
                if not prob or prob in EXCLUDE_GEN:
                    continue
                mods.append((dataset, prob))
    return mods


def gen_tasks(mods):
    base = os.path.join(REPO, "Verilog Gen")
    tasks = []
    for dataset, prob in mods:
        ddir = os.path.join(base, dataset)
        with open(os.path.join(ddir, f"{prob}_prompt.txt")) as f:
            prompt_text = f.read()
        tasks.append({
            "task_id": f"gen__{dataset}__{prob}",
            "category": "gen",
            "dataset": dataset,
            "problem": prob,
            "system_msg": SYSTEM_MSG_SPEC_TO_RTL,
            "user_prompt": build_spec_to_rtl_prompt(prompt_text),
            "ref_sv": os.path.relpath(os.path.join(ddir, f"{prob}_ref.sv"), REPO),
            "test_sv": os.path.relpath(os.path.join(ddir, f"{prob}_test.sv"), REPO),
        })
    return tasks


def debug_tasks():
    base = os.path.join(REPO, "Verilog Debugging")
    with open(os.path.join(REPO, "scripts", "verilog-example-prefix_spec-to-rtl_1-shot.txt")) as f:
        one_shot_prefix = f.read()
    tasks = []
    for dataset in sorted(os.listdir(base)):
        ddir = os.path.join(base, dataset)
        if not (os.path.isdir(ddir) and dataset.startswith("dataset_debug")):
            continue
        pf = os.path.join(ddir, "problems.txt")
        if not os.path.exists(pf):
            continue
        is_one_shot = "one_shot" in dataset
        with open(pf) as f:
            problems = [l.strip() for l in f if l.strip()]
        for prob in problems:
            with open(os.path.join(ddir, f"{prob}_prompt.txt")) as f:
                prompt_text = f.read()
            prefix = one_shot_prefix if is_one_shot else None
            tasks.append({
                "task_id": f"debug__{dataset}__{prob}",
                "category": "debug",
                "dataset": dataset,
                "problem": prob,
                "system_msg": SYSTEM_MSG_SPEC_TO_RTL,
                "user_prompt": build_spec_to_rtl_prompt(prompt_text, examples_prefix=prefix),
                "ref_sv": os.path.relpath(os.path.join(ddir, f"{prob}_ref.sv"), REPO),
                "test_sv": os.path.relpath(os.path.join(ddir, f"{prob}_test.sv"), REPO),
            })
    return tasks


def refmodel_tasks(mods):
    base = os.path.join(REPO, "Verilog Gen")
    with open(os.path.join(REPO, "Ref Model Gen", "gen_python_prompt.txt")) as f:
        py_sys = f.read()
    with open(os.path.join(REPO, "Ref Model Gen", "gen_cxxrtl_prompt.txt")) as f:
        cc_sys = f.read()

    langs = [
        ("python", py_sys, [("Verilog", "Python"), ("verilog", "python"),
                            ("SystemVerilog", "Python"), ("systemverilog", "python")]),
        ("cxxrtl", cc_sys, [("Verilog", "CXXRTL C++"), ("verilog", "CXXRTL C++"),
                            ("SystemVerilog", "CXXRTL C++"), ("systemverilog", "CXXRTL C++")]),
    ]

    tasks = []
    for dataset, prob in mods:
        ddir = os.path.join(base, dataset)
        with open(os.path.join(ddir, f"{prob}_prompt.txt")) as f:
            spec = f.read()
        for lang, sysmsg, repls in langs:
            user = spec
            for a, b in repls:
                user = user.replace(a, b)
            tasks.append({
                "task_id": f"refmodel__{lang}__{dataset}__{prob}",
                "category": "refmodel",
                "lang": lang,
                "dataset": f"ref_{dataset}",
                "problem": prob,
                "system_msg": sysmsg,
                "user_prompt": user,
                "ref_sv": os.path.relpath(os.path.join(ddir, f"{prob}_ref.sv"), REPO),
                "test_sv": os.path.relpath(os.path.join(ddir, f"{prob}_test.sv"), REPO),
            })
    return tasks


def main():
    mods = gen_module_list()
    g = gen_tasks(mods)
    d = debug_tasks()
    r = refmodel_tasks(mods)
    tasks = g + d + r
    print(f"gen={len(g)} debug={len(d)} refmodel={len(r)} (modules={len(mods)}) total={len(tasks)}")
    out = os.path.join(REPO, "eval-fable5", "manifest.jsonl")
    with open(out, "w") as f:
        for t in tasks:
            f.write(json.dumps(t) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
