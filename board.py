#!/usr/bin/env python3
"""Sprint Focus Board: a local board that shows one person's slice of a Jira Cloud active sprint.

Run:   python board.py [--config PATH] [--port 8765] [--open]
Open:  http://localhost:8765

Credentials come from environment variables, or from a .env file next to this script:
  JIRA_BASE_URL   https://your-site.atlassian.net
  JIRA_EMAIL      the email of the Atlassian account
  JIRA_API_TOKEN  an API token for that account
They stay in this process; the page never sees them. See README.md and config.example.jsonc.
"""
import argparse
import base64
import concurrent.futures
import copy
import datetime
import fnmatch
import json
import os
import hmac
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, "board.html")
CACHE_SECONDS = 20
JIRA_CONNECTIONS = 8
UTC = datetime.timezone.utc
RULE_TYPES = ("unassigned", "assignee", "involved", "role", "any")
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Defaults. A config file only needs the keys it changes; see config.example.jsonc.

PROMPTS = {
    "todo": {
        "en": "Pick up {key}: {summary}\n{url}\n\nRead the ticket and its acceptance criteria, then plan the change, implement it with tests "
              "and open a pull request. Show me the plan before you start, and a draft before you post a comment or change the ticket status.",
        "tr": "{key} işini al: {summary}\n{url}\n\nTicket'ı ve kabul kriterlerini oku; değişikliği planla, testleriyle uygula ve pull request aç. "
              "Başlamadan önce planı, yorum yazmadan ya da ticket durumunu değiştirmeden önce taslağı göster.",
    },
    "reopened": {
        "en": "{key} was reopened: {summary}\n{url}\n\nRead the latest comments to find out why it was reopened and what is missing or wrong. "
              "Fix it, then update the pull request. Show me a draft before you post a comment or change the ticket status.",
        "tr": "{key} yeniden açıldı: {summary}\n{url}\n\nNeden yeniden açıldığını, neyin eksik ya da yanlış olduğunu son yorumlardan çıkar. "
              "Düzelt ve pull request'i güncelle. Yorum yazmadan ya da ticket durumunu değiştirmeden önce taslağı göster.",
    },
    "progress": {
        "en": "Continue {key}: {summary}\n{url}\n\nWork out where it stands: branches, pull request, CI and reviews. Then finish what is missing. "
              "Show me a draft before you post a comment or change the ticket status.",
        "tr": "{key} işine devam et: {summary}\n{url}\n\nDurumu çıkar: branch'ler, pull request, CI ve review'lar. Sonra eksikleri tamamla. "
              "Yorum yazmadan ya da ticket durumunu değiştirmeden önce taslağı göster.",
    },
    "blocked": {
        "en": "{key} is blocked: {summary}\n{url}\n\nRead the blocking links and the latest comments, and check whether the blocker is resolved. "
              "If it is, continue the work. If not, summarise who we are waiting for and what is needed.",
        "tr": "{key} bloke: {summary}\n{url}\n\nBloke eden bağlantıları ve son yorumları oku, engel çözüldü mü kontrol et. "
              "Çözüldüyse işe devam et. Çözülmediyse kimi beklediğimizi ve ne gerektiğini özetle.",
    },
    "blockedReviewer": {
        "en": "{key} is blocked and I am one of its reviewers: {summary}\n{url}\n\nRead the blocking links and the latest comments, and check "
              "whether the blocker is resolved. Summarise whether it affects the review order and whether anything is expected from me. "
              "Read only: do not change code.",
        "tr": "{key} bloke ve ben reviewer'larından biriyim: {summary}\n{url}\n\nBloke eden bağlantıları ve son yorumları oku, engel çözüldü mü "
              "kontrol et. Review sırasını etkiliyor mu, benden bir şey bekleniyor mu, özetle. Sadece oku: koda dokunma.",
    },
    "toreview": {
        "en": "Review {key}: {summary}\n{url}\n\nRead the ticket and its acceptance criteria, then review the pull request: correctness, tests, "
              "CI status and open threads. Read only: do not change code. Summarise the findings and show me a draft before you comment anywhere.",
        "tr": "{key} işini review et: {summary}\n{url}\n\nTicket'ı ve kabul kriterlerini oku, sonra pull request'i incele: doğruluk, testler, "
              "CI durumu ve açık thread'ler. Sadece oku: koda dokunma. Bulguları özetle; bir yere yorum yazmadan önce taslağı göster.",
    },
    "inreview": {
        "en": "Continue reviewing {key}: {summary}\n{url}\n\nI am already its reviewer. Check what changed since the last review, the CI status "
              "and open threads. Read only: do not change code. Summarise the findings and show me a draft before you comment anywhere.",
        "tr": "{key} review'una devam et: {summary}\n{url}\n\nReviewer zaten benim. Son review'dan beri neyin değiştiğine, CI durumuna ve açık "
              "thread'lere bak. Sadece oku: koda dokunma. Bulguları özetle; bir yere yorum yazmadan önce taslağı göster.",
    },
    "merge": {
        "en": "Final review of {key} before merge: {summary}\n{url}\n\nCheck the logic against the acceptance criteria and look for gaps. Confirm "
              "that CI is green and no review threads are open. Do not merge; report the outcome and show me a draft before you comment anywhere.",
        "tr": "{key} için merge öncesi son review: {summary}\n{url}\n\nMantığı kabul kriterlerine göre kontrol et, boşlukları ara. CI yeşil mi, "
              "açık review thread'i var mı doğrula. Merge etme; sonucu bildir ve bir yere yorum yazmadan önce taslağı göster.",
    },
    "generic": {
        "en": "Look at {key}: {summary}\n{url}\n\nIt is in {status}. Work out what it needs from me next and summarise that before you change anything.",
        "tr": "{key} işine bak: {summary}\n{url}\n\nDurumu: {status}. Benden sıradaki beklentiyi çıkar ve bir şey değiştirmeden önce özetle.",
    },
}

DEFAULT_COLUMNS = [
    {"id": "todo", "name": "To Do", "color": "#8A93A6", "rule": {"type": "unassigned", "orAssignee": True},
     "types": ["Sub-task", "Bug", "Spike"]},
    {"id": "reopened", "name": "Reopened", "color": "#E5484D", "rule": {"type": "assignee"}},
    {"id": "progress", "name": "In Progress", "color": "#2B8CFF", "rule": {"type": "assignee"}},
    {"id": "blocked", "name": "Blocked", "color": "#D9822B", "rule": {"type": "involved"}},
    {"id": "toreview", "name": "To Review", "color": "#7A5AF8", "excludeTypes": ["Story"],
     "rule": {"type": "role", "role": "approver", "notAssignee": True}},
    {"id": "inreview", "name": "In Review", "color": "#5B6BFF", "excludeTypes": ["Story"],
     "rule": {"type": "role", "role": "approver"}},
    {"id": "merge", "name": "For Merge Approval", "color": "#12A594", "excludeTypes": ["Story"],
     "rule": {"type": "role", "role": "mergeApprover", "notAssignee": True, "emptyExcludes": ["approver"]}},
]

DEFAULT_STAGES = [
    {"id": "dev", "label": "Dev", "from": "In Progress", "to": "To Review", "by": "assignee"},
    {"id": "rev", "label": "Rev", "from": "In Review", "to": "For Merge Approval", "by": "others"},
    {"id": "mr", "label": "MR", "from": "For Merge Approval", "to": "Done", "by": "others"},
]

DEFAULTS = {
    "jira": {"baseUrl": "", "email": ""},
    "board": {"id": None, "sprintNamePrefix": "", "ignoreStatusSuffixes": ["(migrated)"]},
    "server": {"port": 8765},
    "ui": {"defaultLanguage": "en", "refreshSeconds": 60},
    "reviewerRoles": [],
    "columns": DEFAULT_COLUMNS,
    "channels": [],
    "priorities": {"highest": 1, "blocker": 1, "critical": 1, "high": 2, "major": 2, "medium": 3,
                   "low": 4, "minor": 4, "lowest": 5, "trivial": 5},
    "burndown": {
        "enabled": True,
        "timezone": "local",
        "timezoneFallback": None,
        "dailyTargetHours": 6,
        "thresholds": {"low": 2, "mid": 4},
        "weekendDays": [6, 7],
        "excludeTypes": ["Story", "Epic"],
        "doneStatuses": ["Done"],
        "estimateComment": {"enabled": False, "stages": DEFAULT_STAGES},
    },
}


# ---------------------------------------------------------------------------------------------
# Config files: JSON with // and /* */ comments.

def strip_comments(text):
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def read_config(path=None):
    """Return (merged config, path used). An explicit path that does not exist is an error."""
    explicit = path or os.environ.get("BOARD_CONFIG")
    candidates = [explicit] if explicit else [os.path.join(HERE, "config.jsonc"), os.path.join(HERE, "config.json")]
    for p in candidates:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8-sig") as fh:
                    user = json.loads(strip_comments(fh.read()))
            except json.JSONDecodeError as ex:
                raise ConfigError(f"{p} is not valid JSON (line {ex.lineno}, column {ex.colno}): {ex.msg}")
            if not isinstance(user, dict):
                raise ConfigError(f"{p} must hold a JSON object")
            if "apiToken" in (user.get("jira") or {}):
                raise ConfigError("Do not put the API token in the config file. Set JIRA_API_TOKEN in the environment or in .env.")
            return deep_merge(DEFAULTS, user), p
    if explicit:
        raise ConfigError(f"Config file not found: {explicit}")
    return copy.deepcopy(DEFAULTS), None


# The setup wizard in the page saves here. It is layered over the config file, so a hand-written
# config keeps its comments, and it only holds the keys the wizard asks about.
SETTINGS_KEYS = {"board": ("id", "sprintNamePrefix"), "ui": ("refreshSeconds",), "team": None, "channels": None, "prompts": None}


def settings_path():
    return os.environ.get("BOARD_SETTINGS") or os.path.join(HERE, "settings.json")


def read_settings(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as ex:
        raise ConfigError(f"{path} is not valid JSON (line {ex.lineno}): fix it, or delete it to run the setup again.")
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a JSON object")
    return pick_settings(data)


def pick_settings(data):
    out = {}
    for key, sub in SETTINGS_KEYS.items():
        if key in data:
            out[key] = {k: data[key][k] for k in sub if k in data[key]} if sub and isinstance(data[key], dict) else data[key]
    return out


def write_settings(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_env_file(path):
    """Read KEY=VALUE lines into os.environ without overriding variables that are already set."""
    if not path or not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)
    return True


# ---------------------------------------------------------------------------------------------
# Time zones. zoneinfo needs a tz database; Windows has none unless the tzdata package is installed.

def parse_offset(text):
    m = re.fullmatch(r"(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?", (text or "").strip(), re.I)
    if not m:
        return None
    delta = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    return -delta if m.group(1) == "-" else delta


def _sunday(year, month, nth):
    """nth Sunday of a month (1-based), or the last one when nth is -1."""
    if nth == -1:
        d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1) if month < 12 else datetime.date(year, 12, 31)
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=nth - 1)


def _at(day, hour, minus):
    return datetime.datetime(day.year, day.month, day.day, hour, tzinfo=UTC) - minus


def rule_clock(std, dst):
    """A fixed standard offset plus an optional EU or US daylight-saving rule."""
    hour = datetime.timedelta(hours=1)

    def in_dst(utc):
        y = utc.year
        if dst == "eu":  # 01:00 UTC, last Sunday of March to last Sunday of October
            return _at(_sunday(y, 3, -1), 1, datetime.timedelta(0)) <= utc < _at(_sunday(y, 10, -1), 1, datetime.timedelta(0))
        if dst == "us":  # 02:00 local, second Sunday of March to first Sunday of November
            return _at(_sunday(y, 3, 2), 2, std) <= utc < _at(_sunday(y, 11, 1), 2, std + hour)
        return False

    def to_local(utc):
        off = std + (hour if in_dst(utc) else datetime.timedelta(0))
        return utc.astimezone(datetime.timezone(off))
    return to_local


def make_clock(name, fallback=None):
    """Return a function that turns an aware UTC datetime into the configured local time."""
    name = (name or "local").strip()
    if name.lower() == "local":
        return lambda utc: utc.astimezone()
    if name.upper() in ("UTC", "GMT", "Z"):
        return lambda utc: utc.astimezone(UTC)
    off = parse_offset(name)
    if off is not None:
        tz = datetime.timezone(off)
        return lambda utc: utc.astimezone(tz)
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(name)
        return lambda utc: utc.astimezone(tz)
    except Exception:
        pass
    if fallback:
        std = parse_offset(str(fallback.get("utcOffset", "")))
        dst = str(fallback.get("dst", "none")).lower()
        if std is None or dst not in ("eu", "us", "none"):
            raise ConfigError('burndown.timezoneFallback needs "utcOffset" like "+01:00" and "dst" of "eu", "us" or "none"')
        print(f"Time zone {name!r} is not installed; using the fallback {fallback.get('utcOffset')} with the {dst} daylight-saving rule.",
              file=sys.stderr)
        return rule_clock(std, dst)
    raise ConfigError(
        f"Time zone {name!r} is not installed on this machine (Windows has no time zone database by default). "
        "Run `python -m pip install tzdata`, or set burndown.timezoneFallback (see config.example.jsonc), "
        'or use "local" or a fixed offset such as "+01:00".')


def parse_ts(ts):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.datetime.strptime(ts, fmt)
        except ValueError:
            pass
    raise ValueError(f"unexpected timestamp {ts!r}")


# ---------------------------------------------------------------------------------------------
# Runtime state, filled by configure() and connect().

CFG = copy.deepcopy(DEFAULTS)
BASE_CFG = copy.deepcopy(DEFAULTS)  # the config file without the wizard's settings, for saving
SETUP_REQUIRED = False
SETUP_TOKEN = secrets.token_urlsafe(24)
TEAM = []
PROMPT_VARS = []
BASE = ""
AUTH = ""
ROLES = []
COLUMNS = []
CHANNELS = []
PRIORITIES = {}
SUFFIXES = []
BURN = {}
CLOCK = make_clock("UTC")
FIELDS = ""
PARENT_FIELDS = "summary,parent,labels,components"

_me = {}
_icons = {}
_issue_cache = {}
_lock = threading.Lock()
_jira_slots = threading.BoundedSemaphore(JIRA_CONNECTIONS)
_flights = {}


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip().lower()


def norm_type(s):
    # "Sub-task", "Subtask" and "sub task" are the same issue type.
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def clean_status(s):
    s = (s or "").strip()
    for suffix in SUFFIXES:
        if s.lower().endswith(suffix):
            s = s[: -len(suffix)].strip()
    return s


def localized(v, what):
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, dict) and v and all(isinstance(x, str) for x in v.values()):
        return v
    raise ConfigError(f"{what} must be a string or an object of language: text")


def str_list(v, what):
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ConfigError(f"{what} must be a list of strings")
    return v


DEFAULT_CHANNEL_COLOR = "#7FA0FF"


def compile_channels(items, warnings=None):
    """Validate channel definitions and compile their matchers."""
    channels, seen = [], {"other"}
    if not isinstance(items, list):
        raise ConfigError("channels must be a list")
    for i, ch in enumerate(items):
        where = f"channels[{i}]"
        if not isinstance(ch, dict) or not ch.get("id") or not ID_RE.match(str(ch["id"])) or ch["id"] in seen:
            raise ConfigError(f'{where} needs a unique id made of letters, digits, _ or - (and not "other")')
        seen.add(ch["id"])
        words = [w.strip() for w in str_list(ch.get("keywords"), f"{where}.keywords") if w.strip()]
        patterns = [ch["summary"]] if ch.get("summary") else []
        if words:
            # Keywords are whole words or phrases, so "pay" does not match "payroll".
            patterns.append(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)")
        try:
            summary = re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else None
        except re.error as ex:
            raise ConfigError(f"{where}.summary is not a valid regular expression: {ex}")
        channels.append({
            "id": ch["id"],
            "name": ch.get("name") or ch["id"],
            "color": ch.get("color") or DEFAULT_CHANNEL_COLOR,
            "epics": set(str_list(ch.get("epics"), f"{where}.epics")),
            "labels": [p.lower() for p in str_list(ch.get("labels"), f"{where}.labels")],
            "components": [p.lower() for p in str_list(ch.get("components"), f"{where}.components")],
            "summary": summary,
        })
        if warnings is not None and not (channels[-1]["epics"] or channels[-1]["labels"] or channels[-1]["components"] or summary):
            warnings.append(f"{where} has no epics, labels, components, keywords or summary, so no issue will match it.")
    return channels


PLACEHOLDERS = ("key", "summary", "url", "type", "status", "parentKey", "parentSummary", "assignee")


def compile_prompt_vars(spec):
    """Extra prompt placeholders whose value depends on the ticket: {"name": [{"keys": [...], "types": [...], "value": ...}]}.
    The first rule whose key patterns and issue types match gives the value; a rule without either is the fallback."""
    if not isinstance(spec, dict):
        raise ConfigError("promptVars must be an object of name: [rules]")
    out = []
    for name, rules in spec.items():
        where = f"promptVars.{name}"
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name) or name in PLACEHOLDERS:
            raise ConfigError(f"{where}: use letters, digits and _, and not a built-in placeholder ({', '.join(PLACEHOLDERS)})")
        compiled = []
        for i, r in enumerate(rules if isinstance(rules, list) else [rules]):
            if not isinstance(r, dict) or "value" not in r:
                raise ConfigError(f"{where}[{i}] needs a value")
            compiled.append(([k.lower() for k in str_list(r.get("keys"), f"{where}[{i}].keys")],
                             {norm_type(t) for t in str_list(r.get("types"), f"{where}[{i}].types")},
                             localized(r["value"], f"{where}[{i}].value") or ""))
        out.append((name, compiled))
    return out


def prompt_vars_for(key, type_name):
    out, t = {}, norm_type(type_name)
    for name, rules in PROMPT_VARS:
        for keys, types, value in rules:
            if (not keys or glob_any(key, keys)) and (not types or t in types):
                out[name] = value
                break
    return out


def load_mcp_credentials(path):
    """Fill JIRA_* from the Atlassian entry of an MCP config file (such as .mcp.json), without overriding what is set,
    so the token keeps living in one place."""
    path = os.path.expanduser(os.path.expandvars(path))
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except OSError as ex:
        raise ConfigError(f"jira.mcpConfig: cannot read {path} ({ex.strerror or ex})")
    except json.JSONDecodeError as ex:
        raise ConfigError(f"jira.mcpConfig: {path} is not valid JSON (line {ex.lineno})")
    servers = (data.get("mcpServers") or {}).values() if isinstance(data, dict) else []
    env = next((s["env"] for s in servers if isinstance(s, dict) and isinstance(s.get("env"), dict) and s["env"].get("JIRA_API_TOKEN")), None)
    if env is None:
        raise ConfigError(f"jira.mcpConfig: no server in {path} has JIRA_API_TOKEN in its env")
    for ours, theirs in (("JIRA_BASE_URL", "JIRA_URL"), ("JIRA_EMAIL", "JIRA_USERNAME"), ("JIRA_API_TOKEN", "JIRA_API_TOKEN")):
        if env.get(theirs):
            os.environ.setdefault(ours, str(env[theirs]))


def column_prompts(c, rule_type, over, where):
    """A column's prompt and promptIfNotAssignee: its own (else the built-in one), then a "prompts" override."""
    prompt = localized(c.get("prompt"), f"{where}.prompt") or PROMPTS.get(c["id"]) or PROMPTS["generic"]
    other = localized(c.get("promptIfNotAssignee"), f"{where}.promptIfNotAssignee")
    if other is None and rule_type == "involved" and "prompt" not in c:
        other = PROMPTS["blockedReviewer"]
    if not isinstance(over, dict):
        raise ConfigError(f"prompts.{c['id']} must be an object with prompt and promptIfNotAssignee")
    return (localized(over.get("prompt"), f"prompts.{c['id']}.prompt") or prompt,
            localized(over.get("promptIfNotAssignee"), f"prompts.{c['id']}.promptIfNotAssignee") or other)


def configure(cfg, base=None):
    """Validate the merged config and set the module state. Returns a list of warnings.
    base is the config file without the wizard's settings; its prompts are what the wizard's Reset goes back to."""
    global CFG, ROLES, COLUMNS, CHANNELS, PRIORITIES, SUFFIXES, BURN, CLOCK, FIELDS, TEAM, SETUP_REQUIRED, PROMPT_VARS
    warnings = []
    board = cfg["board"]
    # Without a board the server still starts; the page then runs the setup wizard.
    setup_required = board.get("id") in (None, "")
    if not setup_required:
        try:
            board["id"] = int(board["id"])
        except (TypeError, ValueError):
            raise ConfigError("board.id must be a number")

    team = []
    for i, p in enumerate(cfg.get("team") or []):
        if isinstance(p, str):
            p = {"id": p}
        if not isinstance(p, dict) or not p.get("id"):
            raise ConfigError(f"team[{i}] needs an id (a Jira accountId)")
        team.append({"id": str(p["id"]), "name": str(p.get("name") or ""), "avatar": str(p.get("avatar") or "")})
    suffixes = [norm(s) for s in str_list(board.get("ignoreStatusSuffixes"), "board.ignoreStatusSuffixes") if norm(s)]

    roles, seen = [], set()
    for i, r in enumerate(cfg.get("reviewerRoles") or []):
        if not isinstance(r, dict) or not r.get("id") or not r.get("field"):
            raise ConfigError(f"reviewerRoles[{i}] needs an id and a field (for example customfield_10001)")
        if not ID_RE.match(str(r["id"])) or r["id"] in seen:
            raise ConfigError(f"reviewerRoles[{i}].id {r['id']!r} must be unique and use letters, digits, _ or -")
        seen.add(r["id"])
        roles.append({"id": r["id"], "field": str(r["field"]), "label": localized(r.get("label"), f"reviewerRoles[{i}].label") or r["id"]})
    role_index = {r["id"]: i for i, r in enumerate(roles)}

    def check_role(rid, where):
        if rid not in role_index and roles:
            warnings.append(f"{where} refers to reviewer role {rid!r}, which is not in reviewerRoles; it counts as always empty.")

    columns, seen = [], set()
    if not isinstance(cfg.get("columns"), list) or not cfg["columns"]:
        raise ConfigError("columns must be a non-empty list")
    prompts = cfg.get("prompts") or {}
    base_prompts = (base if base is not None else cfg).get("prompts") or {}
    if not isinstance(prompts, dict) or not isinstance(base_prompts, dict):
        raise ConfigError("prompts must be an object of column id: {prompt, promptIfNotAssignee}")
    for i, c in enumerate(cfg["columns"]):
        where = f"columns[{i}]"
        if not isinstance(c, dict) or not c.get("id") or not ID_RE.match(str(c["id"])) or c["id"] in seen:
            raise ConfigError(f"{where} needs a unique id made of letters, digits, _ or -")
        seen.add(c["id"])
        rule = c.get("rule") or {}
        if isinstance(rule, str):
            rule = {"type": rule}
        if rule.get("type") not in RULE_TYPES:
            raise ConfigError(f"{where}.rule.type must be one of {', '.join(RULE_TYPES)}")
        rule = {
            "type": rule["type"],
            "role": rule.get("role"),
            "roles": str_list(rule.get("roles"), f"{where}.rule.roles") if rule.get("roles") is not None else None,
            "notAssignee": bool(rule.get("notAssignee")),
            "orAssignee": bool(rule.get("orAssignee")),
            "emptyExcludes": str_list(rule.get("emptyExcludes"), f"{where}.rule.emptyExcludes"),
            "whenEmpty": rule.get("whenEmpty", "anyone"),
        }
        if rule["whenEmpty"] not in ("anyone", "nobody"):
            raise ConfigError(f'{where}.rule.whenEmpty must be "anyone" or "nobody"')
        if rule["type"] == "role":
            if not rule["role"]:
                raise ConfigError(f"{where}.rule needs a role (a reviewerRoles id)")
            check_role(rule["role"], where)
        for rid in rule["emptyExcludes"] + (rule["roles"] or []):
            check_role(rid, where)
        stage = c.get("stage")
        if stage is None:
            stage = role_index[rule["role"]] + 1 if rule["type"] == "role" and rule["role"] in role_index else (
                len(roles) + 1 if rule["type"] == "role" else 0)
        elif stage == "assignee":
            stage = 0
        elif isinstance(stage, str):
            if stage not in role_index:
                raise ConfigError(f"{where}.stage must be \"assignee\", a reviewerRoles id or a number")
            stage = role_index[stage] + 1
        name = c.get("name") or c["id"]
        prompt, other = column_prompts(c, rule["type"], prompts.get(c["id"]) or {}, where)
        default_prompt, default_other = column_prompts(c, rule["type"], base_prompts.get(c["id"]) or {}, where)
        columns.append({
            "defaultPrompt": default_prompt,
            "defaultPromptIfNotAssignee": default_other,
            "id": c["id"],
            "name": name,
            "color": c.get("color") or "#8A93A6",
            "statuses": {norm(s) for s in (str_list(c.get("statuses"), f"{where}.statuses") or [name])},
            "types": {norm_type(t) for t in str_list(c.get("types"), f"{where}.types")},
            "typeNames": str_list(c.get("types"), f"{where}.types"),
            "excludeTypes": {norm_type(t) for t in str_list(c.get("excludeTypes"), f"{where}.excludeTypes")},
            "rule": rule,
            "channels": bool(c.get("channels", rule["type"] in ("unassigned", "role", "any"))),
            "stage": int(stage),
            "description": localized(c.get("description"), f"{where}.description"),
            "prompt": prompt,
            "promptIfNotAssignee": other,
        })

    for cid in set(prompts) - {c["id"] for c in columns}:
        warnings.append(f"prompts.{cid} is for a column that does not exist; it is ignored.")
    prompt_vars = compile_prompt_vars(cfg.get("promptVars") or {})
    channels = compile_channels(cfg.get("channels") or [], warnings)

    priorities = {}
    for k, v in (cfg.get("priorities") or {}).items():
        if v is not None:
            priorities[norm(k)] = int(v)

    b = cfg["burndown"]
    est = b.get("estimateComment") or {}
    stages = []
    if b.get("enabled") and est.get("enabled"):
        for i, s in enumerate(est.get("stages") or []):
            where = f"burndown.estimateComment.stages[{i}]"
            if not all(s.get(k) for k in ("id", "label", "from", "to")):
                raise ConfigError(f"{where} needs id, label, from and to")
            if s.get("by", "assignee") not in ("assignee", "others"):
                raise ConfigError(f'{where}.by must be "assignee" or "others"')
            stages.append({"id": s["id"], "label": s["label"], "from": norm(s["from"]), "to": norm(s["to"]), "by": s.get("by", "assignee")})
        if not stages:
            raise ConfigError("burndown.estimateComment is enabled but has no stages")
    thresholds = b.get("thresholds") or {}
    burn = {
        "enabled": bool(b.get("enabled")),
        "target": float(b.get("dailyTargetHours") or 0),
        "low": float(thresholds.get("low", 2)),
        "mid": float(thresholds.get("mid", 4)),
        "weekendDays": [int(d) for d in b.get("weekendDays") or []],
        "excludeTypes": {norm_type(t) for t in str_list(b.get("excludeTypes"), "burndown.excludeTypes")},
        "done": {norm(s) for s in str_list(b.get("doneStatuses"), "burndown.doneStatuses")},
        "stages": stages,
    }
    if stages:
        dur = r"((?:\d+(?:\.\d+)?\s*[hm]\s*)+)"
        burn["full"] = re.compile(r"\|\s*".join(re.escape(s["label"]) + r":\s*" + dur for s in stages), re.I)
        burn["part"] = re.compile(r"(?<!\w)(" + "|".join(re.escape(s["label"]) for s in stages) + r"):\s*" + dur, re.I)
        burn["byLabel"] = {s["label"].lower(): s["id"] for s in stages}
        burn["moves"] = {(s["from"], s["to"]): s for s in stages}
    clock = make_clock(b.get("timezone"), b.get("timezoneFallback")) if burn["enabled"] else make_clock("UTC")

    lang = cfg["ui"].get("defaultLanguage", "en")
    if lang not in ("en", "tr"):
        raise ConfigError('ui.defaultLanguage must be "en" or "tr"')

    CFG, ROLES, COLUMNS, CHANNELS, PRIORITIES, SUFFIXES, BURN, CLOCK = cfg, roles, columns, channels, priorities, suffixes, burn, clock
    TEAM, SETUP_REQUIRED, PROMPT_VARS = team, setup_required, prompt_vars
    FIELDS =",".join(["summary", "status", "priority", "issuetype", "parent", "assignee", "labels", "components", "issuelinks"] + [r["field"] for r in roles])
    return warnings


def connect(cfg):
    """Read the Jira site and credentials from the environment (or the config, for the non-secret parts)."""
    global BASE, AUTH
    base = os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or cfg["jira"].get("baseUrl") or ""
    email = os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USERNAME") or cfg["jira"].get("email") or ""
    token = os.environ.get("JIRA_API_TOKEN") or ""
    missing = [n for n, v in (("JIRA_BASE_URL", base), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token)) if not v]
    if missing:
        raise ConfigError("Missing " + ", ".join(missing) + ". Set them in the environment or in a .env file next to board.py (see .env.example), "
                          "or point jira.mcpConfig at an MCP config file that has them.")
    base = base.strip().rstrip("/")
    # Plain http is accepted only on loopback, for a local test double such as demo/demo.py.
    if not re.match(r"^(https://[^/\s]+|http://(localhost|127\.0\.0\.1)(:\d+)?)$", base):
        raise ConfigError(f"JIRA_BASE_URL must look like https://your-site.atlassian.net, not {base!r}")
    BASE = base
    AUTH = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()


# ---------------------------------------------------------------------------------------------
# Jira access

def describe(ex):
    if isinstance(ex, urllib.error.HTTPError):
        detail = ""
        try:
            body = json.loads(ex.read() or b"{}")
            detail = "; ".join(body.get("errorMessages") or []) or "; ".join(f"{k}: {v}" for k, v in (body.get("errors") or {}).items())
        except Exception:
            pass
        hint = {401: "check JIRA_EMAIL and JIRA_API_TOKEN",
                403: "this account cannot see the board or project",
                404: "check JIRA_BASE_URL and board.id"}.get(ex.code, "")
        return f"Jira returned HTTP {ex.code}" + (f": {detail}" if detail else "") + (f" ({hint})" if hint else "")
    if isinstance(ex, urllib.error.URLError):
        return f"Cannot reach Jira: {ex.reason}"
    return str(ex) or ex.__class__.__name__


def fetch(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"Authorization": AUTH, "Accept": accept, "User-Agent": "sprint-focus-board"})
    # Parallel page loads share one small pool of Jira connections; more than that makes Jira drop requests.
    with _jira_slots:
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read(), resp.headers.get("Content-Type", "")
            except urllib.error.HTTPError as ex:
                if attempt == 2 or not (ex.code >= 500 or ex.code == 429):
                    raise
                if ex.code == 429:
                    try:
                        wait = float(ex.headers.get("Retry-After") or 2)
                    except ValueError:
                        wait = 2
                    time.sleep(min(wait, 10))
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == 2:
                    raise


def get(url):
    return json.loads(fetch(url)[0])


def single_flight(key):
    # Concurrent callers for the same key wait for the first one instead of repeating its Jira calls.
    with _lock:
        return _flights.setdefault(key, threading.Lock())


def search(jql, fields):
    out, token = [], None
    while True:
        params = {"jql": jql, "fields": fields, "maxResults": 100}
        if token:
            params["nextPageToken"] = token
        d = get(BASE + "/rest/api/3/search/jql?" + urllib.parse.urlencode(params))
        out += d["issues"]
        token = d.get("nextPageToken")
        if not token or d.get("isLast"):
            return out


def paged(url, key):
    out, start = [], 0
    while True:
        d = get(f"{url}{'&' if '?' in url else '?'}startAt={start}&maxResults=100")
        vals = d.get(key) or []
        out += vals
        start += len(vals)
        if not vals or start >= d.get("total", 0):
            return out


def person(u):
    if not isinstance(u, dict) or not u.get("accountId"):
        return None
    return {"id": u["accountId"], "name": u.get("displayName", ""), "avatar": (u.get("avatarUrls") or {}).get("48x48", "")}


def people(v):
    return [p for p in map(person, v if isinstance(v, list) else [v]) if p]


def myself():
    with _lock:
        if _me:
            return dict(_me)
    me = person(get(BASE + "/rest/api/3/myself"))
    with _lock:
        _me.update(me)
    return me


def active_sprint():
    board, prefix = CFG["board"]["id"], CFG["board"].get("sprintNamePrefix") or ""
    sprints = get(f"{BASE}/rest/agile/1.0/board/{board}/sprint?state=active")["values"]
    for s in sprints:
        if s["name"].startswith(prefix):
            return s
    raise LookupError(f"No active sprint on board {board}" + (f" whose name starts with {prefix!r}" if prefix else ""))


def icon_path(url):
    # Issue-type icons live on the Jira site and need auth, so the page loads them through /icon.
    return "/icon?u=" + urllib.parse.quote(url, safe="") if url and url.startswith(BASE + "/") else None


def icon(url):
    if not url.startswith(BASE + "/"):
        return 400, b"", "text/plain"
    with _lock:
        hit = _icons.get(url)
    if hit is None:
        body, ctype = fetch(url, accept="image/*")
        hit = (body, ctype or "image/svg+xml")
        with _lock:
            _icons[url] = hit
    return 200, hit[0], hit[1]


# ---------------------------------------------------------------------------------------------
# Board

def prio_level(name):
    m = re.match(r"\s*P(\d)\b", name or "", re.I)
    if m:
        return int(m.group(1))
    return PRIORITIES.get(norm(name))


def glob_any(name, patterns):
    name = (name or "").lower()
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def channels_of(issue, parents, channels=None):
    # A ticket belongs to a channel through its own fields or its parent's (labels, components),
    # its parent or grandparent key (epics), or its own, parent or epic summary.
    f = issue["fields"]
    p = f.get("parent") or {}
    pf = parents.get(p.get("key"), {}) if p else {}
    epic = pf.get("parent") or {}
    keys = {k for k in (p.get("key"), epic.get("key")) if k}
    texts = [f.get("summary") or "", pf.get("summary") or (p.get("fields") or {}).get("summary") or "", (epic.get("fields") or {}).get("summary") or ""]
    labels = list(f.get("labels") or []) + list(pf.get("labels") or [])
    comps = [c.get("name", "") for c in (f.get("components") or []) + (pf.get("components") or [])]
    out = []
    for ch in CHANNELS if channels is None else channels:
        if (keys & ch["epics"]
                or any(glob_any(x, ch["labels"]) for x in labels)
                or any(glob_any(x, ch["components"]) for x in comps)
                or (ch["summary"] and any(ch["summary"].search(t) for t in texts if t))):
            out.append(ch["id"])
    return out


def columns_for(status, type_name):
    s, t = norm(status), norm_type(type_name)
    return [c["id"] for c in COLUMNS if s in c["statuses"] and (not c["types"] or t in c["types"]) and t not in c["excludeTypes"]]


def fetch_parents(issues):
    keys = sorted({i["fields"]["parent"]["key"] for i in issues if i["fields"].get("parent")})
    parents = {}
    for n in range(0, len(keys), 100):
        for p in search("key in (%s)" % ",".join(keys[n:n + 100]), PARENT_FIELDS):
            parents[p["key"]] = p["fields"]
    return parents


def open_children(issues):
    # Open sub-tasks per parent, in any sprint, so the board can mark a story's last open sub-task.
    keys = sorted({i["fields"]["parent"]["key"] for i in issues
                   if i["fields"].get("parent") and i["fields"]["issuetype"].get("subtask")})
    count = {}
    for n in range(0, len(keys), 100):
        for c in search("parent in (%s) AND statusCategory != Done" % ",".join(keys[n:n + 100]), "parent"):
            k = c["fields"]["parent"]["key"]
            count[k] = count.get(k, 0) + 1
    return count


def blocked_by_this(f):
    # Open tickets this one holds up through a "Blocks" link (this ticket is the blocker).
    out = []
    for link in f.get("issuelinks") or []:
        other = link.get("outwardIssue")
        if other and (link.get("type") or {}).get("name", "").lower() == "blocks":
            of = other.get("fields") or {}
            st = of.get("status") or {}
            cat = (st.get("statusCategory") or {}).get("key")
            if cat != "done":
                out.append({"key": other["key"], "summary": of.get("summary", ""), "status": st.get("name", ""), "cat": cat or ""})
    return out


def sprint_info(s):
    prefix = CFG["board"].get("sprintNamePrefix") or ""
    name = s["name"]
    shown = name[len(prefix):].strip(" -–:") if prefix and name.startswith(prefix) else name
    return dict({k: s.get(k) for k in ("id", "name", "startDate", "endDate", "goal")}, displayName=shown or name)


def load_board():
    me = myself()
    sprint = active_sprint()
    issues = search(f"sprint = {sprint['id']} AND statusCategory != Done ORDER BY rank ASC", FIELDS)
    parents = fetch_parents(issues) if CHANNELS else {}
    siblings = open_children(issues)
    rows = []
    for rank, i in enumerate(issues):
        f = i["fields"]
        p = f.get("parent")
        prio = (f.get("priority") or {}).get("name", "")
        status = clean_status(f["status"]["name"])
        rows.append({
            "key": i["key"],
            "summary": f.get("summary") or "",
            "status": status,
            "type": f["issuetype"]["name"],
            "typeIcon": icon_path(f["issuetype"].get("iconUrl")),
            "prio": prio_level(prio),
            "prioName": prio,
            "assignee": person(f.get("assignee")),
            "roles": {r["id"]: people(f.get(r["field"])) for r in ROLES},
            "channels": channels_of(i, parents),
            "parent": {"key": p["key"], "summary": parents.get(p["key"], {}).get("summary") or (p.get("fields") or {}).get("summary", "")} if p else None,
            "blocks": blocked_by_this(f),
            "lastOpen": bool(p and f["issuetype"].get("subtask") and siblings.get(p["key"]) == 1),
            "rank": rank,
            "cols": columns_for(status, f["issuetype"]["name"]),
            "vars": prompt_vars_for(i["key"], f["issuetype"]["name"]),
        })
    return {"me": me, "base": BASE, "sprint": sprint_info(sprint), "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "rows": rows}


# Board and burndown answers take seconds (several Jira calls; the burndown reads every changed ticket's
# changelog). They are worked out in the background, so no request holds its connection open that long:
# without a fresh result the server answers 202 at once, with the last result as "stale" when it has one,
# and the page asks again shortly. A browser that reloads or closes the page in the meantime then has
# nothing pending.
_results = {}  # key -> (time, HTTP code, body)
_jobs = {}     # key -> the thread working it out
_gen = 0       # bumped when the settings change, so a job started before that does not store its result


def background(key, compute, force=False):
    with _lock:
        hit = _results.get(key)
        if hit and not force and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1], hit[2]
        if hit and force:
            _results[key] = (0.0, hit[1], hit[2])  # keep it as stale, but no longer fresh
        if key not in _jobs:
            job = threading.Thread(target=_run_job, args=(key, compute, _gen), daemon=True)
            _jobs[key] = job
            job.start()
    stale = hit[2] if hit and hit[1] == 200 else b"null"
    return 202, b'{"pending":true,"stale":' + stale + b"}"


def _run_job(key, compute, gen):
    try:
        code, body = 200, json.dumps(compute()).encode("utf-8")
    except Exception as ex:
        print(f"{key.split('|')[0]}: {describe(ex)}", file=sys.stderr)
        code, body = 502, json.dumps({"error": describe(ex)}).encode("utf-8")
    with _lock:
        if gen == _gen:
            _results[key] = (time.time(), code, body)
            _jobs.pop(key, None)


def forget_results():
    global _gen
    with _lock:
        _results.clear()
        _jobs.clear()
        _gen += 1


def board_json(force=False):
    return background("board", load_board, force)


# ---------------------------------------------------------------------------------------------
# Burndown: the main figure is the drop in Remaining Estimate a person makes on a local day.
# Optionally, for comparison, their first-time stage transitions are valued from an estimate comment
# such as "Dev: 2h | Rev: 30m | MR: 15m" (the newest full line wins, else the newest single values).

def local_day(ts):
    local = CLOCK(parse_ts(ts).astimezone(UTC))
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")


def today():
    return CLOCK(datetime.datetime.now(UTC)).strftime("%Y-%m-%d")


def hours(text):
    return sum(float(n) / (60 if u.lower() == "m" else 1) for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([hm])", text, re.I))


def adf_text(node):
    if isinstance(node, dict):
        own = node.get("text", "")
        inner = "".join(adf_text(c) for c in node.get("content", []))
        return own + inner + ("\n" if node.get("type") in ("paragraph", "heading", "listItem", "tableRow") else "")
    if isinstance(node, list):
        return "".join(adf_text(c) for c in node)
    return node if isinstance(node, str) else ""


def estimate(comments):
    full, part = None, {}
    for c in sorted(comments, key=lambda c: c["created"]):
        text = adf_text(c.get("body"))
        m = BURN["full"].search(text)
        if m:
            full = {s["id"]: hours(m.group(n + 1)) for n, s in enumerate(BURN["stages"])}
        for label, dur in BURN["part"].findall(text):
            part[BURN["byLabel"][label.lower()]] = hours(dur)
    return full or part or None


def issue_detail(key, updated):
    with single_flight("issue|" + key):
        with _lock:
            hit = _issue_cache.get(key)
        if hit and hit[0] == updated:
            return hit[1]
        histories = sorted(paged(f"{BASE}/rest/api/3/issue/{key}/changelog", "values"), key=lambda h: h["created"])
        worklogs = {}
        if any(it.get("field") == "WorklogId" for h in histories for it in h.get("items", [])):
            worklogs = {w["id"]: w["started"] for w in paged(f"{BASE}/rest/api/3/issue/{key}/worklog", "worklogs")}
        est = estimate(paged(f"{BASE}/rest/api/3/issue/{key}/comment?orderBy=created", "comments")) if BURN["stages"] else None
        detail = (histories, est, worklogs)
        with _lock:
            _issue_cache[key] = (updated, detail)
        return detail


def secs(v):
    try:
        return int(v or 0)
    except ValueError:
        return 0


def burn_events(day_from, day_to, sprint):
    since = (datetime.date.fromisoformat(day_from) - datetime.timedelta(days=1)).isoformat()
    issues = search(f'sprint = {sprint["id"]} AND updated >= "{since}"', "summary,assignee,updated,timeestimate,issuetype")
    issues = [i for i in issues if norm_type(i["fields"]["issuetype"]["name"]) not in BURN["excludeTypes"]]
    with concurrent.futures.ThreadPoolExecutor(JIRA_CONNECTIONS) as pool:
        details = list(pool.map(lambda i: issue_detail(i["key"], i["fields"]["updated"]), issues))
    events, remaining, done_left = [], [], []
    moves = BURN.get("moves") or {}
    for issue, (histories, est, worklogs) in zip(issues, details):
        f = issue["fields"]
        items = [it for h in histories for it in h["items"]]
        handovers = [it for it in items if it["field"] == "assignee"]
        assignee = handovers[0].get("from") if handovers else (f.get("assignee") or {}).get("accountId")
        re_moves = [it for it in items if it["field"] == "timeestimate"]
        left = secs(re_moves[0].get("from")) if re_moves else secs(f.get("timeestimate"))
        seen, timeline, dones = set(), [], []
        for h in histories:
            d, at = local_day(h["created"])
            when = parse_ts(h["created"])
            author = (h.get("author") or {}).get("accountId")
            row = {"key": issue["key"], "summary": f.get("summary") or "", "day": d, "at": at, "author": author}
            inside = day_from <= d <= day_to
            # A worklog lowers the remaining estimate when it is entered; book that drop on the day the work was done.
            wl = next((worklogs.get(it.get("to")) for it in h["items"] if it["field"] == "WorklogId"), None)
            for it in h["items"]:
                if it["field"] == "assignee":
                    assignee = it.get("to")
                elif it["field"] == "timeestimate":
                    old, new = secs(it.get("from")), secs(it.get("to"))
                    timeline.append((when, new))
                    wd, wat = local_day(wl) if wl else (d, at)
                    if day_from <= wd <= day_to and old != new:
                        remaining.append(dict(row, day=wd, at=wat, before=old / 3600, after=new / 3600, worklog=bool(wl)))
                elif it["field"] == "status":
                    if inside and norm(clean_status(it.get("toString"))) in BURN["done"]:
                        dones.append((when, row))
                    stage = moves.get((norm(clean_status(it.get("fromString"))), norm(clean_status(it.get("toString")))))
                    if not stage:
                        continue
                    first = stage["id"] not in seen
                    seen.add(stage["id"])
                    if not inside:
                        continue
                    reason = None if first else "notFirst"
                    if reason is None and (stage["by"] == "assignee") != (author == assignee):
                        reason = "notAssignee" if stage["by"] == "assignee" else "own"
                    if reason is None and not (est and stage["id"] in est):
                        reason = "noEstimate"
                    events.append(dict(row, stage=stage["id"], hours=est[stage["id"]] if reason is None else 0, reason=reason))
        # The remaining estimate is often zeroed in a separate changelog entry right after the Done move.
        for when, row in dones:
            later = [v for t, v in timeline if t <= when + datetime.timedelta(minutes=2)]
            rest = later[-1] if later else left
            if rest:
                done_left.append(dict(row, left=rest / 3600))
    return {"events": events, "remaining": remaining, "doneLeft": done_left}


def burn_json(day, whole_sprint=False, force=False):
    def compute():
        sprint = active_sprint()
        start = local_day(sprint["startDate"])[0] if whole_sprint else day
        return dict(burn_events(start, day, sprint), day=day, start=start, target=BURN["target"])
    return background(("burndown|sprint|" if whole_sprint else "burndown|") + day, compute, force)


def logged_json(day, person_id, force=False):
    def compute():
        prev = (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()
        nxt = (datetime.date.fromisoformat(day) + datetime.timedelta(days=1)).isoformat()
        issues = search(f'worklogAuthor = "{person_id}" AND worklogDate >= "{prev}" AND worklogDate <= "{nxt}"', "summary")
        with concurrent.futures.ThreadPoolExecutor(JIRA_CONNECTIONS) as pool:
            logs = list(pool.map(lambda i: paged(f"{BASE}/rest/api/3/issue/{i['key']}/worklog", "worklogs"), issues))
        items = []
        for issue, wls in zip(issues, logs):
            for w in wls:
                d, at = local_day(w["started"])
                if (w.get("author") or {}).get("accountId") == person_id and d == day:
                    items.append({"key": issue["key"], "summary": issue["fields"].get("summary") or "", "at": at,
                                  "seconds": secs(w.get("timeSpentSeconds"))})
        items.sort(key=lambda e: e["at"])
        return {"day": day, "person": person_id, "seconds": sum(e["seconds"] for e in items), "items": items}
    return background(f"logged|{day}|{person_id}", compute, force)


# ---------------------------------------------------------------------------------------------
# Page and HTTP server

def page_config():
    cols = [{k: c[k] for k in ("id", "name", "color", "rule", "channels", "stage", "description", "prompt", "promptIfNotAssignee",
                               "defaultPrompt", "defaultPromptIfNotAssignee")}
            for c in COLUMNS]
    for out, c in zip(cols, COLUMNS):
        out["types"] = c["typeNames"]
    return {
        "columns": cols,
        "roles": [{"id": r["id"], "label": r["label"]} for r in ROLES],
        "channels": [{"id": c["id"], "name": c["name"], "color": c["color"]} for c in CHANNELS],
        "ui": {"defaultLanguage": CFG["ui"].get("defaultLanguage", "en"), "refreshSeconds": int(CFG["ui"].get("refreshSeconds") or 60)},
        "burndown": {"enabled": BURN["enabled"], "target": BURN["target"], "low": BURN["low"], "mid": BURN["mid"],
                     "weekendDays": BURN["weekendDays"], "stages": [{"id": s["id"], "label": s["label"]} for s in BURN["stages"]]},
        "team": TEAM,
        "promptVars": [name for name, _ in PROMPT_VARS],
        # What the setup wizard edits, as it is today; the token guards its save requests.
        "setup": {"required": SETUP_REQUIRED, "token": SETUP_TOKEN,
                  "board": {"id": CFG["board"].get("id"), "sprintNamePrefix": CFG["board"].get("sprintNamePrefix") or ""},
                  # With the colour the header uses, so an untouched channel saves as it came from the config.
                  "channels": [dict(spec, color=c["color"]) for spec, c in zip(CFG.get("channels") or [], CHANNELS)]},
    }


def page():
    with open(PAGE_FILE, encoding="utf-8") as fh:
        html = fh.read()
    cfg = json.dumps(page_config()).replace("<", "\\u003c")
    return html.replace("/*__BOARD_CONFIG__*/null", cfg, 1).encode("utf-8")


ALLOWED_HOSTS = set()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    if os.name == "nt":
        # On Windows SO_REUSEADDR lets a second process bind a port that is already in use.
        allow_reuse_address = False

        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400" if self.path.startswith("/icon") and code == 200 else "no-store")
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            # The browser gave up on this request (the page was reloaded, closed or put to sleep) before the answer
            # was ready; there is nobody left to answer, so note it in one line instead of a traceback.
            print(f"{self.path.split('?')[0]}: the browser closed the connection before the answer was sent.", file=sys.stderr)
            self.close_connection = True

    def do_GET(self):
        # Only answer to localhost names, so a web page cannot reach this server through DNS rebinding.
        if (self.headers.get("Host") or "").lower() not in ALLOWED_HOSTS:
            self.send(403, b"Unknown host", "text/plain")
            return
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        force = q.get("force", [""])[0] == "1"
        if url.path == "/":
            self.send(200, page(), "text/html; charset=utf-8")
        elif url.path.startswith("/api/setup/"):
            self.send(*setup_get(url.path[len("/api/setup/"):], {k: v[0] for k, v in q.items()}), "application/json; charset=utf-8")
        elif url.path in ("/api/board", "/api/burn", "/api/logged") and SETUP_REQUIRED:
            self.send(409, b'{"error":"Finish the setup first.","setup":true}', "application/json; charset=utf-8")
        elif url.path == "/api/board":
            self.send(*board_json(force), "application/json; charset=utf-8")
        elif url.path == "/api/burn":
            if not BURN["enabled"]:
                self.send(404, b'{"error":"The burndown is turned off in the config."}', "application/json; charset=utf-8")
                return
            day = q.get("day", [""])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                day = today()
            self.send(*burn_json(day, q.get("scope", [""])[0] == "sprint", force), "application/json; charset=utf-8")
        elif url.path == "/api/logged":
            person_id = q.get("person", [""])[0]
            if not re.fullmatch(r"[A-Za-z0-9:_\-]{1,128}", person_id):
                self.send(400, b'{"error":"A valid person is required."}', "application/json; charset=utf-8")
                return
            day = q.get("day", [""])[0]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                day = today()
            self.send(*logged_json(day, person_id, force), "application/json; charset=utf-8")
        elif url.path == "/icon":
            try:
                code, body, ctype = icon(q.get("u", [""])[0])
            except Exception:
                code, body, ctype = 502, b"", "text/plain"
            self.send(code, body, ctype)
        else:
            self.send_error(404)

    def do_POST(self):
        # Read the body before anything else, also for a request that is refused: a socket closed with unread
        # data is reset on Windows, and the client then sees an aborted connection instead of the answer.
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = -1
        if not 0 <= size <= 1_000_000:
            self.close_connection = True
            self.send(413 if size > 0 else 400, b'{"error":"Content-Length is missing or too large"}', "application/json; charset=utf-8")
            return
        raw = self.rfile.read(size) if size else b""
        # Writes come only from this server's own page: localhost Host, same-origin Origin when sent,
        # JSON body and the per-process token from the page (another site cannot read or send it).
        origin = (self.headers.get("Origin") or "").lower()
        token = self.headers.get("X-Setup-Token") or ""
        if ((self.headers.get("Host") or "").lower() not in ALLOWED_HOSTS
                or (origin and origin not in {"http://" + h for h in ALLOWED_HOSTS})
                or not hmac.compare_digest(token, SETUP_TOKEN)
                or "application/json" not in (self.headers.get("Content-Type") or "")):
            self.send(403, b'{"error":"Forbidden"}', "application/json; charset=utf-8")
            return
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
        except ValueError as ex:
            self.send(400, json.dumps({"error": str(ex)}).encode(), "application/json; charset=utf-8")
            return
        path = urllib.parse.urlparse(self.path).path
        handler = {"/api/setup/preview": setup_preview, "/api/setup/save": setup_save}.get(path)
        if not handler:
            self.send_error(404)
            return
        self.send(*answer(handler, body), "application/json; charset=utf-8")

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------------------------------------
# Setup wizard: read-only Jira lookups, a channel preview, and saving the settings file.

def answer(fn, *args):
    try:
        return 200, json.dumps(fn(*args)).encode("utf-8")
    except (ConfigError, ValueError, KeyError, TypeError) as ex:
        return 400, json.dumps({"error": str(ex) or ex.__class__.__name__}).encode("utf-8")
    except Exception as ex:
        print("Setup:", describe(ex), file=sys.stderr)
        return 502, json.dumps({"error": describe(ex)}).encode("utf-8")


def setup_get(name, q):
    handlers = {
        "boards": lambda: setup_boards(q.get("q", ""), q.get("id")),
        "sprints": lambda: setup_sprints(int(q["board"])),
        "users": lambda: setup_users(q.get("q", "")),
        "sprint": lambda: setup_sprint_facts(int(q["sprint"])),
    }
    if name not in handlers:
        return 404, b'{"error":"Not found"}'
    return answer(handlers[name])


def board_row(b):
    loc = b.get("location") or {}
    return {"id": b["id"], "name": b.get("name", ""), "type": b.get("type", ""), "project": loc.get("projectKey") or ""}


def setup_boards(text, board_id=None):
    if board_id:
        return [board_row(get(f"{BASE}/rest/agile/1.0/board/{int(board_id)}"))]
    params = {"type": "scrum", "maxResults": 50}
    if text.strip():
        params["name"] = text.strip()
    return [board_row(b) for b in get(f"{BASE}/rest/agile/1.0/board?" + urllib.parse.urlencode(params)).get("values") or []]


def suggest_prefix(name):
    """The part of a sprint name that stays the same from sprint to sprint: "TEAM Sprint 42" -> "TEAM"."""
    m = re.match(r"^(.*?)[\s\-–:]*(?:sprint\s*)?#?\d+\s*$", name or "", re.I)
    return (m.group(1) if m else name or "").strip()


def setup_sprints(board_id):
    values = get(f"{BASE}/rest/agile/1.0/board/{board_id}/sprint?state=active").get("values") or []
    return [dict({k: s.get(k) for k in ("id", "name", "startDate", "endDate", "goal")}, prefix=suggest_prefix(s["name"])) for s in values]


def setup_users(text):
    if len(text.strip()) < 2:
        return []
    found = get(f"{BASE}/rest/api/3/user/search?" + urllib.parse.urlencode({"query": text.strip(), "maxResults": 20}))
    return [person(u) for u in found if u.get("accountType", "atlassian") == "atlassian" and u.get("active", True) and person(u)]


def facets(issues, parents):
    """Epics, labels and components in a set of issues, each with the number of issues that carry it."""
    epics, labels, comps = {}, {}, {}
    for i in issues:
        f = i["fields"]
        p = f.get("parent") or {}
        pf = parents.get(p.get("key"), {})
        epic = pf.get("parent") or (p if ((p.get("fields") or {}).get("issuetype") or {}).get("name", "").lower() == "epic" else {})
        if epic.get("key"):
            hit = epics.setdefault(epic["key"], [(epic.get("fields") or {}).get("summary", ""), 0])
            hit[1] += 1
        for x in set(f.get("labels") or []) | set(pf.get("labels") or []):
            labels[x] = labels.get(x, 0) + 1
        for x in {c.get("name", "") for c in (f.get("components") or []) + (pf.get("components") or [])} - {""}:
            comps[x] = comps.get(x, 0) + 1
    order = lambda d: sorted(d.items(), key=lambda x: (-(x[1][1] if isinstance(x[1], list) else x[1]), x[0]))  # noqa: E731
    return ([{"key": k, "summary": v[0], "n": v[1]} for k, v in order(epics)],
            [{"name": k, "n": v} for k, v in order(labels)],
            [{"name": k, "n": v} for k, v in order(comps)])


_setup_cache = {}


def sprint_issues(sprint_id, fresh=False):
    with _lock:
        hit = _setup_cache.get(sprint_id)
    if hit and not fresh and time.time() - hit[0] < 300:
        return hit[1], hit[2]
    issues = search(f"sprint = {sprint_id} AND statusCategory != Done", FIELDS)
    parents = fetch_parents(issues)
    with _lock:
        _setup_cache[sprint_id] = (time.time(), issues, parents)
    return issues, parents


def setup_sprint_facts(sprint_id):
    issues, parents = sprint_issues(sprint_id, fresh=True)
    found = {}
    for i in issues:
        f = i["fields"]
        for p in [person(f.get("assignee"))] + [x for r in ROLES for x in people(f.get(r["field"]))]:
            if p:
                found.setdefault(p["id"], p)
    epics, labels, comps = facets(issues, parents)
    return {"total": len(issues), "people": sorted(found.values(), key=lambda p: p["name"].lower()),
            "epics": epics[:40], "labels": labels[:40], "components": comps[:40]}


def setup_preview(body):
    issues, parents = sprint_issues(int(body["sprint"]))
    channels = compile_channels(body.get("channels") or [])
    hits = {c["id"]: [] for c in channels + [{"id": "other"}]}
    for i in issues:
        for cid in channels_of(i, parents, channels) or ["other"]:
            hits[cid].append(i["key"])
    return {"total": len(issues), "matches": {k: {"n": len(v), "keys": v[:5]} for k, v in hits.items()}}


def channel_key(ch):
    return (ch.get("id"), ch.get("name") or ch.get("id"), (ch.get("color") or DEFAULT_CHANNEL_COLOR).lower(),
            sorted(str_list(ch.get("epics"), "epics")), [p.lower() for p in str_list(ch.get("labels"), "labels")],
            [p.lower() for p in str_list(ch.get("components"), "components")],
            [w.strip() for w in str_list(ch.get("keywords"), "keywords") if w.strip()], (ch.get("summary") or "").strip())


def team_ids(team):
    return [p if isinstance(p, str) else str((p or {}).get("id")) for p in team or []]


def settings_delta(settings, base):
    """Drop what the config file already says. A copy of a config value in settings.json would hide later edits
    of the config file, so the file stays the source of truth for everything the wizard did not change."""
    out = {}
    base_board = {"id": base["board"].get("id"), "sprintNamePrefix": base["board"].get("sprintNamePrefix") or ""}
    try:
        base_board["id"] = int(base_board["id"])
    except (TypeError, ValueError):
        pass
    board = {k: v for k, v in settings["board"].items() if v != base_board.get(k)}
    if board:
        out["board"] = board
    if team_ids(settings["team"]) != team_ids(base.get("team")):
        out["team"] = settings["team"]
    if [channel_key(c) for c in settings["channels"]] != [channel_key(c) for c in base.get("channels") or []]:
        out["channels"] = settings["channels"]
    defaults = {c["id"]: {"prompt": c["defaultPrompt"], "promptIfNotAssignee": c["defaultPromptIfNotAssignee"]} for c in COLUMNS}
    prompts = {}
    for cid, p in settings["prompts"].items():
        if cid in defaults and isinstance(p, dict):
            changed = {k: v for k, v in p.items() if k in defaults[cid] and v not in (None, "") and v != defaults[cid][k]}
            if changed:
                prompts[cid] = changed
    if prompts:
        out["prompts"] = prompts
    if settings["ui"]["refreshSeconds"] != int(base["ui"].get("refreshSeconds") or 60):
        out["ui"] = settings["ui"]
    return out


def setup_save(body):
    board = body.get("board") or {}
    if not board.get("id"):
        raise ConfigError("Pick a board and its active sprint first.")
    settings = settings_delta(pick_settings({
        "board": {"id": int(board["id"]), "sprintNamePrefix": str(board.get("sprintNamePrefix") or "")},
        "team": body.get("team") or [],
        "channels": body.get("channels") or [],
        "prompts": body.get("prompts") or {},
        "ui": {"refreshSeconds": max(15, int(body.get("refreshSeconds") or 60))},
    }), BASE_CFG)
    warnings = configure(deep_merge(BASE_CFG, settings), BASE_CFG)
    write_settings(settings_path(), settings)
    forget_results()
    if BURN["enabled"]:
        burn_json(today(), True)
    return {"ok": True, "warnings": warnings, "path": settings_path()}


# ---------------------------------------------------------------------------------------------
# Setup helpers: read-only lookups that help fill in the config without handling the token.

def list_boards(name=""):
    out, start = [], 0
    while True:
        q = {"startAt": start, "maxResults": 50}
        if name:
            q["name"] = name
        d = get(f"{BASE}/rest/agile/1.0/board?" + urllib.parse.urlencode(q))
        vals = d.get("values") or []
        out += vals
        start += len(vals)
        if d.get("isLast", True) or not vals:
            break
    for b in out:
        loc = b.get("location") or {}
        print(f"{b['id']:>6}  {b.get('type', ''):<7} {b.get('name', '')}" + (f"  (project {loc['projectKey']})" if loc.get("projectKey") else ""))
    return 0 if out else 1


def list_fields():
    found = 0
    for f in get(BASE + "/rest/api/3/field"):
        kind = ((f.get("schema") or {}).get("custom") or "").lower()
        if f.get("custom") and "userpicker" in kind:
            found += 1
            print(f"{f['id']:<20} {'many' if 'multi' in kind else 'one ':<5} {f.get('name', '')}")
    if not found:
        print("No user-picker custom fields; leave reviewerRoles empty.")
    return 0


def check():
    """Connect, find the sprint and show how its statuses map to the configured columns."""
    if SETUP_REQUIRED:
        print("No board yet. Start the board and finish the setup in the browser, or set board.id in the config.", file=sys.stderr)
        return 2
    try:
        me = myself()
        print(f"Connected to {BASE} as {me['name']}.")
        sprint = active_sprint()
        print(f"Board {CFG['board']['id']}: active sprint {sprint['name']!r} ({(sprint.get('startDate') or '')[:10]} to {(sprint.get('endDate') or '')[:10]}).")
        issues = search(f"sprint = {sprint['id']} AND statusCategory != Done", FIELDS)
    except Exception as ex:
        print(f"Check failed: {describe(ex)}", file=sys.stderr)
        return 1
    counts = {}
    for i in issues:
        s = clean_status(i["fields"]["status"]["name"])
        counts[s] = counts.get(s, 0) + 1
    print(f"{len(issues)} open issues in the sprint. Status -> column:")
    unmatched = []
    for s, n in sorted(counts.items(), key=lambda x: -x[1]):
        cols = [c["name"] for c in COLUMNS if norm(s) in c["statuses"]]
        unmatched += [] if cols else [s]
        print(f"  {s:<28} {n:>4}  {', '.join(cols) if cols else 'NOT SHOWN: add it to a column statuses list'}")
    for r in ROLES:
        filled = sum(1 for i in issues if people(i["fields"].get(r["field"])))
        print(f"Reviewer role {r['id']!r} ({r['field']}): set on {filled} of {len(issues)} issues.")
    for t in sorted({i["fields"]["issuetype"]["name"] for i in issues}):
        print(f"  issue type seen: {t}")
    try:
        channel_report(issues, fetch_parents(issues))
    except Exception as ex:
        print(f"Channel candidates unavailable: {describe(ex)}", file=sys.stderr)
    print("OK" if not unmatched else f"OK, but {len(unmatched)} status(es) have no column.")
    return 0


def channel_report(issues, parents):
    """Print what channels could be built from (epics, labels, components) and how the configured ones match."""
    epics, labels, comps = facets(issues, parents)
    print("Channel candidates (optional header chips; each issue counted once per value):")
    for title, rows in (("epics", [(e["n"], f"{e['key']}  {e['summary']}") for e in epics]),
                        ("labels", [(x["n"], x["name"]) for x in labels]),
                        ("components", [(x["n"], x["name"]) for x in comps])):
        print(f"  {title}:" + (" none" if not rows else ""))
        for n, text in rows[:12]:
            print(f"    {n:>4}  {text}")
    if not CHANNELS:
        print("No channels configured: the header shows no channel chips and no column filters by channel.")
        return
    counts = {}
    for i in issues:
        for ch in channels_of(i, parents) or ["other"]:
            counts[ch] = counts.get(ch, 0) + 1
    for ch in CHANNELS + [{"id": "other", "name": "Other (no channel)"}]:
        print(f"Channel {ch['name']!r}: {counts.get(ch['id'], 0)} of {len(issues)} open issues.")


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="A local, person-focused board for the active sprint of a Jira Cloud board.")
    ap.add_argument("--config", help="config file (default: $BOARD_CONFIG, else config.jsonc or config.json next to board.py)")
    ap.add_argument("--env-file", help="file with the JIRA_* variables (default: $BOARD_ENV_FILE, else .env next to board.py)")
    ap.add_argument("--port", type=int, help="port on 127.0.0.1 (default: server.port, 8765)")
    ap.add_argument("--board", type=int, help="board id; overrides board.id")
    ap.add_argument("--sprint-prefix", help="overrides board.sprintNamePrefix")
    ap.add_argument("--open", action="store_true", help="open the board in the default browser")
    ap.add_argument("--check", action="store_true", help="check the config and the Jira connection, show the status mapping, then exit")
    ap.add_argument("--list-boards", nargs="?", const="", metavar="NAME", help="list the boards this account can see (optionally filtered by name), then exit")
    ap.add_argument("--list-fields", action="store_true", help="list user-picker custom fields (for reviewerRoles), then exit")
    args = ap.parse_args(argv)
    global BASE_CFG
    try:
        cfg, path = read_config(args.config)
        BASE_CFG = copy.deepcopy(cfg)
        cfg = deep_merge(cfg, read_settings(settings_path()))
        if args.board is not None:
            cfg["board"]["id"] = args.board
        if args.sprint_prefix is not None:
            cfg["board"]["sprintNamePrefix"] = args.sprint_prefix
        env_file = args.env_file or os.environ.get("BOARD_ENV_FILE")
        if env_file and not os.path.isfile(env_file):
            raise ConfigError(f"Env file not found: {env_file}")
        load_env_file(env_file or os.path.join(HERE, ".env"))
        mcp = os.environ.get("JIRA_MCP_CONFIG") or cfg["jira"].get("mcpConfig")
        if mcp:
            load_mcp_credentials(mcp)
        if args.list_boards is not None or args.list_fields:
            connect(cfg)
            try:
                return list_boards(args.list_boards) if args.list_boards is not None else list_fields()
            except Exception as ex:
                print(f"Jira lookup failed: {describe(ex)}", file=sys.stderr)
                return 1
        warnings = configure(cfg, BASE_CFG)
        connect(cfg)
    except ConfigError as ex:
        print(f"Config error: {ex}", file=sys.stderr)
        return 2
    if args.check:
        for w in warnings:
            print(f"Warning: {w}", file=sys.stderr)
        return check()
    for w in warnings:
        print(f"Warning: {w}", file=sys.stderr)
    port = args.port or int(cfg["server"].get("port") or 8765)
    ALLOWED_HOSTS.update({f"localhost:{port}", f"127.0.0.1:{port}"})
    try:
        srv = Server(("127.0.0.1", port), Handler)
    except OSError as ex:
        print(f"Cannot listen on 127.0.0.1:{port} ({ex.strerror or ex}). Pick another port with --port.", file=sys.stderr)
        return 2
    url = f"http://localhost:{port}"
    print(f"Sprint Focus Board: {url}  ({'config ' + path if path else 'built-in defaults'}; Ctrl+C to stop)", flush=True)
    if SETUP_REQUIRED:
        print(f"No board is set yet: open {url} and finish the setup there. It is saved to {settings_path()}.", flush=True)
    elif BURN["enabled"]:
        # The sprint-wide burndown reads every changelog once; start on it before the page asks.
        burn_json(today(), True)
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
