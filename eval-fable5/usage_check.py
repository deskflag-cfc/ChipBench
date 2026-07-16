#!/usr/bin/env python3
"""
Query the live subscription usage API and print machine-readable utilization.

Token source (first that works):
  1. env CLAUDE_OAUTH_TOKEN   (raw access token)
  2. macOS Keychain "Claude Code-credentials" (the live CLI credential)
  3. env CLAUDE_TOKEN_FILE / local .oauth_token
  4. env CLAUDE_CRED_FILE / ~/.claude/.credentials.json

Endpoint: GET https://api.anthropic.com/api/oauth/usage
Headers:  Authorization: Bearer <tok> ; anthropic-beta: oauth-2025-04-20

Prints:
  FIVE_HOUR_UTIL=<pct>
  SEVEN_DAY_UTIL=<pct>
  FIVE_HOUR_RESETS_AT=<unix epoch, or -1>
  SEVEN_DAY_RESETS_AT=<unix epoch, or -1>
  FIVE_HOUR_RESETS_IN_MIN=<min>
  RETRY_AFTER=<sec>            (only when the endpoint answered HTTP 429)
  DECISION=OK|STOP
Exit code: 0 if OK (below thresholds), 10 if STOP (at/over), 2 if token/API error.
"""
import json, os, subprocess, sys, time, urllib.error, urllib.request

FIVE_HOUR_THRESHOLD = float(os.environ.get("FIVE_HOUR_THRESHOLD", "80"))
SEVEN_DAY_THRESHOLD = float(os.environ.get("SEVEN_DAY_THRESHOLD", "90"))
CLI_PROBE_COOLDOWN_SEC = int(os.environ.get("CLI_PROBE_COOLDOWN_SEC", "3600"))
USAGE_HTTP_TIMEOUT = int(os.environ.get("USAGE_HTTP_TIMEOUT", "15"))
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PAUSE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".runner_pause")
CLI_PROBE_STAMP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               ".last_cli_probe")


def token_candidates():
    """Yield distinct tokens, preferring Claude CLI's refreshed credential."""
    seen = set()

    def emit(tok):
        tok = tok.strip() if isinstance(tok, str) else None
        if tok and tok not in seen:
            seen.add(tok)
            return tok
        return None

    t = os.environ.get("CLAUDE_OAUTH_TOKEN")
    if emit(t):
        yield t.strip()

    # Claude Code on macOS refreshes this credential automatically. Reading it
    # at check time avoids pinning the runner to an expired copied token.
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-w", "-s",
             "Claude Code-credentials"],
            capture_output=True, text=True, timeout=10,
        )
        if p.returncode == 0:
            d = json.loads(p.stdout)
            tok = d.get("claudeAiOauth", {}).get("accessToken")
            if emit(tok):
                yield tok.strip()
    except Exception:
        pass

    # raw-token file (default: .oauth_token next to this script)
    raw_paths = [os.environ.get("CLAUDE_TOKEN_FILE"),
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), ".oauth_token")]
    for p in raw_paths:
        if p and os.path.exists(p):
            try:
                v = open(p).read().strip()
                if v.startswith("sk-ant-") and emit(v):
                    yield v
            except Exception:
                pass
    # JSON credential files
    for p in (os.environ.get("CLAUDE_CRED_FILE"),
              os.path.expanduser("~/.claude/.credentials.json")):
        if p and os.path.exists(p):
            try:
                d = json.load(open(p))
                tok = d.get("claudeAiOauth", {}).get("accessToken")
                if emit(tok):
                    yield tok.strip()
            except Exception:
                pass


def fetch_usage(tok):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tok}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=USAGE_HTTP_TIMEOUT) as r:
        return json.load(r)


def try_tokens(tokens):
    """Return (data, last_error, retry_after).

    Candidates exist to survive an expired or missing credential, so we only
    fall through on auth/transport errors. A 429 means the credential is valid
    but throttled: another token cannot lift a rate limit, and trying one just
    doubles the load on an endpoint that is already refusing us. Stop and report
    the window instead.
    """
    last_error = None
    retry_after = None
    for tok in tokens:
        try:
            return fetch_usage(tok), None, None
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code == 429:
                try:
                    retry_after = float(e.headers.get("retry-after"))
                except (TypeError, ValueError):
                    retry_after = None
                break
        except Exception as e:
            last_error = e
    return None, last_error, retry_after


def probe_cli_rate_limit():
    """Refresh Claude's OAuth credential and return its five-hour gate event."""
    try:
        p = subprocess.run(
            ["claude", "-p", "Reply only OK.",
             "--system-prompt", "Reply only OK.",
             "--model", "claude-haiku-4-5-20251001",
             "--output-format", "stream-json", "--verbose", "--tools", ""],
            capture_output=True, text=True, timeout=90, cwd="/tmp",
        )
    except Exception:
        return None, None
    for line in p.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "rate_limit_event":
            info = event.get("rate_limit_info", {})
            return info.get("status"), info.get("resetsAt")
    return None, None


def cli_probe_is_due():
    try:
        age = time.time() - os.path.getmtime(CLI_PROBE_STAMP)
        return age >= CLI_PROBE_COOLDOWN_SEC
    except OSError:
        return True


def mark_cli_probe():
    # Mark before probing so a timeout/crash cannot cause a tight inference loop.
    with open(CLI_PROBE_STAMP, "a"):
        os.utime(CLI_PROBE_STAMP, None)


def pick(window):
    """Return (utilization_pct, resets_at_epoch) from a usage window dict."""
    util = window.get("utilization")
    if util is None:
        util = window.get("used_pct") or window.get("percent") or 0
    resets = window.get("resets_at") or window.get("resetsAt")
    return float(util or 0), resets


def main():
    if os.path.exists(PAUSE_FILE):
        print("DECISION=ERROR")
        print("# runner administratively paused by .runner_pause", file=sys.stderr)
        return 2

    tokens = list(token_candidates())
    if not tokens:
        print("DECISION=ERROR")
        print("# no token: set CLAUDE_OAUTH_TOKEN or CLAUDE_CRED_FILE", file=sys.stderr)
        return 2

    data, last_error, retry_after = try_tokens(tokens)
    if data is None:
        # A copied OAuth token can remain HTTP-429 after a five-hour reset until
        # Claude CLI refreshes its live Keychain credential. Do one cheap probe,
        # then retry the numeric endpoint with freshly discovered credentials.
        cli_status = cli_reset = None
        if cli_probe_is_due():
            mark_cli_probe()
            cli_status, cli_reset = probe_cli_rate_limit()
        if cli_status == "rejected":
            data = {
                "five_hour": {"utilization": 100, "resets_at": cli_reset},
                "seven_day": {},
            }
        elif cli_status in ("allowed", "allowed_warning"):
            data, last_error, retry_after = try_tokens(list(token_candidates()))
        if data is None:
            print("DECISION=ERROR")
            if retry_after is not None:
                print(f"RETRY_AFTER={retry_after}")
            print(f"# usage api error: {last_error}", file=sys.stderr)
            return 2

    fh = data.get("five_hour", {}) or {}
    sd = data.get("seven_day", {}) or {}
    fh_util, fh_reset = pick(fh)
    sd_util, sd_reset = pick(sd)

    def mins(resets):
        if not resets:
            return -1
        try:
            if isinstance(resets, str):
                import datetime
                dt = datetime.datetime.fromisoformat(resets.replace("Z", "+00:00"))
                return int((dt.timestamp() - time.time()) / 60)
            return int((float(resets) - time.time()) / 60)
        except Exception:
            return -1

    def epoch(resets):
        if not resets:
            return -1
        try:
            if isinstance(resets, str):
                import datetime
                return datetime.datetime.fromisoformat(
                    resets.replace("Z", "+00:00")
                ).timestamp()
            return float(resets)
        except Exception:
            return -1

    decision = "STOP" if (fh_util >= FIVE_HOUR_THRESHOLD or sd_util >= SEVEN_DAY_THRESHOLD) else "OK"
    print(f"FIVE_HOUR_UTIL={fh_util}")
    print(f"SEVEN_DAY_UTIL={sd_util}")
    print(f"FIVE_HOUR_RESETS_AT={epoch(fh_reset)}")
    print(f"SEVEN_DAY_RESETS_AT={epoch(sd_reset)}")
    print(f"FIVE_HOUR_RESETS_IN_MIN={mins(fh_reset)}")
    print(f"DECISION={decision}")
    print(f"# 5h={fh_util}% (resets in {mins(fh_reset)} min) | 7d={sd_util}% | "
          f"thr 5h>={FIVE_HOUR_THRESHOLD} 7d>={SEVEN_DAY_THRESHOLD} -> {decision}")
    return 10 if decision == "STOP" else 0


if __name__ == "__main__":
    sys.exit(main())
