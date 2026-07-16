#!/usr/bin/env python3
"""
Local ChipBench Fable-5 (xhigh, pass@1) runner.

- Concurrency 5 with bounded scheduling. The main scheduler consults
  five-hour/seven-day usage before every new `claude -p` inference, served from
  a short-lived cache (see usage_snapshot). It never queues more than five tasks
  at once.
- Rate-limit guard: at 80% five-hour usage (or 90% seven-day usage), launching
  pauses. In-flight tasks finish, then the process sleeps until the relevant
  reset and automatically resumes. Usage API errors fail closed and are retried.
- Fully resumable: a task with both raw/<id>.txt and results/<id>.json is skipped.

Usage: python3 run_local.py            # run all pending (gen+debug+refmodel-python)
"""
import fcntl, json, os, shutil, signal, subprocess, sys, threading, time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_task as st

EVAL = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(EVAL, "manifest.jsonl")
RAW = os.path.join(EVAL, "raw")
RES = os.path.join(EVAL, "results")
os.makedirs(RAW, exist_ok=True); os.makedirs(RES, exist_ok=True)

CONCURRENCY = int(os.environ.get("CONCURRENCY", "5"))
GEN_TIMEOUT = int(os.environ.get("GEN_TIMEOUT", "1800"))
MAX_TASK_RETRIES = int(os.environ.get("MAX_TASK_RETRIES", "2"))
USAGE_ERROR_RETRY_SEC = int(os.environ.get("USAGE_ERROR_RETRY_SEC", "60"))
USAGE_MAX_RETRY_SEC = int(os.environ.get("USAGE_MAX_RETRY_SEC", "900"))
USAGE_MIN_INTERVAL_SEC = int(os.environ.get("USAGE_MIN_INTERVAL_SEC", "15"))
USAGE_CACHE_TTL_SEC = int(os.environ.get("USAGE_CACHE_TTL_SEC", "180"))
USAGE_CHECK_TIMEOUT = int(os.environ.get("USAGE_CHECK_TIMEOUT", "300"))
RESET_GRACE_SEC = int(os.environ.get("RESET_GRACE_SEC", "15"))
FIVE_HOUR_THRESHOLD = float(os.environ.get("FIVE_HOUR_THRESHOLD", "80"))
SEVEN_DAY_THRESHOLD = float(os.environ.get("SEVEN_DAY_THRESHOLD", "90"))

stop_event = threading.Event()
shutdown_event = threading.Event()
stop_reason = {"why": None}
_last_status = {"v": "unknown"}
_usage_check_lock = threading.Lock()
_last_usage_check_at = {"v": 0.0}
_usage_cache = {"snap": None, "at": 0.0}

SESSION_LIMIT_MARKERS = ("session limit", "usage limit", "five-hour limit",
                         "five hour limit", "rejected")

USAGE_CHECK = os.path.join(EVAL, "usage_check.py")
LOCK_FILE = os.path.join(EVAL, ".run_local.lock")
_runner_lock = {"file": None}


def parse_usage(stdout):
    """Return decision, utilization, reset epochs, and any 429 retry window."""
    fh = sd = None
    fh_reset = sd_reset = retry_after = None
    decision = None
    for line in stdout.splitlines():
        if line.startswith("FIVE_HOUR_UTIL="):
            try: fh = float(line.split("=", 1)[1])
            except ValueError: pass
        elif line.startswith("SEVEN_DAY_UTIL="):
            try: sd = float(line.split("=", 1)[1])
            except ValueError: pass
        elif line.startswith("FIVE_HOUR_RESETS_AT="):
            try: fh_reset = float(line.split("=", 1)[1])
            except ValueError: pass
        elif line.startswith("SEVEN_DAY_RESETS_AT="):
            try: sd_reset = float(line.split("=", 1)[1])
            except ValueError: pass
        elif line.startswith("RETRY_AFTER="):
            try: retry_after = float(line.split("=", 1)[1])
            except ValueError: pass
        elif line.startswith("DECISION="):
            decision = line.split("=", 1)[1].strip()
    if decision not in ("OK", "STOP"):
        decision = "ERROR"
    return decision, fh, sd, fh_reset, sd_reset, retry_after


def usage_snapshot():
    """Return (snapshot, from_cache).

    snapshot is (decision, five_hour, seven_day, fh_reset, sd_reset, retry_after).

    The OAuth usage endpoint budgets on the order of 30 requests/hour. Querying
    it once per launch demands several hundred, so the guard rate-limits itself
    and then cannot verify usage at all. Serve launches from a short-lived cache
    instead: a stale reading only risks overshooting a threshold, whereas a 429
    halts the run outright. Real exhaustion still fails closed, because
    generate() detects a session-limit rejection and forces a reset wait.
    """
    with _usage_check_lock:
        cached = _usage_cache["snap"]
        if (cached is not None and
                time.monotonic() - _usage_cache["at"] < USAGE_CACHE_TTL_SEC):
            return cached, True
        delay = USAGE_MIN_INTERVAL_SEC - (
            time.monotonic() - _last_usage_check_at["v"]
        )
        if delay > 0 and shutdown_event.wait(delay):
            return ("ERROR", None, None, None, None, None), False
        try:
            p = subprocess.run([sys.executable, USAGE_CHECK],
                               capture_output=True, text=True,
                               timeout=USAGE_CHECK_TIMEOUT)
        except Exception:
            _last_usage_check_at["v"] = time.monotonic()
            return ("ERROR", None, None, None, None, None), False
        _last_usage_check_at["v"] = time.monotonic()
        snap = parse_usage(p.stdout)
        # Cache verified readings only; an ERROR must stay immediately retryable.
        if snap[0] != "ERROR":
            _usage_cache["snap"] = snap
            _usage_cache["at"] = time.monotonic()
        return snap, False


def tasks_by_id():
    tasks = {}
    with open(MANIFEST) as f:
        for lineno, line in enumerate(f, 1):
            try:
                task = json.loads(line)
                tid = task["task_id"]
            except (ValueError, KeyError, TypeError) as e:
                raise ValueError(f"invalid manifest line {lineno}: {e}") from e
            if tid in tasks:
                raise ValueError(f"duplicate task_id in manifest: {tid}")
            tasks[tid] = task
    return tasks


def validate_environment(tasks):
    if not tasks:
        raise RuntimeError("manifest contains no tasks")
    if CONCURRENCY < 1:
        raise RuntimeError("CONCURRENCY must be at least 1")
    if GEN_TIMEOUT < 1 or USAGE_CHECK_TIMEOUT < 1:
        raise RuntimeError("timeouts must be positive")
    if not 0 < FIVE_HOUR_THRESHOLD <= 100:
        raise RuntimeError("FIVE_HOUR_THRESHOLD must be in (0, 100]")
    if not 0 < SEVEN_DAY_THRESHOLD <= 100:
        raise RuntimeError("SEVEN_DAY_THRESHOLD must be in (0, 100]")
    for command in ("claude", "docker"):
        if shutil.which(command) is None:
            raise RuntimeError(f"required command not found: {command}")
    if not os.path.isfile(USAGE_CHECK):
        raise RuntimeError(f"usage checker not found: {USAGE_CHECK}")
    required = {"task_id", "category", "dataset", "problem", "system_msg",
                "user_prompt"}
    for tid, task in tasks.items():
        missing = required - task.keys()
        if missing:
            raise RuntimeError(f"task {tid} missing fields: {sorted(missing)}")
    try:
        p = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}",
             "chipbench-runner"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        raise RuntimeError(f"cannot inspect chipbench-runner: {e}") from e
    if p.returncode != 0 or p.stdout.strip().lower() != "true":
        detail = p.stderr.strip() or p.stdout.strip() or "not running"
        raise RuntimeError(f"chipbench-runner container unavailable: {detail}")


def valid_raw(tid):
    path = os.path.join(RAW, f"{tid}.txt")
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def already_done(tid):
    if not valid_raw(tid):
        return False
    try:
        with open(os.path.join(RES, f"{tid}.json")) as f:
            result = json.load(f)
        return result.get("task_id") == tid and isinstance(result.get("passed"), bool)
    except (OSError, ValueError, TypeError):
        return False


def atomic_write_text(path, content):
    """Publish an artifact only after its complete contents are on disk."""
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path, data, **kwargs):
    atomic_write_text(path, json.dumps(data, **kwargs))


def sleep_interruptibly(seconds):
    """Return False when an administrative shutdown interrupts a wait."""
    deadline = time.monotonic() + max(0, seconds)
    while not shutdown_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        shutdown_event.wait(min(60, remaining))
    return False


def sleep_until(epoch):
    return sleep_interruptibly(max(0, epoch - time.time()))


def wait_for_usage(all_ids, force_five_hour_reset=False):
    """Fail-closed gate used immediately before each task submission."""
    error_retry_sec = USAGE_ERROR_RETRY_SEC
    while True:
        if shutdown_event.is_set():
            return False
        snap, from_cache = usage_snapshot()
        decision, fh, sd, fh_reset, sd_reset, retry_after = snap
        _last_status["v"] = f"5h={fh}% 7d={sd}% -> {decision}"
        # Record cached vs fresh: the request *rate* in this log is the signal
        # for diagnosing endpoint rate-limiting, and cache hits cost no request.
        with open(os.path.join(EVAL, "usage_watch.log"), "a") as f:
            f.write(f"{int(time.time())} 5h={fh} 7d={sd} {decision} "
                    f"{'cached' if from_cache else 'fresh'}\n")

        if decision == "OK" and not force_five_hour_reset:
            stop_reason["why"] = None
            write_resume_state(all_ids)
            return True

        now = time.time()
        reset_candidates = []
        if force_five_hour_reset or (
            fh is not None and fh >= FIVE_HOUR_THRESHOLD
        ):
            if fh_reset is not None and fh_reset > now:
                reset_candidates.append(fh_reset)
        if sd is not None and sd >= SEVEN_DAY_THRESHOLD:
            if sd_reset is not None and sd_reset > now:
                reset_candidates.append(sd_reset)

        # In forced-reset mode, an explicit OK still means "wait for this
        # window's reset". An ERROR is not confirmation of a limit and follows
        # the exponential fail-closed path below.
        confirmed_wait = decision == "STOP" or (
            force_five_hour_reset and decision == "OK"
        )
        if confirmed_wait:
            stop_reason["why"] = (
                f"usage pause (5h={fh}% 7d={sd}%); waiting for reset"
            )
            write_resume_state(all_ids)
            if reset_candidates:
                wake_at = max(reset_candidates) + RESET_GRACE_SEC
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wake_at))
                print(f"PAUSED: 5h={fh}% 7d={sd}%; auto-resume check at {stamp}",
                      flush=True)
                if not sleep_until(wake_at):
                    return False
                force_five_hour_reset = False
            else:
                print("PAUSED: limit reached but reset time unavailable; "
                      f"retrying usage in {error_retry_sec}s", flush=True)
                if not sleep_interruptibly(error_retry_sec):
                    return False
                error_retry_sec = min(USAGE_MAX_RETRY_SEC,
                                      error_retry_sec * 2)
            continue

        # Never launch inference when usage cannot be verified.
        stop_reason["why"] = "usage API unavailable; waiting to retry"
        write_resume_state(all_ids)
        # A 429 states when the window frees up; guessing shorter just spends
        # another request on an endpoint that is already refusing them.
        if retry_after and retry_after > 0:
            delay = max(retry_after, USAGE_ERROR_RETRY_SEC)
            print(f"PAUSED: usage API rate-limited; honoring Retry-After "
                  f"{int(delay)}s", flush=True)
        else:
            delay = error_retry_sec
            error_retry_sec = min(USAGE_MAX_RETRY_SEC, error_retry_sec * 2)
            print("PAUSED: usage API unavailable; "
                  f"retrying in {delay}s", flush=True)
        if not sleep_interruptibly(delay):
            return False


def generate(task):
    """One claude -p inference; returns raw text or None. Sets stop_event on limit."""
    raw_path = os.path.join(RAW, f"{task['task_id']}.txt")
    sys_msg = task["system_msg"]
    user = task["user_prompt"]
    try:
        proc = subprocess.run(
            ["claude", "-p", user, "--system-prompt", sys_msg,
             "--model", "claude-fable-5", "--effort", "xhigh", "--tools", "",
             "--output-format", "json"],
            capture_output=True, text=True, timeout=GEN_TIMEOUT, cwd="/tmp")
        out = proc.stdout
        try:
            data = json.loads(out)
        except ValueError:
            blob = (out + proc.stderr).lower()
            if any(m in blob for m in SESSION_LIMIT_MARKERS):
                stop_reason["why"] = "session limit hit during inference"
                stop_event.set()
            return None
        if data.get("is_error"):
            blob = json.dumps(data).lower()
            if any(m in blob for m in SESSION_LIMIT_MARKERS):
                stop_reason["why"] = "session limit hit during inference"
                stop_event.set()
            return None
        result = data.get("result", "")
        if not result.strip():
            return None
        atomic_write_text(raw_path, result)
        atomic_write_json(
            os.path.join(RAW, f"{task['task_id']}.meta.json"),
            {"cost_usd": data.get("total_cost_usd"),
             "output_tokens": data.get("usage", {}).get("output_tokens")},
        )
        return result
    except subprocess.TimeoutExpired:
        return None


def score(task, raw):
    if task["category"] in ("gen", "debug"):
        code = st.extract_verilog_code(raw)
        passed, log = st.score_gen_or_debug(task, code)
    else:
        code = (st.extract_python_code(raw) if task["lang"] == "python"
                else st.extract_cxxrtl_code(raw))
        passed, log = st.score_refmodel(task, code)
    atomic_write_json(
        os.path.join(RES, f"{task['task_id']}.json"),
        {"task_id": task["task_id"], "category": task["category"],
         "dataset": task["dataset"], "problem": task["problem"],
         "lang": task.get("lang"), "passed": passed,
         "log_tail": log[-2000:]},
        indent=2,
    )
    return passed


def worker(task, counters, lock):
    tid = task["task_id"]
    if stop_event.is_set():
        return tid, "SKIPPED_STOP"
    raw_path = os.path.join(RAW, f"{tid}.txt")
    result_path = os.path.join(RES, f"{tid}.json")
    # A completed inference whose scoring was interrupted is valid resume work;
    # score it directly instead of paying for the inference again.
    if valid_raw(tid) and not already_done(tid):
        with open(raw_path) as f:
            raw = f.read()
    else:
        raw = generate(task)
    if raw is None:
        return tid, "GEN_FAILED"
    try:
        passed = score(task, raw)
    except Exception as e:
        return tid, f"SCORE_ERR:{e}"
    with lock:
        counters["done"] += 1
        if passed:
            counters["pass"] += 1
    return tid, "PASS" if passed else "FAIL"


def write_resume_state(all_ids):
    done = [t for t in all_ids if already_done(t)]
    pending = [t for t in all_ids if not already_done(t)]
    state = {
        "stopped_reason": stop_reason["why"],
        "five_hour_status": _last_status["v"],
        "total": len(all_ids),
        "done": len(done),
        "pending": len(pending),
        "pending_ids": pending,
    }
    atomic_write_json(os.path.join(EVAL, "resume_state.json"), state, indent=2)
    return state


def request_shutdown(signum, _frame):
    if not shutdown_event.is_set():
        stop_reason["why"] = f"shutdown requested by signal {signum}"
        shutdown_event.set()
        print("\nSHUTDOWN: no new tasks will launch; draining in-flight tasks",
              flush=True)


def acquire_runner_lock():
    lock = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return False
    lock.seek(0)
    lock.truncate()
    lock.write(f"{os.getpid()}\n")
    lock.flush()
    _runner_lock["file"] = lock
    return True


def release_runner_lock():
    lock = _runner_lock.pop("file", None)
    if lock is not None:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def run_benchmark():
    tb = tasks_by_id()
    # full paper scope: gen + debug + refmodel(python + cxxrtl) = 310
    all_ids = list(tb.keys())
    pending = [t for t in all_ids if not already_done(t)]
    print(f"scope={len(all_ids)} done={len(all_ids)-len(pending)} pending={len(pending)}", flush=True)
    if not pending:
        print("nothing pending"); write_resume_state(all_ids); return
    validate_environment(tb)

    counters = {"done": 0, "pass": 0}
    lock = threading.Lock()
    remaining = [tb[tid] for tid in pending]
    retry_after_reset = []
    attempts = {}
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        active = {}
        while active or ((remaining or retry_after_reset) and
                         not shutdown_event.is_set()):
            # A session-limit response can arrive before the usage API catches
            # up. Drain in-flight work, wait for the five-hour reset, then retry
            # the affected task(s).
            if stop_event.is_set() and not active and not shutdown_event.is_set():
                if not wait_for_usage(all_ids, force_five_hour_reset=True):
                    break
                stop_event.clear()
                stop_reason["why"] = None
                remaining = retry_after_reset + remaining
                retry_after_reset = []

            while (remaining and len(active) < CONCURRENCY and
                   not stop_event.is_set() and not shutdown_event.is_set()):
                # Required invariant: usage is verified below threshold before
                # every inference task is submitted, within USAGE_CACHE_TTL_SEC.
                if not wait_for_usage(all_ids):
                    break
                # A concurrent inference can report a session limit while this
                # check is in progress. Recheck before committing the launch.
                if stop_event.is_set() or shutdown_event.is_set():
                    break
                stop_reason["why"] = None
                task = remaining.pop(0)
                tid = task["task_id"]
                attempts[tid] = attempts.get(tid, 0) + 1
                fut = ex.submit(worker, task, counters, lock)
                active[fut] = task

            if not active:
                continue

            completed, _ = wait(active, return_when=FIRST_COMPLETED)
            for fut in completed:
                task = active.pop(fut)
                tid = task["task_id"]
                try:
                    tid, status = fut.result()
                except Exception as e:
                    status = f"WORKER_ERR:{type(e).__name__}:{e}"
                if (status in ("GEN_FAILED", "SKIPPED_STOP") and
                    stop_event.is_set() and not shutdown_event.is_set()):
                    retry_after_reset.append(task)
                elif (status == "GEN_FAILED" or status.startswith("SCORE_ERR:") or
                      status.startswith("WORKER_ERR:")) and not shutdown_event.is_set():
                    if attempts[tid] <= MAX_TASK_RETRIES:
                        remaining.append(task)
                        print(f"RETRY {attempts[tid]}/{MAX_TASK_RETRIES} {tid} "
                              f"after {status}", flush=True)
                if status not in ("SKIPPED_STOP",):
                    print(f"[{counters['done']}] {status:12s} {tid}", flush=True)

    state = write_resume_state(all_ids)
    if stop_event.is_set():
        print(f"\nSTOPPED: {stop_reason['why']}", flush=True)
    print(f"resume_state: done={state['done']} pending={state['pending']} "
          f"(scope {state['total']})", flush=True)
    if shutdown_event.is_set():
        return 130
    if state["pending"]:
        print("INCOMPLETE: retries exhausted for one or more pending tasks",
              file=sys.stderr, flush=True)
        return 4
    return 0


def main():
    if not acquire_runner_lock():
        print("ABORT: another run_local.py instance owns .run_local.lock",
              file=sys.stderr, flush=True)
        return 3
    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        try:
            return run_benchmark()
        except Exception as e:
            stop_reason["why"] = f"fatal error: {type(e).__name__}: {e}"
            print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr,
                  flush=True)
            return 1
    finally:
        release_runner_lock()


if __name__ == "__main__":
    sys.exit(main())
