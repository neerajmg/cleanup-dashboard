#!/usr/bin/env python3
"""Backend for the cleanup dashboard.

Scans three things:
  - processes that look like AI agents or agent backends (Claude Code, Codex,
    Ollama, home-grown scripts), skipping OS daemons and Electron helper
    processes, which are noise
  - non-Apple LaunchAgents, including ones already turned off by renaming
    the plist to *.plist.disabled
  - crontab entries

Everything seen goes into review_state.json with first/last seen timestamps
and whatever safe/bogus mark you've given it. That file is what makes the
NEW badge work.

The HTTP side is deliberately restricted. It binds to localhost, and it drops
requests whose Host or Origin header isn't this dashboard, otherwise any page
in your browser could POST to the kill endpoint. Every /api request must also
carry a per-session token that only travels through the URL opened at startup,
because localhost is reachable by every process on the machine, not just
browsers. Actions re-scan before they run and refuse anything not in the
fresh results, so a stale PID or a plist that moved can't be acted on by
mistake.
"""

import atexit
import json
import os
import plistlib
import re
import secrets
import signal
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HOST = "127.0.0.1"
PORT = 8765
STATIC_DIR = Path(__file__).parent / "static"
STATE_PATH = Path(__file__).parent / "review_state.json"

# Per-session secret. Host/Origin checks stop web pages, but any local
# process can talk to a localhost port, so every /api request must also
# carry this token. It reaches the browser only through the URL opened at
# startup (never embedded in the served page, which any process could
# download), and is written to a 0600 file so the app launcher can build
# that URL.
TOKEN = secrets.token_urlsafe(32)
TOKEN_PATH = Path(__file__).parent / ".session_token"

ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}", HOST, "localhost"}
ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}

# Items first seen within this window (and after the store was created)
# get a NEW badge in the UI.
NEW_WINDOW = timedelta(days=7)
# Sightings not refreshed for this long are pruned from the store.
PRUNE_AFTER = timedelta(days=90)

AI_KEYWORDS = (
    "claude", "codex", "anthropic", "openai", "chatgpt", "gpt-",
    "ollama", "langchain", "llama.cpp", "copilot", "gemini-cli",
    "autogpt", "crewai", "mcp-server",
)

# Real OS-owned paths. Processes here are never flagged even if a keyword
# happens to match (e.g. "...Agent" system daemons).
SYSTEM_PATH_PREFIXES = (
    "/System/", "/usr/libexec/", "/usr/sbin/", "/usr/bin/", "/sbin/",
    "/Library/Apple", "/Library/PrivilegedHelperTools/com.apple",
)

# Electron/Chromium multi-process flags: these mark a helper process that
# belongs to a foreground GUI app (renderer/gpu/utility workers), not a
# standalone background job. Killing them just crashes the parent app.
GUI_HELPER_MARKERS = (
    "--type=renderer", "--type=gpu-process", "--type=utility",
    "--type=zygote", "--type=broker", "helper (renderer)",
    "helper (gpu)", "helper (plugin)",
)


def run(args, timeout=10):
    try:
        return subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


# ---------------------------------------------------------------------------
# Review / sighting store
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()

REVIEW_STATUSES = ("keep", "bogus")
ID_RE = re.compile(r"^(proc|agent|cron):.+", re.S)


def _load_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH) as fh:
                state = json.load(fh)
            state.setdefault("meta", {})
            state.setdefault("reviews", {})
            state.setdefault("sightings", {})
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "meta": {"created_at": datetime.now().isoformat(timespec="seconds")},
        "reviews": {},
        "sightings": {},
    }


def _save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    tmp.replace(STATE_PATH)


def record_sightings(item_ids):
    """Update first/last-seen for the given ids; return (reviews, new_ids)."""
    now = datetime.now()
    now_s = now.isoformat(timespec="seconds")
    with _state_lock:
        state = _load_state()
        created = datetime.fromisoformat(
            state["meta"].get("created_at", now_s)
        )
        sightings = state["sightings"]
        for iid in item_ids:
            entry = sightings.setdefault(iid, {"first_seen": now_s})
            entry["last_seen"] = now_s
        # prune sightings (but never reviewed items) not seen in a long time
        cutoff = now - PRUNE_AFTER
        for iid in list(sightings):
            if iid in state["reviews"]:
                continue
            try:
                last = datetime.fromisoformat(sightings[iid]["last_seen"])
            except (KeyError, ValueError):
                continue
            if last < cutoff:
                del sightings[iid]
        _save_state(state)

        new_ids = set()
        for iid in item_ids:
            try:
                first = datetime.fromisoformat(sightings[iid]["first_seen"])
            except (KeyError, ValueError):
                continue
            # Baseline batch (everything present when the store was created)
            # is not "new"; after that, anything first seen recently is.
            if first > created + timedelta(minutes=2) and now - first < NEW_WINDOW:
                new_ids.add(iid)
        return dict(state["reviews"]), new_ids


def set_review(item_id, status):
    if not isinstance(item_id, str) or not ID_RE.match(item_id):
        return False, "invalid item id"
    if status not in REVIEW_STATUSES + ("clear",):
        return False, "status must be keep, bogus, or clear"
    with _state_lock:
        state = _load_state()
        if status == "clear":
            state["reviews"].pop(item_id, None)
        else:
            state["reviews"][item_id] = {
                "status": status,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
        _save_state(state)
    return True, ("cleared" if status == "clear" else f"marked {status}")


def attach_review(items, reviews, new_ids):
    for it in items:
        review = reviews.get(it["id"])
        it["review"] = review["status"] if review else None
        it["new"] = it["id"] in new_ids


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def parse_etime(etime):
    """Parse ps 'etime' ([[dd-]hh:]mm:ss) into seconds."""
    days = 0
    if "-" in etime:
        d, rest = etime.split("-", 1)
        days = int(d)
    else:
        rest = etime
    parts = rest.split(":")
    parts = [int(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return days * 86400 + h * 3600 + m * 60 + s


def human_age(seconds):
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def is_system_path(command):
    for p in SYSTEM_PATH_PREFIXES:
        if p in command:
            return True
    return False


def is_gui_helper(command):
    lower = command.lower()
    return any(marker in lower for marker in GUI_HELPER_MARKERS)


def classify_process(command):
    """Return a type label if this command looks like an AI agent process
    worth surfacing, else None."""
    if is_system_path(command):
        return None
    lower = command.lower()
    if is_gui_helper(lower):
        return None

    if "ollama" in lower:
        return "Ollama"
    if ".vscode/extensions" in lower or "vscode/extensions" in lower:
        if "claude" in lower:
            return "Claude Code CLI (VS Code)"
        if "codex" in lower or "chatgpt" in lower:
            return "Codex / ChatGPT CLI (VS Code)"
        if "copilot" in lower:
            return "Copilot backend (VS Code)"
    if "copilot" in lower and "code helper" in lower:
        return "Copilot backend (VS Code)"
    if "claude.app" in lower:
        return None  # desktop app itself; not a hidden background job

    for kw in AI_KEYWORDS:
        if kw in lower:
            return "Custom AI script/agent"
    return None


# ---------------------------------------------------------------------------
# Assessment — plain-English name, verdict, and explanation for each item.
# Verdicts: needed (don't stop), fine (recognized & harmless),
#           review (worth a look), suspicious (unrecognized or odd location)
# ---------------------------------------------------------------------------

def assess_process(ptype, command):
    lower = command.lower()
    if ptype == "Ollama":
        return ("Ollama — local AI models", "fine",
                "Runs AI models on your Mac. It sits idle until something asks it a "
                "question. Safe to leave; stop it to free memory if you don't use local AI.")
    if ptype == "Claude Code CLI (VS Code)":
        return ("Claude Code — AI assistant", "needed",
                "The AI coding assistant running in VS Code right now. Stopping it "
                "ends your current assistant session.")
    if ptype == "Codex / ChatGPT CLI (VS Code)":
        return ("ChatGPT/Codex extension (VS Code)", "fine",
                "Backend for the ChatGPT extension in VS Code. It stops on its own "
                "when VS Code closes.")
    if ptype == "Copilot backend (VS Code)":
        return ("GitHub Copilot (VS Code)", "fine",
                "Backend for the Copilot AI extension in VS Code. It stops on its "
                "own when VS Code closes.")
    if "/.claude/" in lower and ("zsh" in lower or "bash" in lower or "/sh " in lower):
        return ("Claude Code helper shell", "fine",
                "A small helper that Claude Code starts to run commands. It comes "
                "and goes on its own.")
    if any(p in lower for p in ("/tmp/", "/private/tmp", "/downloads/", "/private/var/folders")):
        return ("Unrecognized program in a temporary folder", "suspicious",
                "Running from a temporary or downloads folder, where legitimate "
                "background software rarely lives. Stop it unless you know exactly "
                "what it is.")
    if "/users/" in lower:
        return ("Your own AI script", "review",
                "A program running from your home folder that mentions an AI tool — "
                "typically something you created. If you don't recognize it, stop it.")
    return ("Unrecognized AI-related program", "review",
            "Mentions an AI tool but isn't in the known list. Check the technical "
            "detail below; stop it if you don't recognize it.")


# Known LaunchAgent vendors, matched by label prefix.
AGENT_KNOWLEDGE = (
    ("com.google.keystone", "Google software updater", "fine",
     "Keeps Google apps like Chrome up to date. Standard and harmless."),
    ("com.google.googleupdater", "Google software updater", "fine",
     "Keeps Google apps like Chrome up to date. Standard and harmless."),
    ("us.zoom", "Zoom updater", "fine",
     "Keeps Zoom up to date. Standard and harmless."),
    ("com.microsoft.update", "Microsoft app updater", "fine",
     "Keeps Microsoft apps (Office, Teams…) up to date. Standard and harmless."),
    ("ubf8t346g9.com.microsoft.entrabroker", "Microsoft work/school sign-in helper", "fine",
     "Part of signing in to a work or school Microsoft account (Company Portal)."),
    ("org.chromium.chromoting", "Chrome Remote Desktop", "review",
     "Allows this Mac to be controlled remotely via Chrome Remote Desktop. Fine "
     "if you use it — turn it off if you don't, since it grants remote access."),
    ("com.p5sys.jump", "Jump Desktop remote access", "review",
     "Allows this Mac to be controlled remotely via Jump Desktop. Fine if you "
     "use it — turn it off if you don't, since it grants remote access."),
    ("com.federicoterzi.espanso", "Espanso text expander", "fine",
     "Expands typing shortcuts into longer text as you type."),
    ("org.freedownloadmanager", "Free Download Manager helper", "fine",
     "Helper for the Free Download Manager app."),
    ("com.huion", "Huion tablet driver", "fine",
     "Driver for a Huion drawing tablet."),
    ("com.alertus", "Alertus desktop alerts", "fine",
     "Emergency notification client, usually installed by a university or employer."),
    ("com.logi", "Logitech device software", "fine",
     "Supports Logitech mice, keyboards, and webcams."),
)


def assess_agent(label, command, user_owned):
    lower_label = label.lower()
    # macOS keeps its own agents in /System/Library, which this tool never
    # scans, so an Apple label found here is out of place. Usually installer
    # residue; occasionally something borrowing the name to look official.
    if lower_label.startswith("com.apple."):
        return ("Apple-named task outside the system folders", "review",
                "Carries an Apple label, but macOS stores its own agents in "
                "/System/Library, not here. Often left behind by an installer, "
                "and occasionally software using the name to look official. "
                "Worth reading the command below before you keep it.")
    for prefix, name, verdict, expl in AGENT_KNOWLEDGE:
        if lower_label.startswith(prefix):
            return (name, verdict, expl)
    lower_cmd = command.lower()
    if any(p in lower_cmd for p in ("/tmp/", "/private/tmp", "/downloads/")):
        return ("Unrecognized scheduled task (temporary folder)", "suspicious",
                "Scheduled to run a program from a temporary or downloads folder — "
                "legitimate software rarely does this. Turn it off unless you know it.")
    if user_owned and "/users/" in lower_cmd and ".app/" not in lower_cmd:
        return ("Your own scheduled agent", "review",
                "A scheduled task on your account that runs a script from your home "
                "folder — typically something you set up yourself. If you don't "
                "remember creating it, turn it off.")
    if user_owned:
        return ("App-installed scheduled task", "review",
                "Added to your account by an app, but not in the known list. Usually "
                "fine — review what it runs below.")
    return ("App-installed scheduled task", "review",
            "Installed system-wide by an application, but not in the known list. "
            "Usually fine — review what it runs below.")


def assess_cron(raw):
    lower = raw.lower()
    if any(p in lower for p in ("/tmp/", "/downloads/")):
        return ("Scheduled command (cron) — temporary folder", "suspicious",
                "Runs a command from a temporary or downloads folder on a schedule. "
                "Remove it unless you know exactly what it is.")
    return ("Scheduled command (cron)", "review",
            "Runs automatically on a schedule. If you don't recognize it, remove it.")


def get_exe_paths():
    """pid -> full executable path (ps 'comm' has no args, so spaces in the
    path survive; splitting the command string on whitespace would not)."""
    proc = run(["ps", "-eo", "pid=,comm="])
    paths = {}
    if proc is None or proc.returncode != 0:
        return paths
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            paths[int(parts[0])] = parts[1]
    return paths


def get_processes():
    proc = run(["ps", "-eo", "pid,ppid,etime,pcpu,rss,command"])
    if proc is None or proc.returncode != 0:
        return []
    exe_paths = get_exe_paths()
    lines = proc.stdout.splitlines()[1:]
    now = datetime.now()
    results = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        pid, ppid, etime, pcpu, rss, command = fields
        ptype = classify_process(command)
        if not ptype:
            continue
        try:
            age_seconds = parse_etime(etime)
        except ValueError:
            age_seconds = 0
        started = now - timedelta(seconds=age_seconds)
        exe = exe_paths.get(int(pid), command.split(None, 1)[0])
        friendly, verdict, explanation = assess_process(ptype, command)
        results.append({
            "id": "proc:" + exe,
            "friendly_name": friendly,
            "verdict": verdict,
            "explanation": explanation,
            "pid": int(pid),
            "ppid": int(ppid),
            "type": ptype,
            "started": started.isoformat(timespec="seconds"),
            "age_seconds": age_seconds,
            "age_human": human_age(age_seconds),
            "cpu_pct": float(pcpu),
            "rss_kb": int(rss),
            "command": command,
        })
    results.sort(key=lambda r: -r["age_seconds"])
    return results


WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def _format_days(days):
    """Compact day list: consecutive runs become ranges (Mon-Fri)."""
    days = sorted(days)
    runs = []
    for d in days:
        if runs and d == runs[-1][-1] + 1:
            runs[-1].append(d)
        else:
            runs.append([d])
    out = []
    for r in runs:
        if len(r) >= 3:
            out.append(f"{WEEKDAYS[r[0] % 7]}-{WEEKDAYS[r[-1] % 7]}")
        else:
            out.extend(WEEKDAYS[d % 7] for d in r)
    return ", ".join(out)


def describe_schedule(plist):
    parts = []
    if plist.get("RunAtLoad"):
        parts.append("at login")
    interval = plist.get("StartInterval")
    if interval:
        parts.append(f"every {human_age(int(interval))}")
    cal = plist.get("StartCalendarInterval")
    if isinstance(cal, dict):
        cal = [cal]
    if isinstance(cal, list):
        # Group calendar entries so "same times, Mon..Fri" reads as
        # "Mon-Fri 08:00, 16:00" instead of ten separate entries.
        day_times = {}  # weekday (None = every day) -> [times]
        for entry in cal:
            wd = entry.get("Weekday")
            hh = entry.get("Hour", 0)
            mm = entry.get("Minute", 0)
            day_times.setdefault(wd, []).append(f"{hh:02d}:{mm:02d}")
        by_times = {}  # tuple(times) -> [weekdays]
        for wd, times in day_times.items():
            key = tuple(sorted(set(times)))
            by_times.setdefault(key, []).append(wd)
        for times, wds in sorted(by_times.items()):
            if wds == [None]:
                parts.append(f"every day {', '.join(times)}")
            else:
                days = _format_days([w for w in wds if w is not None])
                parts.append(f"{days} {', '.join(times)}")
    return "; ".join(parts) if parts else "no schedule (manual/on-demand)"


def get_loaded_labels():
    proc = run(["launchctl", "list"])
    loaded = {}
    if proc is None or proc.returncode != 0:
        return loaded
    for line in proc.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        pid_s, status, label = cols[0], cols[1], cols[2]
        loaded[label] = {
            "pid": int(pid_s) if pid_s.isdigit() else None,
            "status": status,
        }
    return loaded


DISABLED_SUFFIX = ".plist.disabled"


def get_launch_agents():
    loaded = get_loaded_labels()
    dirs = [
        Path.home() / "Library" / "LaunchAgents",
        Path("/Library/LaunchAgents"),
    ]
    results = []
    for d in dirs:
        if not d.is_dir():
            continue
        files = sorted(d.glob("*.plist")) + sorted(d.glob("*" + DISABLED_SUFFIX))
        for f in files:
            disabled = f.name.endswith(DISABLED_SUFFIX)
            try:
                with open(f, "rb") as fh:
                    plist = plistlib.load(fh)
            except Exception:
                continue
            fallback = f.name[:-len(DISABLED_SUFFIX)] if disabled else f.stem
            label = plist.get("Label", fallback)
            is_apple = label.startswith("com.apple.")
            prog_args = plist.get("ProgramArguments")
            program = plist.get("Program")
            if prog_args:
                command = " ".join(str(a) for a in prog_args)
            elif program:
                command = str(program)
            else:
                command = "(no program specified)"
            lower = (label + " " + command).lower()
            ai_related = any(kw in lower for kw in AI_KEYWORDS)
            info = None if disabled else loaded.get(label)
            user_owned = str(d).startswith(str(Path.home()))
            friendly, verdict, explanation = assess_agent(label, command, user_owned)
            results.append({
                "id": "agent:" + label,
                "friendly_name": friendly,
                "verdict": verdict,
                "explanation": explanation,
                "label": label,
                "plist_path": str(f),
                "user_owned": user_owned,
                "command": command,
                "schedule": describe_schedule(plist),
                "disabled": disabled,
                "loaded": info is not None,
                "pid": info["pid"] if info else None,
                "apple": is_apple,
                "ai_related": ai_related,
            })
    results.sort(key=lambda r: (not r["ai_related"], r["label"]))
    return results


def get_cron_jobs():
    proc = run(["crontab", "-l"])
    if proc is None or proc.returncode != 0:
        return []
    results = []
    for idx, line in enumerate(proc.stdout.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lower = stripped.lower()
        ai_related = any(kw in lower for kw in AI_KEYWORDS)
        friendly, verdict, explanation = assess_cron(stripped)
        results.append({
            "id": "cron:" + stripped,
            "friendly_name": friendly,
            "verdict": verdict,
            "explanation": explanation,
            "index": idx,
            "raw": line,
            "ai_related": ai_related,
        })
    return results


def build_scan():
    processes = get_processes()
    launch_agents = get_launch_agents()
    cron_jobs = get_cron_jobs()

    all_items = processes + launch_agents + cron_jobs
    reviews, new_ids = record_sightings([it["id"] for it in all_items])
    for group in (processes, launch_agents, cron_jobs):
        attach_review(group, reviews, new_ids)

    # Needs attention = flagged by the auto-assessment and not yet marked
    # safe/bogus by the user.
    needs_review = sum(
        1 for it in all_items
        if reviews.get(it["id"]) is None and it["verdict"] in ("review", "suspicious")
    )
    bogus = sum(
        1 for it in all_items
        if (reviews.get(it["id"]) or {}).get("status") == "bogus"
    )
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "processes": processes,
        "launch_agents": launch_agents,
        "cron_jobs": cron_jobs,
        "summary": {
            "process_count": len(processes),
            "total_rss_mb": round(sum(p["rss_kb"] for p in processes) / 1024, 1),
            "launch_agent_count": len(launch_agents),
            "cron_count": len(cron_jobs),
            "needs_review_count": needs_review,
            "bogus_count": bogus,
        },
    }


# ---------------------------------------------------------------------------
# Actions — each re-validates its target against a fresh scan first
# ---------------------------------------------------------------------------

LABEL_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")


def kill_process(pid, force=False):
    if not isinstance(pid, int) or pid <= 1:
        return False, "invalid pid"
    current = {p["pid"]: p for p in get_processes()}
    if pid not in current:
        return False, "process is not a currently-scanned AI agent process (refusing to kill)"
    try:
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        return True, "already gone"
    except PermissionError:
        return False, "permission denied (owned by another user)"
    return True, "killed"


def _find_agent(label):
    if not LABEL_RE.match(label or ""):
        return None
    agents = [a for a in get_launch_agents() if a["label"] == label]
    if not agents:
        return None
    # Prefer the enabled entry if both an enabled and a disabled copy exist.
    agents.sort(key=lambda a: a["disabled"])
    return agents[0]


def _bootout(label):
    uid = os.getuid()
    proc = run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    # rc 3 / "No such process" = wasn't loaded, which is fine for our purposes
    ok = proc is not None and proc.returncode in (0, 3)
    if not ok and proc is not None and "No such process" in (proc.stderr or ""):
        ok = True
    return ok, (proc.stderr.strip() if proc else "launchctl failed")


def unload_launch_agent(label):
    agent = _find_agent(label)
    if not agent:
        return False, "not found in current scan (refusing to act)"
    ok, err = _bootout(label)
    return ok, ("unloaded (will return at next login unless disabled)" if ok else err)


def disable_launch_agent(label):
    """Unload now AND rename the plist to *.plist.disabled so it can't come
    back at next login. Reversible via enable."""
    agent = _find_agent(label)
    if not agent:
        return False, "not found in current scan (refusing to act)"
    if agent["disabled"]:
        return True, "already disabled"
    ok, err = _bootout(label)
    if not ok:
        return False, err
    if not agent["user_owned"]:
        return True, "unloaded for this session; system-owned plist needs sudo to disable permanently"
    src = Path(agent["plist_path"])
    try:
        src.rename(src.with_name(src.name + ".disabled"))
    except OSError as e:
        return False, f"unloaded, but rename failed: {e}"
    return True, "disabled (unloaded + plist renamed)"


def enable_launch_agent(label):
    agent = _find_agent(label)
    if not agent:
        return False, "not found in current scan (refusing to act)"
    if not agent["disabled"]:
        return True, "already enabled"
    if not agent["user_owned"]:
        return False, "system-owned plist needs sudo to enable"
    src = Path(agent["plist_path"])
    dst = src.with_name(src.name[:-len(".disabled")])
    try:
        src.rename(dst)
    except OSError as e:
        return False, f"rename failed: {e}"
    uid = os.getuid()
    proc = run(["launchctl", "bootstrap", f"gui/{uid}", str(dst)])
    if proc is None or proc.returncode != 0:
        msg = proc.stderr.strip() if proc else "launchctl failed"
        return True, f"plist restored, but load failed ({msg}); it will load at next login"
    return True, "enabled and loaded"


def delete_launch_agent(label):
    agent = _find_agent(label)
    if not agent:
        return False, "not found in current scan (refusing to act)"
    if not agent["user_owned"]:
        return False, "refusing to delete a system-owned plist (needs sudo)"
    ok, err = _bootout(label)
    if not ok:
        return False, err
    try:
        Path(agent["plist_path"]).unlink()
    except OSError as e:
        return False, f"unloaded, but delete failed: {e}"
    return True, "unloaded and plist deleted"


def remove_cron_job(index):
    jobs = get_cron_jobs()
    target = next((j for j in jobs if j["index"] == index), None)
    if target is None:
        return False, "cron line not found in current crontab (refusing to act)"
    proc = run(["crontab", "-l"])
    if proc is None or proc.returncode != 0:
        return False, "could not read crontab"
    lines = proc.stdout.splitlines()
    if index >= len(lines) or lines[index] != target["raw"]:
        return False, "crontab changed since scan, refusing to act"
    new_lines = lines[:index] + lines[index + 1:]
    new_content = "\n".join(new_lines)
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"
    write_proc = subprocess.run(
        ["crontab", "-"], input=new_content, capture_output=True, text=True
    )
    if write_proc.returncode != 0:
        return False, write_proc.stderr.strip() or "crontab write failed"
    return True, "removed"


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _origin_ok(self, is_post):
        """Reject requests that don't come from the dashboard itself.
        Browsers always send Host, and send Origin on cross-site POSTs, so
        this blocks CSRF/DNS-rebinding against the destructive endpoints."""
        host = self.headers.get("Host", "")
        if host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if is_post and origin is not None and origin not in ALLOWED_ORIGINS:
            return False
        return True

    def _forbidden(self):
        self._send_json({"ok": False, "message": "forbidden origin"}, status=403)

    def _token_ok(self):
        supplied = self.headers.get("X-Auth-Token", "")
        return secrets.compare_digest(supplied, TOKEN)

    def _unauthorized(self):
        self._send_json(
            {"ok": False, "message": "missing or bad session token"}, status=403
        )

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if not self._origin_ok(is_post=False):
            self._forbidden()
            return
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            f = STATIC_DIR / "index.html"
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/scan":
            if not self._token_ok():
                self._unauthorized()
                return
            self._send_json(build_scan())
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if not self._origin_ok(is_post=True):
            self._forbidden()
            return
        if not self._token_ok():
            self._unauthorized()
            return
        parsed = urlparse(self.path)
        data = self._read_json()

        if parsed.path == "/api/process/kill":
            ok, msg = kill_process(data.get("pid"), force=bool(data.get("force")))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/launchagent/unload":
            ok, msg = unload_launch_agent(data.get("label"))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/launchagent/disable":
            ok, msg = disable_launch_agent(data.get("label"))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/launchagent/enable":
            ok, msg = enable_launch_agent(data.get("label"))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/launchagent/delete":
            ok, msg = delete_launch_agent(data.get("label"))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/cron/remove":
            ok, msg = remove_cron_job(data.get("index"))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/review":
            ok, msg = set_review(data.get("id"), data.get("status"))
            self._send_json({"ok": ok, "message": msg})
            return

        self.send_response(404)
        self.end_headers()


def _write_token_file():
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(TOKEN_PATH, flags, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(TOKEN)

    def _cleanup():
        try:
            TOKEN_PATH.unlink()
        except OSError:
            pass
    atexit.register(_cleanup)


def main():
    # The app's Quit sends SIGTERM; without this, atexit never runs and the
    # token file outlives the server it belonged to.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    _write_token_file()
    url = f"http://{HOST}:{PORT}/?token={TOKEN}"
    print(f"Cleanup dashboard running at {url}")
    print("(the token is this session's key; Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
