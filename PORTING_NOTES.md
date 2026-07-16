# Porting Notes

Field notes for anyone bringing the ChipBench harness to a new design set, a new
toolchain install, or a new model. Every entry below is a bug that cost real
score: the model's answer was fine and the harness reported FAIL anyway.

The single most useful habit: **a harness bug and a model bug look identical in
the scoreboard.** Both are `passed: false`. Before believing any number, bucket
the failures by cause (see [Triage](#triage-separating-harness-bugs-from-model-bugs)).

---

## Ground rule: never edit inference outputs

`eval-fable5/raw/` holds the model's completions. Scoring replays them, so a
scoring fix costs zero inference and can be re-run freely. That property only
holds if `raw/` is treated as immutable evidence.

Fix the harness. Never fix the answer. If a task cannot be scored without
editing what the model said, the task is unscoreable — report it as such
(see [Unscoreable by construction](#4-unscoreable-by-construction)).

A corollary worth internalizing: **a harness fix that makes verification weaker
is worse than the bug it replaces.** Silently defaulting an unresolved 32-bit
port to 1 bit "fixes" a crash and quietly stops testing 31 bits, while still
printing a confident PASS. Prefer a loud error over a lenient guess.

---

## Triage: separating harness bugs from model bugs

Bucket every failure before drawing conclusions:

```python
import json, glob, collections
buckets, examples = collections.Counter(), {}
for f in glob.glob("eval-fable5/results/*.json"):
    d = json.load(open(f))
    if d.get("passed"):
        continue
    log = d.get("log_tail", "")          # NB: the key is log_tail, not log
    if   "cxxrtl_drive_clk" in log and "not declared" in log: k = "clk detection disagreement"
    elif "was not declared" in log:      k = "identifier not declared"
    elif "has no member named" in log:   k = "port name mismatch"
    elif "BLKANDNBLK" in log:            k = "Verilator rejects golden ref"
    elif "Unknown module type" in log:   k = "missing submodule (unscoreable)"
    elif "MISMATCH" in log:              k = "REAL model failure"
    else:                                k = "unclassified"
    buckets[k] += 1
    examples.setdefault(k, d["task_id"])
for k, v in buckets.most_common():
    print(f"{v:4d}  {k:32s} e.g. {examples[k]}")
```

Two traps this script exists to avoid, both of which produced confidently wrong
answers during the original debugging:

- **Guessing the JSON key.** An earlier version read `d["log"]`, which does not
  exist. Every log came back `""`, everything bucketed as "unclassified", and
  the output looked plausible. Confirm the schema, don't assume it.
- **Branch ordering.** A real mismatch log contains *both* `Generating
  testbench.cpp...` and `FAIL (22 errors)`. An earlier version tested for
  "testbench.cpp" before the mismatch check and swallowed every genuine model
  failure, reporting "100% pass among scoreable tasks". Order branches most
  specific first, and treat an absurdly clean result as a bug in the analysis,
  not a triumph.

Sanity check that passes are real, not vacuous:

```python
# every passing refmodel task should have run hundreds of comparisons
n = max((int(x) for x in re.findall(r"Total:\s*(\d+)", log)), default=0)
```

Match the string the harness actually prints (`Total: 500`, above `Running 500
clock cycles`). Writing this check with `r"(\d+) tests"` — a plausible guess that
appears nowhere in the logs — reports every task as 0 checks and manufactures 24
"hollow passes" that do not exist. That mistake was made while writing *this
document*, which is the point: the wrong pattern fails silently and looks like a
finding.

Verified at time of writing: all 36 passing refmodel tasks ran 500 or 1000
checks (`{500: 24, 1000: 12}`), none under 500, none containing MISMATCH. A
"pass" with a handful of checks means the testbench compiled but compared almost
nothing.

---

## 1. Port extraction (`tools/extract_ports.py`)

The highest-yield bug class. Port extraction decides *which signals get compared
and at what width* — so when it is wrong, the testbench either fails to compile
(scored FAIL) or compares the wrong bits (bogus mismatch).

It does **not** decide the verdict. That comes from the generated C++
(`if (errors == 0) PASS`) via `score_task.py`. A port-extraction fix therefore
cannot promote a wrong model to passing.

### Symptom: `redeclaration of 'uint32_t logic'`

The original regex was

```python
r'(input|output)\s+(?:reg|wire|logic)?\s*(?:\[(\d+):(\d+)\])?\s*(\w+)'
```

Width accepted **literal digits only**. On `input logic [DATA_WIDTH-1:0] SrcA`
the width group cannot match, so the optional qualifier group backtracks to
empty and `(\w+)` happily captures the type keyword. The port comes back named
`logic`. Two such ports emit `uint32_t logic` twice and nothing compiles.

This is the classic optional-group backtracking hazard: when a later group
fails, an earlier optional group gives back text to let the match succeed
*somewhere*, producing a match that is syntactically valid and semantically
garbage. **A regex that can match the wrong thing rather than fail is worse than
one that fails.** Hence the explicit guard: a port name landing on a type
keyword now raises.

### Fix pattern

- Widths are arbitrary expressions, resolved against `` `define `` macros and
  `parameter`/`localparam` collected from the *whole file*. Parameters live in
  the `#(...)` block that the port-list scan deliberately skips, so collect
  constants before narrowing to the module body.
- Qualifiers repeat (`input signed [7:0] y`) — use a repeated group, not `?`.
- Unresolvable width → **raise**, never default to 1.

### Symptom: submodule ports leak into the top-level list

The old code scanned every line in the file, so a `ref.sv` declaring helper
modules before `RefModule` contributed their ports. Walk to the named module and
read only its own parens; skip the optional `#(...)` block first.

---

## 2. CXXRTL model contract (`Ref Model Gen/gen_cxxrtl_prompt.txt`)

**When every model fails the same way, suspect the prompt, not the model.**

The prompt instructed:

```cpp
struct p_TopModule : public cxxrtl::module {
    void eval() override;
    bool commit() override;
};
```

The real runtime's abstract interface is `bool eval(performer*)` / `void
reset()`. Those signatures never match, so the struct stays abstract and every
`override` is a hard error. Models that followed instructions perfectly produced
code that could not build.

Two-sided fix, and both sides are needed:

- **Prompt**: specify a standalone struct, plain `eval()`/`commit()`, no
  inheritance, no `override`.
- **Harness** (`normalize_cxxrtl_source`): repair sources anyway — strip
  markdown fences (models emit them regardless of instructions), drop
  inheritance and `override`, rewrite bare `p_<wire>.commit()` (the real
  `wire::commit` requires an observer argument).

Detect yosys-generated CXXRTL via `performer`/`debug_info` and pass it through
untouched. Normalization must only touch hand-written models.

> Porting lesson: a prompt is part of the harness. It has bugs, and they show up
> as uniform model failure across every task.

---

## 3. Clock protocol

`p_clk` is `wire<1>` in simplified models and `value<1>` in yosys output, and
`.set()` means different things on each. `cxxrtl_drive_clk` overloads on both:
for `wire<1>` it sets **curr and next**, so a level check inside `eval()`
observes the value driven for this half-cycle.

The prompt must state the protocol exactly — one `eval()` + `commit()` per clock
level, treat a high level inside `eval()` as the rising edge — rather than
leaving edge semantics to be inferred. It also must say to compute all
next-state values from current state before assigning any (registers update
simultaneously in hardware).

Combinational DUTs have no `clk` member, so `sequential_init` must not be
emitted for them.

---

## 4. Toolchain paths

`CXXRTL_INCLUDE` was hardcoded to the Docker image's yosys layout. Any
package-manager or Homebrew yosys puts the runtime elsewhere and every CXXRTL
task fails at `#include` with no hint the cause is a path.

Resolve in order: `$CXXRTL_INCLUDE` → known path → `yosys-config --datdir`, and
fall back to the old constant rather than raising, so behaviour on the known-good
image is unchanged.

---

## 5. Rate limiting (`eval-fable5/usage_check.py`, `run_local.py`)

**A guard that consumes the resource it protects will exhaust it.** The usage
poll ran before every task launch: ~30 requests/hour of budget against several
hundred wanted. It 429'd itself and halted the run at 5h=34%, against its own
80% threshold.

Note the shape of this bug, because it generalizes: the poll rate is a function
of *task count*, not concurrency. Turning concurrency down cannot fix it — even
`CONCURRENCY=1` exceeds the budget. Reaching for the obvious knob would have
produced a slower run that failed identically.

- Cache polls (180s TTL). A stale reading risks overshooting a threshold; a 429
  halts the run outright. Never cache ERROR readings — they must stay
  retryable. Real exhaustion still fails closed via the session-limit rejection
  path.
- Honor `Retry-After`. Guessing shorter spends another request on an endpoint
  already refusing them.
- Don't fall through token candidates on 429. Candidates exist to survive an
  *expired* credential; a second token cannot lift a rate limit, it only doubles
  load. Fall through on auth/transport errors only.
- Log cache hits distinctly. The request *rate* is the diagnostic signal, so
  logging cached and real polls identically destroys the log's only purpose.

Also: the endpoint 429s a copied token while the live Keychain credential
succeeds. A stale `.oauth_token` beside the script silently doubles cost. The
window drains on its own — waiting is a legitimate fix, and `retry-after`
counting down in step with elapsed time is how you confirm it.

---

## 6. Known-open issues

Each is a real harness defect suppressing real score. Root causes are
identified; none are fixed.

### `is_clk_signal` inspects only the first port — 3 tasks

`tools/clk.py`:

```python
for input in inputs:
    if not re.search(r'clk', input[0], re.IGNORECASE):
        return False
    return True          # <-- returns on the FIRST iteration, always
```

The loop body returns unconditionally, so only `inputs[0]` is ever examined.
`Prob034`'s ports are `(rst_n, clk, money, set, boost)` — `rst_n` is first, so
`is_sequential` is False for a clearly sequential design.

Meanwhile `signal_gen.py` detects the clock independently (`name.lower() ==
'clk'`) and emits `ref->clk = clk` plus the drive call regardless. The two
disagree, and the generated testbench references a `clk` local and a helper that
the `is_sequential` gate never declared:

```
error: 'cxxrtl_drive_clk' was not declared in this scope
error: 'clk' was not declared in this scope
```

**Two independent detections of the same property will drift.** The fix is to
scan all inputs and derive both call sites from one source of truth. Deferred
only because it requires a re-score.

### `'class VRefModule' has no member named 'set'` — 5 tasks

`set` is a genuine `RefModule` port, so this is *not* the port-name bug above;
Verilator is not exposing the member under that name. Unclear whether Verilator
mangles it or the ref reaching Verilator differs. Not yet root-caused.

### Verilator rejects the golden ref (`BLKANDNBLK`) — 2 tasks

Verilator refuses legal Verilog that iverilog accepts (blocking and non-blocking
assignment to one variable). The `refmodel` path uses Verilator; `gen` uses
iverilog — so the same golden file passes in one category and fails in another.
Either relax the Verilator flags or fix the golden refs; both are methodology
decisions.

### Unscoreable by construction — 6 tasks

`dataset_not_self_contain/Prob005_8_bit_alu_ref.sv` instantiates `bit_8_AND`,
`bit_8_OR`, `bit_8_ADDER`, `mux_2X1`; `Prob002` instantiates `decoder_38`. None
exist anywhere in the repo, and `score_refmodel` builds the ref from defines +
ref body only — it never supplies submodules. These cannot pass regardless of
model output. Authoring the submodules or dropping the tasks is a judgment call;
fabricating them to lift the score would be scoring the harness, not the model.

### Orphan result files

`results/refmodel__python__73228.json` (`passed: true`) and
`gen__dataset_self_contain__Prob000_...` are not in `manifest.jsonl`. They do not
affect manifest-scoped totals but inflate any `glob`-based count — the origin of
a spurious "37 vs 36" discrepancy. Score against the manifest, never against a
directory glob.

---

## Scoreboard at time of writing

Manifest-scoped, 310/310 scored:

| Category | Score | |
|---|---|---|
| gen | 20/44 | 45% |
| debug | 137/178 | 77% |
| refmodel | 36/88 | 41% (was 26/88 before the port fix) |
| **Overall** | **193/310** | **62.3%** |

Failure buckets across all 118 failing result files: 76 unclassified, 29 real
model mismatches, 5 port-name mismatches, 3 clk-detection disagreements, 3
identifier-not-declared, 2 Verilator `BLKANDNBLK`.

The port fix moved 10 tasks FAIL→PASS with **zero** PASS→FAIL, replayed from
`raw/` at no inference cost. `gen` and `debug` do not use `extract_ports` (they
go through iverilog directly) and were unaffected.

---

## Verification discipline

Four verifications during this work were themselves wrong, each caught only by
re-checking. They share a shape worth naming: **a command that silently measures
nothing returns success.**

- `git diff --stat -- Tool_Box/...` run from a subdirectory resolved to a
  nonexistent path, returned empty, and made a "0 violations" grep vacuous. An
  empty result is only evidence if you have confirmed the query can produce a
  non-empty one.
- `grep -v test` intended to exclude test files also excluded
  `generate_testbench.py`, because "test" is a substring of "testbench" — so the
  "who calls extract_ports?" check found nothing and appeared conclusive.
- A hand-transcribed "recovered tasks" list included a task that had not
  actually passed.
- A `re.findall(r"(\d+) tests", log)` hollow-pass check matched a string the
  harness never prints, scored every task 0, and produced 24 fabricated "hollow
  passes" — a scary-looking finding that was purely an artifact of the query.

Before trusting a check, ask what its output would look like if it were broken.
If "broken" and "clean" look the same, the check is not evidence.
