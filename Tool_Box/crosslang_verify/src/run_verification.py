"""
Verification pipeline: generate testbench, compile, link, run.

Supports any combination of DUTs:
- dut_sv:  Verilator DUT (compiled + linked)
- dut_cc:  CXXRTL DUT (#included in testbench.cpp)
- dut_py:  Python DUT (run via subprocess at simulation time)
"""

import os
import re
import subprocess
import shutil
from pathlib import Path

from src.generate_testbench import generate_testbench


def normalize_cxxrtl_source(text):
    """Normalize a CXXRTL DUT source file so it compiles against the real
    Yosys CXXRTL runtime.

    Real yosys-generated CXXRTL implements the abstract cxxrtl::module
    interface (``bool eval(performer*)``, ``void reset()``, ``debug_info``)
    and is left untouched.

    Hand-written "simplified" models (the contract in
    ``Ref Model Gen/gen_cxxrtl_prompt.txt``) define ``void eval()`` /
    ``bool commit()``. Those signatures do NOT match the abstract base's
    pure virtuals, so inheriting from cxxrtl::module makes the struct
    abstract and every ``override`` a compile error. Rewrite such models
    into a standalone struct:
      - strip markdown code fences (``` lines) if present
      - drop ``: public cxxrtl::module`` inheritance from p_TopModule
      - drop ``override`` keywords
      - rewrite bare ``p_<wire>.commit()`` calls (wire::commit requires a
        mandatory observer argument) into an equivalent manual commit
    """
    lines = [l for l in text.splitlines() if not l.lstrip().startswith("```")]
    text = "\n".join(lines).strip() + "\n"

    # Yosys-generated code: leave untouched.
    if re.search(r'\bperformer\b|\bdebug_info\b', text):
        return text

    # Only simplified models declare a no-argument eval().
    if not re.search(r'\bvoid\s+eval\s*\(\s*\)', text):
        return text

    text = re.sub(
        r'((?:struct|class)\s+p_TopModule\b[^:{;()]*?)\s*:\s*'
        r'(?:public\s+|private\s+|protected\s+)?(?:cxxrtl\s*::\s*)?module\b',
        r'\1', text)
    text = re.sub(r'\s+override\b', '', text)
    text = re.sub(
        r'\b(p_\w+)\s*\.\s*commit\s*\(\s*\)',
        r'([&]() { bool _chg = (\1.curr != \1.next); \1.curr = \1.next; '
        r'return _chg; }())',
        text)
    return text

def _detect_cxxrtl_include():
    for candidate in (
        os.environ.get("CXXRTL_INCLUDE"),
        "/usr/local/share/yosys/include/backends/cxxrtl/runtime",
    ):
        if candidate and os.path.isdir(candidate):
            return candidate
    try:
        datdir = subprocess.run(
            ["yosys-config", "--datdir"], capture_output=True, text=True, check=True
        ).stdout.strip()
        return os.path.join(datdir, "include", "backends", "cxxrtl", "runtime")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "/usr/local/share/yosys/include/backends/cxxrtl/runtime"


CXXRTL_INCLUDE = _detect_cxxrtl_include()
VERILATOR_WARNS = [
    "-Wno-fatal", "-Wno-WIDTH", "-Wno-UNUSED",
    "-Wno-UNDRIVEN", "-Wno-UNOPTFLAT", "-Wno-DECLFILENAME",
]


def _run(cmd, label):
    """Run a subprocess, print errors, return (success, result)."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FAILED: {label}")
        if result.stderr:
            print(result.stderr)
        if result.stdout:
            print(result.stdout)
        return False, result
    return True, result


def run_verification(ref_sv, dut_sv=None, dut_cc=None, dut_py=None,
                     json_file=None, work_dir="work"):
    """
    Run the full cross-language verification flow.

    Args:
        ref_sv:    Path to reference Verilog (module RefModule). Required.
        dut_sv:    Path to DUT Verilog (module TopModule). Optional.
        dut_cc:    Path to CXXRTL C++ file. Optional.
        dut_py:    Path to Python DUT file. Optional.
        json_file: Path to JSON port description. Optional (extracts from ref_sv if not given).
        work_dir:  Working directory for build artifacts.

    Returns:
        0 on success (all tests pass), 1 on failure.
    """
    if not dut_sv and not dut_cc and not dut_py:
        print("Error: provide at least one DUT (--dut-sv, --dut-cc, or --dut-py)")
        return 1

    # --- Setup work directory ---
    work_path = Path(work_dir)
    work_path.mkdir(parents=True, exist_ok=True)

    # Copy files to work dir (normalizing the CXXRTL DUT so simplified
    # hand-written models compile against the real CXXRTL runtime)
    for src in filter(None, [ref_sv, dut_sv, dut_py]):
        shutil.copy(src, work_path / os.path.basename(src))
    if dut_cc:
        with open(dut_cc) as f:
            cc_text = f.read()
        with open(work_path / os.path.basename(dut_cc), 'w') as f:
            f.write(normalize_cxxrtl_source(cc_text))

    # --- Generate testbench.cpp ---
    print("Generating testbench.cpp...")
    tb_code = generate_testbench(
        json_file=json_file,
        ref_verilog_file=ref_sv if json_file is None else None,
        dut_verilog_file=dut_sv,
        dut_cxxrtl_file=dut_cc,
        dut_python_file=dut_py,
    )
    tb_path = work_path / "testbench.cpp"
    with open(tb_path, 'w') as f:
        f.write(tb_code)
    print(f"  Generated: {tb_path}")

    # --- Compile and run (in work directory) ---
    orig_dir = os.getcwd()
    os.chdir(work_path)

    try:
        return _build_and_run(
            ref_basename=os.path.basename(ref_sv),
            dut_basename=os.path.basename(dut_sv) if dut_sv else None,
            has_cxxrtl=dut_cc is not None,
        )
    except subprocess.TimeoutExpired:
        print("Simulation timed out!")
        return 1
    finally:
        os.chdir(orig_dir)


def _build_and_run(ref_basename, dut_basename, has_cxxrtl):
    """Compile with Verilator, build, link, and run. Called from work dir."""

    # Build CFLAGS
    cflags_parts = ["-std=c++14", "-I."]
    if has_cxxrtl:
        cflags_parts.append(f"-I{CXXRTL_INCLUDE}")
    cflags = " ".join(cflags_parts)

    # --- Step 1: Compile RefModule with Verilator ---
    print("\n[1] Compiling RefModule with Verilator...")
    ref_cmd = [
        "verilator", "--cc", ref_basename,
        "--top-module", "RefModule",
        "--prefix", "VRefModule",
        "--x-initial", "unique",
        "--x-assign", "unique",
        *VERILATOR_WARNS,
    ]
    # If no DUT Verilog, attach --exe to ref (we still need to compile testbench.cpp)
    if not dut_basename:
        ref_cmd += ["--exe", "testbench.cpp", "-CFLAGS", cflags, "-o", "sim"]
    ok, _ = _run(ref_cmd, "Verilator RefModule")
    if not ok:
        return 1
    print("  RefModule compiled successfully")

    # --- Step 2: Compile DUT TopModule with Verilator (if provided) ---
    if dut_basename:
        print("\n[2] Compiling TopModule with Verilator...")
        dut_cmd = [
            "verilator", "--cc", dut_basename,
            "--top-module", "TopModule",
            "--prefix", "VTopModule",
            "--exe", "testbench.cpp",
            "-CFLAGS", cflags,
            "--x-initial", "unique",
            "--x-assign", "unique",
            *VERILATOR_WARNS,
            "-o", "sim",
        ]
        ok, _ = _run(dut_cmd, "Verilator TopModule")
        if not ok:
            return 1
        print("  TopModule compiled successfully")

    # --- Step 3: Build ---
    print("\n[3] Building...")

    # Build objects from the makefile that has --exe (either DUT or ref)
    if dut_basename:
        mk = "VTopModule.mk"
        libs = ["obj_dir/VTopModule__ALL.a", "obj_dir/VRefModule__ALL.a"]
        # RefModule library built separately
        ok, _ = _run(
            ["make", "-C", "obj_dir", "-f", "VRefModule.mk", "VRefModule__ALL.a"],
            "make VRefModule",
        )
        if not ok:
            return 1
    else:
        mk = "VRefModule.mk"
        libs = ["obj_dir/VRefModule__ALL.a"]

    ok, _ = _run(
        ["make", "-C", "obj_dir", "-f", mk,
         f"V{'TopModule' if dut_basename else 'RefModule'}__ALL.a",
         "testbench.o", "verilated.o"],
        f"make objects",
    )
    if not ok:
        return 1

    print("  Linking...")
    ok, _ = _run(
        ["g++", "-o", "obj_dir/sim", "obj_dir/testbench.o", "obj_dir/verilated.o", *libs],
        "linking",
    )
    if not ok:
        return 1

    print("  Build successful")

    # --- Step 4: Run simulation ---
    print("\n[4] Running simulation...")
    sim_path = "./obj_dir/sim"

    result = subprocess.run([sim_path], capture_output=True, text=True, timeout=300)

    print("\n" + "=" * 50)
    print("SIMULATION OUTPUT")
    print("=" * 50)
    print(result.stdout)
    if result.stderr:
        print(result.stderr)

    return result.returncode
