#!/usr/bin/env python3
"""Backend for the cleanup dashboard.

Scans three things:
  - processes that look like AI agents or agent backends (Claude Code, Codex,
    Ollama, home-grown scripts), skipping OS daemons and Electron helper
    processes, which are noise
  - LaunchAgents in ~/Library and /Library, including ones already turned
    off, whether by renaming the plist to *.plist.disabled (what this does
    to plists you own) or by a launchd override (what it does to the rest,
    since renaming those needs sudo). macOS keeps its own agents in /System
    and /Library/Apple, which are left alone.
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
            state.setdefault("agent_state", {})
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "meta": {"created_at": datetime.now().isoformat(timespec="seconds")},
        "reviews": {},
        "sightings": {},
        "agent_state": {},
    }


def _save_state(state):
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1)
    tmp.replace(STATE_PATH)


def _legacy_agent_id(item_id):
    """Agent ids gained an "@<directory>" suffix once it became clear the
    same label can live in two folders. Map a current id back to the old
    form so existing marks and first-seen dates aren't orphaned."""
    if item_id.startswith("agent:") and "@" in item_id:
        return item_id.rsplit("@", 1)[0]
    return None


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
            entry = sightings.get(iid)
            if entry is None:
                legacy = _legacy_agent_id(iid)
                seed = sightings.get(legacy) if legacy else None
                entry = {"first_seen": seed["first_seen"] if seed else now_s}
                sightings[iid] = entry
            entry["last_seen"] = now_s
        # prune sightings (but never reviewed items) not seen in a long time
        cutoff = now - PRUNE_AFTER
        agent_state = state.setdefault("agent_state", {})
        for iid in list(sightings):
            if iid in state["reviews"]:
                continue
            try:
                last = datetime.fromisoformat(sightings[iid]["last_seen"])
            except (KeyError, ValueError):
                continue
            if last < cutoff:
                del sightings[iid]
                agent_state.pop(iid, None)
        # remembered load states for items whose sighting is long gone
        for iid in list(agent_state):
            if iid not in sightings:
                agent_state.pop(iid, None)
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


def remember_load_state(item_id, was_loaded):
    """Note whether an agent was actually running when it was turned off, so
    turning it back on restores that rather than starting something that had
    been sitting idle."""
    with _state_lock:
        state = _load_state()
        state.setdefault("agent_state", {})[item_id] = {"was_loaded": bool(was_loaded)}
        _save_state(state)


def recall_load_state(item_id, default=True):
    with _state_lock:
        entry = _load_state().get("agent_state", {}).get(item_id)
    return default if entry is None else entry.get("was_loaded", default)


def attach_review(items, reviews, new_ids):
    for it in items:
        review = reviews.get(it["id"])
        if review is None:
            legacy = _legacy_agent_id(it["id"])
            review = reviews.get(legacy) if legacy else None
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


# Software whose job is to protect the person or the fleet: emergency
# notification, anti-malware, VPN, device management. Switching these off is
# a decision with consequences past this Mac, and someone reviewing a list of
# background tasks won't necessarily know that, so the page warns before it
# lets them.
PROTECTED_PREFIXES = (
    ("com.alertus", "emergency alerts"),
    ("com.rave", "emergency alerts"),
    ("com.blackboard.connect", "emergency alerts"),
    ("com.microsoft.wdav", "malware protection"),
    ("com.microsoft.defender", "malware protection"),
    ("com.crowdstrike", "malware protection"),
    ("com.sentinelone", "malware protection"),
    ("com.sophos", "malware protection"),
    ("com.mcafee", "malware protection"),
    ("com.symantec", "malware protection"),
    ("com.eset", "malware protection"),
    ("com.google.santa", "malware protection"),
    ("com.jamf", "device management"),
    ("com.jamfsoftware", "device management"),
    ("com.microsoft.intune", "device management"),
    ("com.airwatch", "device management"),
    ("com.vmware.hub", "device management"),
    ("com.apple.managedclient", "device management"),
    ("com.cisco.anyconnect", "network security"),
    ("com.paloaltonetworks", "network security"),
    ("com.zscaler", "network security"),
)


def protected_kind(label):
    """What this agent protects, if anything, else None."""
    lower = label.lower()
    for prefix, kind in PROTECTED_PREFIXES:
        if lower.startswith(prefix):
            return kind
    return None


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
    ("com.ollama", "Ollama — local AI models", "review",
     "Starts Ollama's model server in the background. Fine if you use local "
     "AI; it's holding memory for nothing if you don't."),
    ("com.docker", "Docker helper", "fine",
     "Starts Docker Desktop's background helper."),
    ("io.tailscale", "Tailscale VPN helper", "fine",
     "Keeps you connected to your Tailscale network."),
    ("com.anthropic.claudefordesktop", "Claude desktop updater", "fine",
     "Installs updates for the Claude desktop app."),
    ("app.monitorcontrol", "MonitorControl helper", "fine",
     "Lets MonitorControl change external display brightness."),
    ("nkujuxuj3b.com.nextcloud", "Nextcloud Finder extension", "fine",
     "Shows sync badges on files in Finder."),
    ("com.openssh.ssh-agent", "SSH key agent", "fine",
     "Holds your unlocked SSH keys. Part of macOS's own ssh setup."),
)


def assess_agent(label, command, user_owned):
    lower_label = label.lower()
    lower_cmd = command.lower()
    in_temp = any(p in lower_cmd for p in ("/tmp/", "/private/tmp", "/downloads/"))

    for prefix, name, verdict, expl in AGENT_KNOWLEDGE:
        if lower_label.startswith(prefix):
            # A known name doesn't excuse an unknown location: real vendors
            # don't run their software out of /tmp, so a match there is more
            # likely something wearing the name than the vendor itself.
            if in_temp:
                return (f"{name}, but running from a temporary folder", "suspicious",
                        "Carries a name this tool recognises, yet runs from a "
                        "temporary or downloads folder, which the real thing "
                        "wouldn't. Treat the name as unproven.")
            return (name, verdict, expl)

    # Apple's own agents live in /System and /Library/Apple, neither of which
    # this scans, so an Apple label in the folders it does scan is out of
    # place. Usually installer residue; borrowing the name is also an old
    # malware trick (EvilQuest persisted as com.apple.questd). Must stay
    # above the temp-folder rule only in the sense that it escalates with it:
    # an Apple name is a reason to worry more, never less.
    if lower_label.startswith("com.apple."):
        if in_temp:
            return ("Apple-named task run from a temporary folder", "suspicious",
                    "Uses an Apple label and runs from a temporary or downloads "
                    "folder, where neither Apple nor any other real software "
                    "keeps anything. Turn it off unless you put it there.")
        return ("Apple-named task in a non-Apple folder", "review",
                "Uses an Apple label, but macOS keeps its own agents in /System "
                "and /Library/Apple, which this scan doesn't read. Usually "
                "left behind by an installer. Read the command below before "
                "deciding.")

    if in_temp:
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


def find_restarter(pid, parents, launchd_pids):
    """Walk up from a process to whatever would start it again.

    Killing a process only sticks if nothing is waiting to relaunch it, and
    almost nothing here runs on its own: the AI backends are children of an
    editor, the model server is a child of its app. Returns (kind, name):
    kind is "app" when a running application owns it, "launchd" when a
    background job does, or None when nothing obvious would bring it back.
    """
    seen = set()
    cur = pid
    while cur and cur > 1 and cur not in seen:
        seen.add(cur)
        label = launchd_pids.get(cur)
        if label and cur != pid:
            if label.startswith("application."):
                # Launch Services registration for an open GUI app.
                app = launchd_pids.get(cur, "")
                return "app", _app_name_from(parents.get(cur, ("", ""))[1])
            return "launchd", label
        parent = parents.get(cur)
        if not parent:
            return None, None
        ppid, comm = parent
        if cur != pid and ".app/Contents/MacOS/" in comm:
            return "app", _app_name_from(comm)
        cur = ppid
    return None, None


def _app_name_from(command):
    m = re.search(r"/([^/]+)\.app/", command or "")
    return m.group(1) if m else (command or "").split("/")[-1]


def get_processes():
    proc = run(["ps", "-eo", "pid,ppid,etime,pcpu,rss,command"])
    if proc is None or proc.returncode != 0:
        return []
    exe_paths = get_exe_paths()
    parents = {}
    for line in proc.stdout.splitlines()[1:]:
        f = line.split(None, 5)
        if len(f) == 6 and f[0].isdigit():
            parents[int(f[0])] = (int(f[1]), f[5])
    launchd_pids = {}
    lc = run(["launchctl", "list"])
    if lc is not None and lc.returncode == 0:
        for line in lc.stdout.splitlines()[1:]:
            cols = line.split("\t")
            if len(cols) >= 3 and cols[0].isdigit():
                launchd_pids[int(cols[0])] = cols[2]
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
        restart_kind, restart_name = find_restarter(int(pid), parents, launchd_pids)
        results.append({
            "id": "proc:" + exe,
            "restart_kind": restart_kind,
            "restart_name": restart_name,
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


def get_disabled_labels():
    """Labels switched off in launchd's per-user override database. This is
    the only off-switch available for plists you don't own: renaming those
    needs sudo, while `launchctl disable` is per-user, survives logout, and
    blocks the agent from loading again.

    Returns (labels, ok). ok is False when launchd couldn't be asked at all
    (no GUI session, for instance): callers must not read an empty set as
    "nothing is switched off", or agents that are off would render as on
    with no way to turn them back on.
    """
    uid = os.getuid()
    proc = run(["launchctl", "print-disabled", f"gui/{uid}"])
    if proc is None or proc.returncode != 0:
        return set(), False
    disabled = set()
    for line in proc.stdout.splitlines():
        m = re.match(r'\s*"(.+?)"\s*=>\s*(\S+)', line)
        if m and m.group(2).lower() in ("disabled", "true"):
            disabled.add(m.group(1))
    return disabled, True


def get_background_items(overridden, plist_labels):
    """Background items apps register with launchd through SMAppService.

    These are the ones System Settings calls "Allow in the Background". They
    have no plist in any folder this scans because they live inside the app
    bundle, which makes them invisible here and, since they restart at login,
    the usual reason something you stopped is back the next day. launchctl
    still knows them, and a per-user override still switches them off.
    """
    proc = run(["launchctl", "list"])
    if proc is None or proc.returncode != 0:
        return []
    live = {}
    for line in proc.stdout.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) >= 3:
            live[cols[2]] = cols[0]
    # launchd drops a disabled job from `list` entirely, so enumerating only
    # from there loses the row the moment it's switched off, leaving no way
    # to switch it back on. The override database remembers it.
    candidates = list(live) + [l for l in overridden if l not in live]
    results = []
    for label in candidates:
        pid_s = live.get(label, "-")
        if label in plist_labels or label.startswith("com.apple."):
            continue
        # "application.*" entries are just GUI apps that happen to be open,
        # registered by Launch Services; they aren't background items.
        if label.startswith("application."):
            continue
        friendly, verdict, explanation = assess_agent(label, label, user_owned=True)
        disabled = label in overridden
        results.append({
            "id": "bgitem:" + label,
            "friendly_name": friendly,
            "verdict": verdict,
            "explanation": explanation + " Registered by an app rather than by a "
                           "file, so it starts again at login until it's blocked.",
            "label": label,
            "plist_path": "(registered by an app, no file on disk)",
            "user_owned": False,
            "command": "(inside the app's own bundle)",
            "schedule": "at login, and whenever the app asks for it",
            "disabled": disabled,
            "renamed": False,
            "overridden": disabled,
            "label_from_plist": True,
            "inert": False,
            "protected": protected_kind(label),
            "shared_with": "",
            "loaded": pid_s != "-",
            "pid": int(pid_s) if pid_s.isdigit() else None,
            "ai_related": any(kw in label.lower() for kw in AI_KEYWORDS),
            "app_registered": True,
        })
    return results


def get_launch_agents(override_state=None):
    loaded = get_loaded_labels()
    # Take the caller's reading of the override database when there is one,
    # so the rows and the "could launchd be asked?" flag can't disagree.
    overridden, _ok = override_state if override_state else get_disabled_labels()
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
            renamed = f.name.endswith(DISABLED_SUFFIX)
            try:
                with open(f, "rb") as fh:
                    plist = plistlib.load(fh)
            except Exception:
                continue
            fallback = f.name[:-len(DISABLED_SUFFIX)] if renamed else f.stem
            # Some plists carry no Label (Google's uninstaller blanks its
            # files to {}), so the name is a guess from the filename. That's
            # fine to display, but it must never be handed to launchctl:
            # writing an override for a guessed label invents a service.
            label_from_plist = "Label" in plist
            label = plist.get("Label", fallback)
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
            # Two independent ways an agent can be off, and both can be true
            # at once, so neither can be collapsed into a single "how".
            overridden_off = label_from_plist and label in overridden
            disabled = renamed or overridden_off
            # Report the live load state even for a disabled row. An
            # override is a load-time gate, not an unload: it can be set
            # while the service is still bootstrapped and running, and
            # hiding that made a running agent look stopped.
            info = loaded.get(label)
            user_owned = str(d).startswith(str(Path.home()))
            friendly, verdict, explanation = assess_agent(label, command, user_owned)
            # A plist with no name and no program can't be a job: launchd
            # requires both. These are uninstaller leftovers (Google's blanks
            # its files to an empty dict). They're inert, so the page offers
            # no on/off switch for them, and nothing hands launchd a name
            # that was only ever guessed from the filename.
            inert = not label_from_plist or command == "(no program specified)"
            if inert:
                verdict = "fine"
                explanation = ("An empty leftover file: it names no service and no "
                               "program, so it does nothing at all. Safe to delete, "
                               "safe to ignore.")
            results.append({
                # Directory in the id: the same label can exist in both
                # ~/Library and /Library, and without it the two rows share
                # review marks and actions land on whichever was scanned
                # first. The filename is deliberately not part of the id, so
                # turning an agent off (which renames it .disabled) doesn't
                # orphan its history.
                "id": "agent:" + label + "@" + str(d),
                "friendly_name": friendly,
                "verdict": verdict,
                "explanation": explanation,
                "label": label,
                "plist_path": str(f),
                "user_owned": user_owned,
                "command": command,
                "schedule": describe_schedule(plist),
                "disabled": disabled,
                "renamed": renamed,
                "overridden": overridden_off,
                "label_from_plist": label_from_plist,
                "inert": inert,
                "protected": protected_kind(label),
                "loaded": info is not None,
                "pid": info["pid"] if info else None,
                "ai_related": ai_related,
            })
    # A launchd override is keyed on the label alone, so switching off one
    # row switches off every row sharing that label. Work out who those are
    # now, so the page can say so in the dialog rather than in a toast after
    # the fact.
    by_label = {}
    for r in results:
        by_label.setdefault(r["label"], []).append(r)
    for r in results:
        others = [o for o in by_label[r["label"]] if o["plist_path"] != r["plist_path"]]
        dirs_ = []
        for o in others:
            parent = str(Path(o["plist_path"]).parent)
            if parent not in dirs_:
                dirs_.append(parent)
        r["shared_with"] = " and ".join(dirs_) if (dirs_ and not r["user_owned"]) else ""
        r["app_registered"] = False
    results.extend(get_background_items(overridden, set(by_label)))
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
    override_state = get_disabled_labels()
    launch_agents = get_launch_agents(override_state)
    cron_jobs = get_cron_jobs()
    launchctl_ok = override_state[1]

    all_items = processes + launch_agents + cron_jobs
    reviews, new_ids = record_sightings([it["id"] for it in all_items])
    for group in (processes, launch_agents, cron_jobs):
        attach_review(group, reviews, new_ids)

    # Count from the verdicts attach_review() just resolved, not from a
    # second lookup: the two disagreed once ids changed shape, so the tiles
    # read zero while the rows below them showed badges.
    # Something already switched off isn't asking for a decision any more,
    # so it shouldn't keep inflating the count the README tells people to
    # work down.
    needs_review = sum(
        1 for it in all_items
        if it["review"] is None and not it.get("disabled")
        and it["verdict"] in ("review", "suspicious")
    )
    bogus = sum(1 for it in all_items if it["review"] == "bogus")
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
            # False when launchd's disabled list couldn't be read, in which
            # case rows may claim to be on when they aren't.
            "launchctl_ok": launchctl_ok,
        },
    }


# ---------------------------------------------------------------------------
# Actions — each re-validates its target against a fresh scan first
# ---------------------------------------------------------------------------

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


def _find_agent(item_id):
    """Resolve the exact row the page was showing. Matching on the id rather
    than the label matters when the same label exists in both ~/Library and
    /Library: acting on one row used to rename the other directory's file."""
    if not isinstance(item_id, str) or not item_id.startswith(("agent:", "bgitem:")):
        return None
    agents = [a for a in get_launch_agents() if a["id"] == item_id]
    if not agents:
        return None
    # Both a foo.plist and a foo.plist.disabled can sit in the same folder
    # under one id; prefer the live file. Sorting on "disabled" would tie
    # when an override is in play, so sort on the rename itself.
    agents.sort(key=lambda a: a["renamed"])
    return agents[0]


def _launchctl(verb, label):
    uid = os.getuid()
    proc = run(["launchctl", verb, f"gui/{uid}/{label}"])
    if proc is None:
        return False, "launchctl failed"
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip() or f"launchctl {verb} failed"
    return True, ""


def _bootout(label):
    uid = os.getuid()
    proc = run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    # rc 3 / "No such process" = wasn't loaded, which is fine for our purposes
    ok = proc is not None and proc.returncode in (0, 3)
    if not ok and proc is not None and "No such process" in (proc.stderr or ""):
        ok = True
    return ok, (proc.stderr.strip() if proc else "launchctl failed")


def unload_launch_agent(item_id):
    agent = _find_agent(item_id)
    if not agent:
        return False, "not found in current scan (refusing to act)"
    if agent["inert"]:
        return False, INERT_MSG
    ok, err = _bootout(agent["label"])
    if not ok:
        return False, err
    if agent["disabled"]:
        return True, "stopped"
    return True, "stopped for now; it comes back at your next login"


INERT_MSG = ("this file names no service and no program, so there's nothing "
             "to switch on or off; delete it if you want it gone")


def _same_label_elsewhere(agent):
    """Other scanned rows carrying this label. A launchd override is keyed on
    the label alone, so it covers all of them, and the user needs telling."""
    return [a for a in get_launch_agents()
            if a["label"] == agent["label"] and a["plist_path"] != agent["plist_path"]]


def _name_dirs(agents):
    seen = []
    for a in agents:
        d = str(Path(a["plist_path"]).parent)
        if d not in seen:
            seen.append(d)
    return " and ".join(seen)


def disable_launch_agent(item_id):
    """Turn an agent off for good, not just until the next login. Renames the
    plist when it's yours; writes a launchd override when it isn't."""
    agent = _find_agent(item_id)
    if not agent:
        return False, "not found in current scan (refusing to act)"
    if agent["inert"]:
        return False, INERT_MSG
    if agent["disabled"] and not agent["loaded"]:
        return True, "already turned off"

    if not agent["user_owned"]:
        # Renaming a root-owned plist needs sudo; a launchd override is
        # per-user, needs none, and unlike bootout it survives logout.
        # Write the block before unloading: if the unload then fails, the
        # agent is at least off from the next login rather than fully live.
        ok, err = _launchctl("disable", agent["label"])
        if not ok:
            return False, f"couldn't turn it off: {err}"
        remember_load_state(item_id, agent["loaded"])
        booted, boot_err = _bootout(agent["label"])
        shared = _same_label_elsewhere(agent)
        extra = (f"; this covers the copy in {_name_dirs(shared)} too" if shared else "")
        if not booted:
            return True, f"blocked from starting again, but it's still running ({boot_err}){extra}"
        return True, f"turned off for your account (stays off after restart){extra}"

    ok, err = _bootout(agent["label"])
    if not ok:
        return False, err
    src = Path(agent["plist_path"])
    if src.name.endswith(DISABLED_SUFFIX):
        remember_load_state(item_id, agent["loaded"])
        return True, "turned off"
    try:
        src.rename(src.with_name(src.name + ".disabled"))
    except OSError as e:
        return False, f"unloaded, but rename failed: {e}"
    remember_load_state(item_id, agent["loaded"])
    return True, "turned off (unloaded and plist renamed)"


def enable_launch_agent(item_id):
    agent = _find_agent(item_id)
    if not agent:
        return False, "not found in current scan (refusing to act)"
    if agent["inert"]:
        return False, INERT_MSG
    if not agent["disabled"]:
        return True, "already on"

    # Undo every mechanism that's in play, not just the one recorded: an
    # agent can be renamed AND overridden, and launchd refuses to bootstrap
    # a label whose override is still set. A rename-only undo doesn't need
    # launchd's list at all, so don't fail the whole action when it's the
    # file that has to move.
    also = ""
    if agent["overridden"] or not agent["renamed"]:
        overridden_now, lookup_ok = get_disabled_labels()
        if not lookup_ok:
            return False, ("macOS didn't answer when asked which items are blocked, "
                           "so nothing was changed")
        if agent["label"] in overridden_now:
            ok, err = _launchctl("enable", agent["label"])
            if not ok:
                return False, err
            # Only an override is label-wide; a rename affects one file.
            shared = _same_label_elsewhere(agent)
            if shared:
                also = f"; the copy in {_name_dirs(shared)} shares this name and is on again too"

    # An app-registered item has no file to restore or bootstrap; clearing
    # the block is the whole job, and the app puts it back itself.
    if agent.get("app_registered"):
        return True, "turned back on; the app will start it again at login"

    path = Path(agent["plist_path"])
    if path.name.endswith(DISABLED_SUFFIX):
        if not agent["user_owned"]:
            return False, "unblocked, but renaming this plist back needs sudo"
        dst = path.with_name(path.name[:-len(".disabled")])
        try:
            path.rename(dst)
        except OSError as e:
            return False, f"rename failed: {e}"
        path = dst

    # Only start it now if it was running when it was turned off. Otherwise
    # bootstrap would launch something that had merely been sitting there,
    # which is not what "turn back on" promised.
    if not recall_load_state(item_id, default=True):
        return True, f"turned back on; it will start at login as before{also}"

    uid = os.getuid()
    proc = run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
    if proc is None or proc.returncode != 0:
        msg = proc.stderr.strip() if proc else "launchctl failed"
        return True, f"turned back on, but it didn't start now ({msg}){also}"
    return True, f"turned back on and running{also}"


def delete_launch_agent(item_id):
    agent = _find_agent(item_id)
    if not agent:
        return False, "not found in current scan (refusing to act)"
    if agent.get("app_registered"):
        return False, ("this one lives inside an app, so there's no file to delete; "
                       "Turn off blocks it instead")
    if not agent["user_owned"]:
        return False, "refusing to delete a system-owned plist (needs sudo)"
    if not agent["inert"]:
        ok, err = _bootout(agent["label"])
        if not ok:
            return False, err
        # Clear any override too. Left behind, it silently blocks the same
        # label if the app is reinstalled, with nothing on screen to say why.
        overridden_now, lookup_ok = get_disabled_labels()
        if lookup_ok and agent["label"] in overridden_now and not _same_label_elsewhere(agent):
            _launchctl("enable", agent["label"])
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
            ok, msg = unload_launch_agent(data.get("id"))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/launchagent/disable":
            ok, msg = disable_launch_agent(data.get("id"))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/launchagent/enable":
            ok, msg = enable_launch_agent(data.get("id"))
            self._send_json({"ok": ok, "message": msg})
            return

        if parsed.path == "/api/launchagent/delete":
            ok, msg = delete_launch_agent(data.get("id"))
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
