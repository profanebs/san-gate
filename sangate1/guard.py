#!/usr/bin/env python3
"""san-gate control - one switch for the whole guard.

  san-gate.py on              enable the guard
  san-gate.py off             disable the guard
  san-gate.py status          show current posture and drift
  san-gate.py check <target>  run preflight on a file, dir or URL
  san-gate.py run -- <cmd>    run a command with a scrubbed environment
  san-gate.py sanitize <file> write a copy with invisible characters removed
  san-gate.py verify          self-test against the bundled fixtures

Exit codes mirror preflight: 0 GREEN, 1 YELLOW, 2 RED, 3 ERROR.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

VERSION = "2.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.environ.get("SAN_GATE_HOME") or os.path.join(os.path.expanduser("~"), ".san-gate")
STATE_FILE = os.path.join(HOME, "state.json")
PREFLIGHT = os.path.join(HERE, "preflight.py")
FIXTURES = os.path.join(HERE, "tests", "fixtures")

SAFE_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "OS", "LANG", "LC_ALL", "TERM",
}

SECRET_HINTS = ("password", "passwd", "token", "key", "secret", "recovery", "codes",
                "rustdesk", "sunlogin", "remote")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"mode": "unknown", "since": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=True, indent=2)


def npm_ignore_scripts():
    exe = shutil.which("npm")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "config", "get", "ignore-scripts"],
                             capture_output=True, text=True, timeout=20)
        return out.stdout.strip()
    except Exception:
        return None


def set_npm_ignore_scripts(value):
    exe = shutil.which("npm")
    if not exe:
        return "npm not found, skipped"
    try:
        subprocess.run([exe, "config", "set", "ignore-scripts", value],
                       capture_output=True, text=True, timeout=20)
        return "npm ignore-scripts=%s" % value
    except Exception as exc:
        return "npm update failed: %s" % exc


def discover_agents_files():
    """AGENTS.md locations that the running agent is likely to load."""
    out = []
    for base in (os.getcwd(), os.path.expanduser("~/.config/opencode"),
                 os.path.expanduser("~/.opencode")):
        for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                out.append(path)
    return out


def check_agents_block():
    found = []
    for path in discover_agents_files():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if "san-gate" in fh.read().lower():
                    found.append(path)
        except OSError:
            continue
    return found


def count_plaintext_secrets(secrets_dir):
    if not os.path.isdir(secrets_dir):
        return None
    risky = 0
    total = 0
    for name in os.listdir(secrets_dir):
        path = os.path.join(secrets_dir, name)
        if not os.path.isfile(path) or name.startswith("."):
            continue
        total += 1
        low = name.lower()
        if any(h in low for h in SECRET_HINTS) and not low.endswith((".enc", ".md")):
            risky += 1
    return risky, total


def cmd_on(args):
    state = {
        "mode": "strict",
        "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "version": VERSION,
    }
    save_state(state)
    print("san-gate: ON (strict)")
    if "--no-npm" not in args:
        print("  " + set_npm_ignore_scripts("true"))
    print("  rule: any artifact that was not written by the user must pass")
    print("        `san-gate.py check` before it is executed or installed.")
    print("  RED     -> stop, report to the user, do not proceed")
    print("  YELLOW  -> proceed only after the finding is explained in the report")
    print("  GREEN   -> proceed")
    return 0


def cmd_off(args):
    state = load_state()
    state["mode"] = "off"
    state["since"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_state(state)
    print("san-gate: OFF")
    if "--keep-npm" not in args:
        print("  " + set_npm_ignore_scripts("false"))
    print("  warning: the guard is inert. Untrusted artifacts will run unscanned.")
    return 0


def cmd_status(args):
    state = load_state()
    mode = state.get("mode", "unknown")
    print("san-gate v%s" % VERSION)
    print("  mode            : %s%s" % (mode, "  (since %s)" % state["since"] if state.get("since") else ""))
    print("  npm ignore-scripts: %s" % npm_ignore_scripts())
    agents = check_agents_block()
    print("  AGENTS.md block : %s" % ("present in %d file(s)" % len(agents) if agents else "MISSING"))
    for path in agents:
        print("                    %s" % path)
    secrets_dir = os.environ.get("SAN_GATE_SECRETS_DIR", "")
    if not secrets_dir:
        guess = os.path.join(os.path.dirname(os.path.dirname(HOME)), "secrets")
        if os.path.isdir(guess):
            secrets_dir = guess
    if secrets_dir:
        stats = count_plaintext_secrets(secrets_dir)
        if stats:
            print("  secrets at rest : %d plaintext file(s) of which %d look critical" % (stats[1], stats[0]))
    else:
        print("  secrets at rest : not configured (set SAN_GATE_SECRETS_DIR)")
    ok = mode == "strict" and npm_ignore_scripts() == "true" and agents
    print("  verdict         : %s" % ("armed" if ok else "NOT FULLY ARMED"))
    return 0 if ok else 1


def cmd_check(args):
    if not args:
        print("usage: san-gate.py check <file|dir|url> [--json]")
        return 3
    cmd = [sys.executable, PREFLIGHT] + args
    return subprocess.call(cmd)


def cmd_run(args):
    if "--" in args:
        args = args[args.index("--") + 1:]
    if not args:
        print("usage: san-gate.py run -- <command> [args...]")
        return 3
    timeout = 300
    for token in list(args):
        if token.startswith("--timeout="):
            timeout = int(token.split("=", 1)[1])
            args.remove(token)

    workdir = tempfile.mkdtemp(prefix="sangate-run-")
    env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV_KEYS}
    env["HOME"] = workdir
    env["USERPROFILE"] = workdir
    env["TMP"] = workdir
    env["TEMP"] = workdir
    print("san-gate run: cwd=%s" % workdir)
    print("san-gate run: env scrubbed to %d keys" % len(env))
    try:
        proc = subprocess.run(args, cwd=workdir, env=env, timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        print("san-gate run: timed out after %ss" % timeout)
        code = 124
    except FileNotFoundError:
        print("san-gate run: command not found: %s" % args[0])
        code = 127
    return code


def cmd_sanitize(args):
    if not args:
        print("usage: san-gate.py sanitize <file> [-o out]")
        return 3
    src = args[0]
    out = None
    if "-o" in args:
        out = args[args.index("-o") + 1]
    with open(src, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    cleaned = []
    removed = 0
    for ch in text:
        cp = ord(ch)
        if 0xE0000 <= cp <= 0xE007F or cp in (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF,
                                              0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                                              0x2066, 0x2067, 0x2068, 0x2069, 0x00AD):
            removed += 1
            continue
        cleaned.append(ch)
    if out is None:
        base, ext = os.path.splitext(src)
        out = base + ".clean" + ext
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("".join(cleaned))
    print("san-gate sanitize: removed %d invisible character(s)" % removed)
    print("san-gate sanitize: wrote %s" % out)
    return 0


def cmd_verify(args):
    if not os.path.isdir(FIXTURES):
        print("san-gate verify: fixtures missing at %s" % FIXTURES)
        return 3
    expect = {
        "clean-notes.md": "GREEN",
        "evil-readme.md": "RED",
        "smuggled-tagchars.md": "RED",
        "zero-width.md": "YELLOW",
        "evil-script.ps1": "RED",
        "exfil-creds.py": "RED",
        "disguised.pdf": "RED",
        "dropper.zip": "RED",
        "traversal.zip": "RED",
    }
    failures = 0
    for name, want in expect.items():
        path = os.path.join(FIXTURES, name)
        if not os.path.exists(path):
            print("  MISSING %-24s" % name)
            failures += 1
            continue
        proc = subprocess.run([sys.executable, PREFLIGHT, path, "--quiet"],
                              capture_output=True, text=True)
        got = (proc.stdout or "").strip().splitlines()
        got = got[-1] if got else "ERROR"
        flag = "ok  " if got == want else "FAIL"
        if got != want:
            failures += 1
        print("  %s %-24s want=%-6s got=%s" % (flag, name, want, got))
    print("")
    print("san-gate verify: %s" % ("all fixtures matched" if not failures else "%d failure(s)" % failures))
    return 0 if not failures else 2


COMMANDS = {
    "on": cmd_on,
    "off": cmd_off,
    "status": cmd_status,
    "check": cmd_check,
    "scan": cmd_check,
    "run": cmd_run,
    "sanitize": cmd_sanitize,
    "verify": cmd_verify,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd not in COMMANDS:
        print("san-gate: unknown command '%s'" % cmd)
        print(__doc__)
        return 3
    return COMMANDS[cmd](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
