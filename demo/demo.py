#!/usr/bin/env python3
"""Run the board against a built-in fake Jira filled with made-up data.

    python demo/demo.py [--port 8765] [--open]

Use it to try the board without a Jira site, or to retake the README screenshots.
Nothing here talks to a real Jira: the fake site runs on a random 127.0.0.1 port in this
process, and its sprint, tickets and burndown history are generated around today's date.
"""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import board  # noqa: E402

PEOPLE = {
    "alex": ("Alex Rivera", "#3B82F6"),
    "priya": ("Priya Shah", "#D946EF"),
    "tom": ("Tom Becker", "#F59E0B"),
    "mei": ("Mei Lin", "#10B981"),
    "sam": ("Sam Okafor", "#EF4444"),
    "lena": ("Lena Novak", "#8B5CF6"),
    # Not on the sprint; they only show up in the setup wizard's people search.
    "jordan": ("Jordan Blake", "#0EA5E9"),
    "ava": ("Ava Chen", "#F43F5E"),
    "noah": ("Noah Kim", "#84CC16"),
    "olivia": ("Olivia Park", "#A855F7"),
    "liam": ("Liam Rossi", "#F97316"),
    "emma": ("Emma Silva", "#14B8A6"),
}
BOARDS = [{"id": 1, "name": "TEAM board", "type": "scrum", "location": {"projectKey": "DEMO"}},
          {"id": 2, "name": "Platform board", "type": "scrum", "location": {"projectKey": "PLAT"}},
          {"id": 3, "name": "Support queue", "type": "kanban", "location": {"projectKey": "SUP"}}]
ICONS = {
    "Sub-task": ('#4BADE8', '<rect x="4" y="4" width="5" height="5" rx="1" fill="#fff"/><rect x="7" y="7" width="5" height="5" rx="1" fill="#fff" opacity=".7"/>'),
    "Bug": ('#E5493A', '<circle cx="8" cy="8" r="3.2" fill="#fff"/>'),
    "Spike": ('#F59E0B', '<path d="M9 3 5 9h3l-1 4 4-6H8z" fill="#fff"/>'),
    "Task": ('#4BADE8', '<path d="m4.5 8.2 2.3 2.3 4.7-5" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round"/>'),
    "Story": ('#63BA3C', '<path d="M5 3.5h6v9l-3-2-3 2z" fill="#fff"/>'),
    "Epic": ('#904EE2', '<path d="M9 3 5 9h3l-1 4 4-6H8z" fill="#fff"/>'),
}

# key, type, summary, status, priority, assignee, approvers, merge approvers, parent, labels
TICKETS = [
    ("DEMO-1", "Epic", "Payments platform", "In Progress", "Medium", None, [], [], None, []),
    ("DEMO-2", "Epic", "Mobile app", "In Progress", "Medium", None, [], [], None, []),
    ("DEMO-10", "Story", "Card payments v2", "In Progress", "High", None, [], [], "DEMO-1", []),
    ("DEMO-11", "Story", "Self-service refunds", "In Progress", "Medium", None, [], [], "DEMO-1", []),
    ("DEMO-12", "Story", "Mobile checkout redesign", "In Progress", "High", None, [], [], "DEMO-2", []),
    ("DEMO-13", "Story", "Account settings clean-up", "To Do", "Low", None, [], [], None, []),
    ("DEMO-21", "Sub-task", "Add the 3-D Secure challenge screen", "To Do", "High", None, [], [], "DEMO-10", []),
    ("DEMO-22", "Bug", "Refund total rounds half-cents the wrong way", "To Do", "Highest", None, [], [], "DEMO-11", []),
    ("DEMO-23", "Spike", "Compare wallet SDKs for Android", "To Do", "Medium", None, [], [], "DEMO-12", ["mobile-android"]),
    ("DEMO-24", "Sub-task", "Validate the IBAN before saving payout details", "To Do", "Low", None, [], [], "DEMO-13", []),
    ("DEMO-25", "Bug", "Saved card list still shows expired cards", "Reopened", "High", "alex", ["priya"], ["tom"], "DEMO-10", []),
    ("DEMO-26", "Sub-task", "Retry webhooks with exponential backoff", "In Progress", "Highest", "alex", [], [], "DEMO-10", []),
    ("DEMO-27", "Sub-task", "Partial refund API endpoint", "In Progress", "Medium", "alex", ["mei"], [], "DEMO-11", []),
    ("DEMO-28", "Sub-task", "Apple Pay merchant validation", "Blocked", "High", "alex", ["sam"], [], "DEMO-12", []),
    ("DEMO-29", "Bug", "Checkout hangs when the bank times out", "Blocked", "Highest", "tom", ["alex"], ["priya"], "DEMO-10", []),
    ("DEMO-30", "Sub-task", "Receipt email template", "To Review", "Medium", "mei", [], [], "DEMO-11", []),
    ("DEMO-31", "Sub-task", "Biometric unlock on payment confirm", "To Review", "High", "sam", ["alex"], [], "DEMO-12", []),
    ("DEMO-32", "Bug", "Currency symbol missing on iOS", "To Review", "Medium", "lena", ["priya"], [], "DEMO-12", []),
    ("DEMO-33", "Sub-task", "Export the ledger to CSV", "In Review", "Low", "priya", ["alex"], [], "DEMO-11", []),
    ("DEMO-34", "Spike", "Load-test the refund queue", "In Review", "Medium", "tom", [], [], "DEMO-11", ["pay-perf"]),
    ("DEMO-35", "Sub-task", "Feature flag for the new checkout", "For Merge Approval", "High", "mei", ["priya"], ["alex"], "DEMO-12", []),
    ("DEMO-36", "Bug", "Duplicate charge on a double tap", "For Merge Approval", "Highest", "sam", ["tom"], [], "DEMO-10", []),
    ("DEMO-37", "Sub-task", "Dark mode for receipts", "For Merge Approval", "Lowest", "lena", ["alex"], [], "DEMO-11", []),
    ("DEMO-38", "Sub-task", "Payout schedule settings", "In Progress", "Medium", "priya", [], [], "DEMO-13", []),
    ("DEMO-39", "Task", "Rotate the sandbox API keys", "In Progress", "Low", "tom", [], [], None, []),
    ("DEMO-40", "Sub-task", "Offline mode banner", "In Progress", "Medium", "sam", [], [], "DEMO-12", []),
    ("DEMO-41", "Sub-task", "Tokenise stored cards", "Done", "High", "alex", ["mei"], ["priya"], "DEMO-10", []),
    ("DEMO-43", "Sub-task", "Mask card numbers in the logs", "To Review", "High", "alex", [], [], "DEMO-10", []),
    ("DEMO-44", "Sub-task", "Retry banner copy", "For Merge Approval", "Low", "priya", ["alex"], ["tom"], "DEMO-12", []),
]
ESTIMATES = {"DEMO-41": "Dev: 4h | Rev: 45m | MR: 15m", "DEMO-43": "Dev: 3h | Rev: 45m | MR: 15m",
             "DEMO-44": "Dev: 2h | Rev: 30m | MR: 15m", "DEMO-26": "Dev: 6h | Rev: 1h | MR: 30m"}


class Data:
    def __init__(self, base):
        self.base = base
        self.tz = datetime.datetime.now().astimezone().tzinfo
        self.today = datetime.datetime.now(self.tz).date()
        self.start = self.today - datetime.timedelta(days=8)
        self.histories = {t[0]: [] for t in TICKETS}
        self.worklogs = {}
        self.remaining = {t[0]: 0 for t in TICKETS}
        self.build_history()

    def at(self, day, hhmm):
        h, m = map(int, hhmm.split(":"))
        return datetime.datetime(day.year, day.month, day.day, h, m, tzinfo=self.tz)

    @staticmethod
    def stamp(dt):
        return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

    def change(self, key, when, who, *items):
        self.histories[key].append({"id": str(len(self.histories[key]) + 1), "created": self.stamp(when),
                                    "author": self.user(who), "items": list(items)})

    def re_change(self, key, when, who, new_hours, worklog_started=None):
        old = self.remaining[key]
        new = int(new_hours * 3600)
        items = [{"field": "timeestimate", "fieldtype": "jira", "from": str(old), "to": str(new)}]
        if worklog_started:
            wid = str(9000 + len(self.worklogs))
            self.worklogs.setdefault(key, []).append({"id": wid, "started": self.stamp(worklog_started), "author": self.user(who),
                                                     "timeSpentSeconds": max(old - new, 900)})
            items.insert(0, {"field": "WorklogId", "fieldtype": "jira", "from": None, "to": wid})
        self.remaining[key] = new
        self.change(key, when, who, *items)

    def move(self, key, when, who, frm, to):
        self.change(key, when, who, {"field": "status", "fieldtype": "jira", "fromString": frm, "toString": to})

    def drop(self, key, when, who, hours, worklog_started=None):
        self.re_change(key, when, who, max(0, self.remaining[key] / 3600 - hours), worklog_started)

    def build_history(self):
        work = [t for t in TICKETS if t[1] not in ("Story", "Epic")]
        for t in work:
            self.re_change(t[0], self.at(self.start, "08:00"), t[5] or "priya", 2 if t[0] == "DEMO-41" else 20)
        owned = {p: keys for p in PEOPLE if (keys := [t[0] for t in work if t[5] == p and t[0] != "DEMO-41"])}
        pattern = {"alex": [5.5, 6.25, 3.5, 7, 1.75, 6, 5], "priya": [4, 6.5, 6, 2.5, 5.25, 7, 3],
                   "tom": [3, 2, 6, 5.5, 4.25, 1.5, 6], "mei": [6, 5, 7.25, 4, 6, 3.5, 5],
                   "sam": [2.5, 4, 5, 6.75, 3, 5.5, 4], "lena": [1.5, 3, 2.25, 4.5, 6, 2, 3.5]}
        d, n = self.start, 0
        while d < self.today:
            if d.isoweekday() <= 5:
                for p, keys in owned.items():
                    hours = pattern[p][n % 7]
                    first = round(hours * 0.6 * 4) / 4
                    self.drop(keys[n % len(keys)], self.at(d, "11:30"), p, first)
                    self.drop(keys[(n + 1) % len(keys)], self.at(d, "16:10"), p, hours - first)
                n += 1
            last = d
            d += datetime.timedelta(days=1)
        self.drop("DEMO-41", self.at(last, "15:00"), "alex", 1.5)
        today = self.today
        # Today, for the token owner: 4.25h burned, one raise, a worklog booked on its start time,
        # two stage moves valued from estimate comments, and a Done move that leaves 0.5h.
        self.drop("DEMO-26", self.at(today, "09:12"), "alex", 1.5)
        self.drop("DEMO-27", self.at(today, "10:40"), "alex", 1.25, worklog_started=self.at(today, "08:30"))
        self.move("DEMO-44", self.at(today, "11:20"), "alex", "In Review", "For Merge Approval")
        self.drop("DEMO-25", self.at(today, "11:55"), "alex", -0.5)
        self.move("DEMO-43", self.at(today, "13:10"), "alex", "In Progress", "To Review")
        self.drop("DEMO-26", self.at(today, "14:20"), "alex", 1.5)
        self.move("DEMO-41", self.at(today, "15:05"), "alex", "For Merge Approval", "Done")
        for p in ("priya", "tom", "mei", "sam"):
            self.drop(owned[p][0], self.at(today, "10:05"), p, 2.5)
        for key in self.histories:
            self.histories[key].sort(key=lambda h: h["created"])

    def user(self, pid):
        if not pid:
            return None
        name, _ = PEOPLE[pid]
        return {"accountId": "demo-" + pid, "displayName": name, "avatarUrls": {"48x48": f"{self.base}/avatars/{pid}.svg"}}

    def sprint(self):
        end = self.start + datetime.timedelta(days=13)
        return {"id": 42, "name": "TEAM Sprint 42", "state": "active", "goal": "Ship card payments v2 and the new mobile checkout",
                "startDate": self.stamp(self.at(self.start, "09:00")), "endDate": self.stamp(self.at(end, "17:00"))}

    def fields(self, t, wanted):
        key, typ, summary, status, prio, who, appr, merge, parent, labels = t
        by_key = {x[0]: x for x in TICKETS}
        f = {
            "summary": summary,
            "status": {"name": status},
            "priority": {"name": prio},
            "issuetype": {"name": typ, "iconUrl": f"{self.base}/images/icons/{typ.lower()}.svg"},
            "assignee": self.user(who),
            "labels": labels,
            "components": [],
            "customfield_10001": [self.user(p) for p in appr],
            "customfield_10002": [self.user(p) for p in merge],
            "timeestimate": self.remaining[key],
            "updated": self.histories[key][-1]["created"] if self.histories[key] else self.stamp(self.at(self.start, "08:00")),
        }
        if parent:
            p = by_key[parent]
            f["parent"] = {"key": parent, "fields": {"summary": p[2], "issuetype": {"name": p[1]}}}
        return {k: v for k, v in f.items() if k in wanted or k == "parent" and "parent" in wanted}

    def search(self, jql, fields):
        wanted = set(fields.split(","))
        m = re.search(r"key in \(([^)]*)\)", jql)
        if m:
            keys = m.group(1).split(",")
            picked = [t for t in TICKETS if t[0] in keys]
        elif "worklogAuthor" in jql:
            picked = [t for t in TICKETS if t[0] in self.worklogs]
        elif "statusCategory != Done" in jql:
            picked = [t for t in TICKETS if t[3] != "Done" and t[1] != "Epic"]
        else:
            picked = [t for t in TICKETS if t[1] != "Epic"]
        return {"issues": [{"key": t[0], "fields": self.fields(t, wanted)} for t in picked], "isLast": True}

    def comments(self, key):
        if key not in ESTIMATES:
            return []
        body = {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Estimate: " + ESTIMATES[key]}]}]}
        return [{"id": "1", "created": self.stamp(self.at(self.start, "10:00")), "body": body}]


def svg_avatar(pid):
    name, color = PEOPLE[pid]
    ini = "".join(w[0] for w in name.split()[:2])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48"><circle cx="24" cy="24" r="24" fill="{color}"/>'
            f'<text x="24" y="30" text-anchor="middle" font-family="system-ui,sans-serif" font-size="17" font-weight="600" fill="#fff">{ini}</text></svg>')


def svg_icon(name):
    for typ, (color, inner) in ICONS.items():
        if typ.lower() == name:
            return f'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="{color}"/>{inner}</svg>'
    return None


def fake_jira(state):
    """A fake Jira on a free 127.0.0.1 port; state["data"] is filled in once the port is known."""
    class Handler(BaseHTTPRequestHandler):
        def reply(self, body, ctype="application/json"):
            raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            data = state["data"]
            url = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            path = url.path
            m = re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)/(changelog|comment|worklog)", path)
            if path == "/rest/api/3/myself":
                self.reply(data.user("alex"))
            elif re.fullmatch(r"/rest/agile/1.0/board/\d+/sprint", path):
                self.reply({"values": [data.sprint()] if path.split("/")[5] == "1" else []})
            elif path == "/rest/agile/1.0/board":
                name = q.get("name", "").lower()
                found = [b for b in BOARDS if name in b["name"].lower() and q.get("type", b["type"]) == b["type"]]
                self.reply({"values": found, "isLast": True})
            elif re.fullmatch(r"/rest/agile/1.0/board/\d+", path):
                found = [b for b in BOARDS if str(b["id"]) == path.rsplit("/", 1)[1]]
                self.reply(found[0]) if found else self.send_error(404)
            elif path == "/rest/api/3/user/search":
                text = q.get("query", "").lower()
                self.reply([dict(data.user(p), accountType="atlassian", active=True) for p, (n, _) in PEOPLE.items()
                            if text and (text in n.lower() or text in p + "@example.com")])
            elif path == "/rest/api/3/search/jql":
                self.reply(data.search(q.get("jql", ""), q.get("fields", "")))
            elif m:
                key, what = m.groups()
                vals = {"changelog": data.histories.get(key, []), "comment": data.comments(key), "worklog": data.worklogs.get(key, [])}[what]
                name = {"changelog": "values", "comment": "comments", "worklog": "worklogs"}[what]
                start = int(q.get("startAt", 0))
                self.reply({name: vals[start:start + 100], "startAt": start, "total": len(vals)})
            elif path.startswith("/avatars/") and path[9:-4] in PEOPLE:
                self.reply(svg_avatar(path[9:-4]), "image/svg+xml")
            elif path.startswith("/images/icons/") and svg_icon(path[14:-4]):
                self.reply(svg_icon(path[14:-4]), "image/svg+xml")
            else:
                self.send_error(404)

        def log_message(self, *a):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 64  # the page loads every avatar at once; the default backlog of 5 refuses some

    return Server(("127.0.0.1", 0), Handler)


def main():
    ap = argparse.ArgumentParser(description="Run the board against a fake Jira with made-up data.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--first-run", action="store_true", help="start without a board, so the setup wizard opens")
    args = ap.parse_args()
    state = {}
    srv = fake_jira(state)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    state["data"] = Data(base)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # Set, not setdefault: the demo must never reach a real Jira site from the environment or .env.
    # Wizard saves go to a throwaway file, never to the settings of a real board.
    work = tempfile.mkdtemp(prefix="sprint-focus-board-demo-")
    os.environ.update(JIRA_BASE_URL=base, JIRA_EMAIL="alex@example.com", JIRA_API_TOKEN="demo",
                      BOARD_SETTINGS=os.path.join(work, "settings.json"))
    config = os.path.join(HERE, "config.jsonc")
    if args.first_run:
        cfg = json.loads(board.strip_comments(open(config, encoding="utf-8").read()))
        cfg.pop("board", None)
        cfg.pop("channels", None)
        config = os.path.join(work, "config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
    return board.main(["--config", config, "--port", str(args.port)] + (["--open"] if args.open else []))


if __name__ == "__main__":
    sys.exit(main())
