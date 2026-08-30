#!/usr/bin/env python3
"""
Minimal standalone backend for the "I Am Money" Profit First tracker.
Zero dependencies - Python 3's standard library only. Run with:

    SYNC_TOKEN=your-long-random-secret python3 server.py

It does two jobs:

1. Cloud save for the tracker HTML page itself:
     GET  /api/state   -> the full app state (same shape the app used to
                           keep in localStorage)
     PUT  /api/state   -> overwrite the full app state

2. An MCP server (Streamable HTTP, JSON-RPC 2.0) so Claude can log
   transactions and read balances during normal chat:
     POST /mcp

Both require `Authorization: Bearer <SYNC_TOKEN>`. Same token for both -
the HTML page embeds it (see the sync block near the top of
profit-first-tracker.html) and you paste it into Claude's connector setup
as the Authorization header when you add /mcp as a custom connector.

Storage is a single key in an Upstash Redis database (free tier, REST API,
no SDK needed) rather than a local file - a host like Render rebuilds the
filesystem from scratch on every deploy, which silently wipes a local file.
Needs two more env vars: UPSTASH_REDIS_REST_URL and
UPSTASH_REDIS_REST_TOKEN, both copy-pasteable straight from the Upstash
console for your database.

NOTE ON AUTH: SYNC_TOKEN is a shared secret embedded in client-side HTML -
anyone with the file's source can read it and hit this API directly. That's
an acceptable MVP tradeoff for an internal tool with a small, trusted team;
move to per-user login when this graduates to the real Laravel backend.
"""

import json
import os
import sys
import time
import random
import string
import urllib.request
import urllib.error
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", 8787))
TRACKER_HTML_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tracker.html")
TOKEN = os.environ.get("SYNC_TOKEN")
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN")
STATE_KEY = "profit_first_tracker_state"

if not TOKEN:
    print("Set SYNC_TOKEN before starting, e.g.:")
    print("  SYNC_TOKEN=$(python3 -c \"import secrets; print(secrets.token_hex(32))\") python3 server.py")
    sys.exit(1)

if not UPSTASH_URL or not UPSTASH_TOKEN:
    print("Set UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN before starting")
    print("(from your Upstash Redis database's console page).")
    sys.exit(1)

# ---- state storage (Upstash Redis REST API) -----------------------------

def _upstash_request(method, path, body=None):
    req = urllib.request.Request(
        UPSTASH_URL.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + UPSTASH_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def read_state():
    try:
        result = _upstash_request("GET", "/get/" + STATE_KEY)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print("Upstash read failed:", e, file=sys.stderr)
        return None
    value = result.get("result")
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def write_state(state):
    body = json.dumps(state).encode("utf-8")
    result = _upstash_request("POST", "/set/" + STATE_KEY, body=body)
    if result.get("result") != "OK":
        raise RuntimeError(f"Upstash write did not confirm OK: {result}")


def round2(v):
    return round(float(v) + 1e-9, 2)


def uid():
    return "tx_" + format(int(time.time() * 1000), "x") + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def today_str():
    return date.today().isoformat()


# Mirrors profit-first-tracker.html's own splittableBuckets()/computeIncomeSplit()
# so chat-logged income lands in the same buckets, in the same proportions,
# as an entry made by hand in the app.
def splittable_buckets(state):
    return [b for b in state["buckets"] if b.get("tier") != "secondary"]


def compute_income_split(state, base):
    buckets = splittable_buckets(state)
    amounts = {}
    running = 0
    for i, b in enumerate(buckets):
        if i == len(buckets) - 1:
            amounts[b["id"]] = round2(base - running)
        else:
            a = round(base * (float(b.get("cap") or 0)) / 100)
            amounts[b["id"]] = a
            running += a
    return amounts


def resolve_account_id(state, account_name):
    if account_name:
        for a in state["accounts"]:
            if a["id"] == account_name or a["name"].lower() == str(account_name).lower():
                return a["id"]
    default_id = state.get("defaultAccountId")
    if default_id and any(a["id"] == default_id for a in state["accounts"]):
        return default_id
    return state["accounts"][0]["id"] if state["accounts"] else "cash"


def resolve_bucket_id(state, bucket_name):
    for b in state["buckets"]:
        if b["id"] == bucket_name or b["name"].lower() == str(bucket_name or "").lower():
            return b["id"]
    return None


def bucket_label(state, bucket_id):
    for b in state["buckets"]:
        if b["id"] == bucket_id:
            return b["name"]
    return bucket_id


# ---- MCP tool implementations ------------------------------------------

class ToolError(Exception):
    pass


def log_income(state, args):
    try:
        amount = round2(args.get("amount"))
    except (TypeError, ValueError):
        amount = 0
    if not amount or amount <= 0:
        raise ToolError("amount must be a positive number.")
    description = str(args.get("description") or "").strip()
    if not description:
        raise ToolError("description is required.")
    tx_date = args.get("date") or today_str()
    account_id = resolve_account_id(state, args.get("account"))
    cap_sum = sum(float(b.get("cap") or 0) for b in splittable_buckets(state))
    if cap_sum <= 0:
        raise ToolError("No bucket CAP percentages are set yet - set those up in the app first.")

    split = compute_income_split(state, amount)
    tx = {
        "id": uid(),
        "type": "income",
        "date": tx_date,
        "description": description,
        "notes": "Logged via Claude chat",
        "amount": amount,
        "materialsCost": 0,
        "accountId": account_id,
        "split": split,
        "pending": False,
        "historical": False,
        "recurring": None,
        "createdAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)
    bucket_lines = ", ".join(f"{bucket_label(state, bid)}: {amt:.2f}" for bid, amt in split.items())
    currency = state.get("currency", "")
    return f'Income logged: {amount:.2f} {currency} - "{description}" on {tx_date}. Split -> {bucket_lines}.'


def log_expense(state, args):
    try:
        amount = round2(args.get("amount"))
    except (TypeError, ValueError):
        amount = 0
    if not amount or amount <= 0:
        raise ToolError("amount must be a positive number.")
    description = str(args.get("description") or "").strip()
    if not description:
        raise ToolError("description is required.")
    if not args.get("bucket"):
        raise ToolError("bucket is required (e.g. Opex, Profit, Tax, Owner's Pay).")
    bucket_id = resolve_bucket_id(state, args.get("bucket"))
    if not bucket_id:
        names = ", ".join(b["name"] for b in state["buckets"])
        raise ToolError(f'Unknown bucket "{args.get("bucket")}". Available buckets: {names}')
    tx_date = args.get("date") or today_str()
    account_id = resolve_account_id(state, args.get("account"))

    tx = {
        "id": uid(),
        "type": "expense",
        "date": tx_date,
        "description": description,
        "amount": amount,
        "bucketId": bucket_id,
        "accountId": account_id,
        "affectsBucket": True,
        "affectsAccount": True,
        "historical": False,
        "createdAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)
    currency = state.get("currency", "")
    return f'Expense logged: {amount:.2f} {currency} from {bucket_label(state, bucket_id)} - "{description}" on {tx_date}.'


def list_transactions(state, args):
    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else 20
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))

    type_filter = args.get("type")
    bucket_id_filter = None
    if args.get("bucket"):
        bucket_id_filter = resolve_bucket_id(state, args["bucket"])
        if not bucket_id_filter:
            names = ", ".join(b["name"] for b in state["buckets"])
            raise ToolError(f'Unknown bucket "{args["bucket"]}". Available buckets: {names}')
    since = args.get("since")

    def matches(t):
        if type_filter and t.get("type") != type_filter:
            return False
        if bucket_id_filter:
            if t.get("type") == "income":
                if not (t.get("split") and bucket_id_filter in t["split"]):
                    return False
            elif t.get("bucketId") != bucket_id_filter:
                return False
        if since and t.get("date", "") < since:
            return False
        return True

    txs = [t for t in state["transactions"] if matches(t)]
    txs.sort(key=lambda t: (t.get("date", ""), t.get("createdAt", 0)), reverse=True)
    total_matches = len(txs)
    shown = txs[:limit]

    if not shown:
        return "No transactions match."

    currency = state.get("currency", "")

    def account_name(acc_id):
        return next((a["name"] for a in state["accounts"] if a["id"] == acc_id), acc_id or "")

    lines = []
    for t in shown:
        acc = account_name(t.get("accountId"))
        if t.get("type") == "income":
            lines.append(f'{t.get("date")} | income | +{t.get("amount", 0):.2f} {currency} | "{t.get("description", "")}" | {acc}')
        elif t.get("type") == "expense":
            b = bucket_label(state, t.get("bucketId"))
            lines.append(f'{t.get("date")} | expense | -{t.get("amount", 0):.2f} {currency} | {b} | "{t.get("description", "")}" | {acc}')
        else:
            lines.append(f'{t.get("date")} | {t.get("type")} | {t.get("amount", 0):.2f} {currency} | "{t.get("description", "")}"')

    header = f"Showing {len(shown)} of {total_matches} matching transaction(s), newest first:"
    return header + "\n" + "\n".join(lines)


def get_summary(state):
    totals = {b["id"]: 0.0 for b in state["buckets"]}
    for t in state["transactions"]:
        if t.get("type") == "income" and t.get("split"):
            for bid, amt in t["split"].items():
                if bid in totals:
                    totals[bid] += amt
        elif t.get("type") == "expense" and t.get("bucketId") and t.get("affectsBucket", True):
            if t["bucketId"] in totals:
                totals[t["bucketId"]] -= t["amount"]
        elif t.get("type") == "payout" and t.get("bucketId"):
            if t["bucketId"] in totals:
                totals[t["bucketId"]] -= t["amount"]

    currency = state.get("currency", "")
    lines = [
        f'{b["name"]}: {round2(totals.get(b["id"], 0)):.2f} {currency}'
        for b in state["buckets"]
        if b.get("tier") != "secondary"
    ]
    return (
        "Approximate balances (from raw splits - open the app for the exact live view, "
        "which also accounts for cross-bucket borrowing when a bucket runs negative):\n"
        + "\n".join(lines)
    )


# ---- MCP JSON-RPC plumbing ----------------------------------------------

TOOLS = [
    {
        "name": "log_income",
        "description": "Log a new income transaction. Splits it across the tracker's Profit First "
        "buckets using their current CAP percentages, same as logging it by hand in the app.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount received"},
                "description": {"type": "string", "description": "What this income was for / client name"},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "account": {
                    "type": "string",
                    "description": 'Account name it landed in, e.g. "Main Account". Defaults to the app\'s default account.',
                },
            },
            "required": ["amount", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_expense",
        "description": "Log a new expense transaction drawn from one Profit First bucket "
        "(e.g. Opex, Profit, Tax, Owner's Pay - or any custom bucket name in this tracker).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount spent"},
                "description": {"type": "string", "description": "What this expense was for / vendor"},
                "bucket": {"type": "string", "description": 'Which bucket to draw from, by name (e.g. "Opex")'},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "account": {
                    "type": "string",
                    "description": "Account it was paid from. Defaults to the app's default account.",
                },
            },
            "required": ["amount", "description", "bucket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_summary",
        "description": "Get approximate current balances for every primary Profit First bucket in this tracker.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_transactions",
        "description": "List individual transactions (newest first), optionally filtered by type, bucket, or a start date. Use this for specific questions like \"what did I spend on X\" or \"show my last few transactions\" - get_summary only gives bucket totals, not line items.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Max transactions to return, default 20, max 200"},
                "type": {"type": "string", "enum": ["income", "expense"], "description": "Filter to just income or just expenses"},
                "bucket": {"type": "string", "description": 'Filter to transactions touching one bucket, by name (e.g. "Opex")'},
                "since": {"type": "string", "description": "YYYY-MM-DD - only transactions on or after this date"},
            },
            "additionalProperties": False,
        },
    },
]


def handle_mcp(body):
    method = body.get("method")
    req_id = body.get("id")
    params = body.get("params") or {}

    if method == "notifications/initialized":
        return 202, None

    if method == "initialize":
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "profit-first-tracker", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        state = read_state()
        if not state:
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "No tracker data found yet - open the app once first."}],
                    "isError": True,
                },
            }
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "log_income":
                text = log_income(state, args)
            elif name == "log_expense":
                text = log_expense(state, args)
            elif name == "get_summary":
                text = get_summary(state)
            elif name == "list_transactions":
                text = list_transactions(state, args)
            else:
                raise ToolError(f"Unknown tool: {name}")
            write_state(state)
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except ToolError as e:
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        except Exception as e:
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Storage error, transaction not saved: {e}"}], "isError": True}}

    return 400, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


# ---- HTTP server ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body):
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {TOKEN}"

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self):
        self._send(204, None)

    def do_GET(self):
        # The tracker page itself is served with no auth check - a plain
        # page load has no way to send a Bearer header, and the page embeds
        # the same SYNC_TOKEN whether it's hosted here or opened as a local
        # file, so this doesn't expose anything new. Everything else below
        # (the actual data) stays behind the token.
        if self.path in ("/", "/index.html"):
            try:
                with open(TRACKER_HTML_FILE, "rb") as f:
                    html = f.read()
            except FileNotFoundError:
                return self._send(500, {"error": "tracker.html not found next to server.py"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return

        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/api/state":
            state = read_state()
            return self._send(200 if state else 404, state or {"error": "No state saved yet"})
        self._send(404, {"error": "Not found"})

    def do_PUT(self):
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/api/state":
            try:
                state = json.loads(self._body())
            except json.JSONDecodeError:
                return self._send(400, {"error": "Invalid JSON body"})
            try:
                write_state(state)
            except Exception as e:
                return self._send(502, {"error": f"Storage write failed: {e}"})
            return self._send(200, {"ok": True})
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/mcp":
            try:
                body = json.loads(self._body())
            except json.JSONDecodeError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            status, resp_body = handle_mcp(body)
            return self._send(status, resp_body)
        self._send(404, {"error": "Not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Tracker sync + MCP server listening on :{PORT}")
    server.serve_forever()def _upstash_request(method, path, body=None):
    req = urllib.request.Request(
        UPSTASH_URL.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Authorization": "Bearer " + UPSTASH_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def read_state():
    try:
        result = _upstash_request("GET", "/get/" + STATE_KEY)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print("Upstash read failed:", e, file=sys.stderr)
        return None
    value = result.get("result")
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def write_state(state):
    body = json.dumps(state).encode("utf-8")
    result = _upstash_request("POST", "/set/" + STATE_KEY, body=body)
    if result.get("result") != "OK":
        raise RuntimeError(f"Upstash write did not confirm OK: {result}")


def round2(v):
    return round(float(v) + 1e-9, 2)


def uid():
    return "tx_" + format(int(time.time() * 1000), "x") + "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def today_str():
    return date.today().isoformat()


# Mirrors profit-first-tracker.html's own splittableBuckets()/computeIncomeSplit()
# so chat-logged income lands in the same buckets, in the same proportions,
# as an entry made by hand in the app.
def splittable_buckets(state):
    return [b for b in state["buckets"] if b.get("tier") != "secondary"]


def compute_income_split(state, base):
    buckets = splittable_buckets(state)
    amounts = {}
    running = 0
    for i, b in enumerate(buckets):
        if i == len(buckets) - 1:
            amounts[b["id"]] = round2(base - running)
        else:
            a = round(base * (float(b.get("cap") or 0)) / 100)
            amounts[b["id"]] = a
            running += a
    return amounts


def resolve_account_id(state, account_name):
    if account_name:
        for a in state["accounts"]:
            if a["id"] == account_name or a["name"].lower() == str(account_name).lower():
                return a["id"]
    default_id = state.get("defaultAccountId")
    if default_id and any(a["id"] == default_id for a in state["accounts"]):
        return default_id
    return state["accounts"][0]["id"] if state["accounts"] else "cash"


def resolve_bucket_id(state, bucket_name):
    for b in state["buckets"]:
        if b["id"] == bucket_name or b["name"].lower() == str(bucket_name or "").lower():
            return b["id"]
    return None


def bucket_label(state, bucket_id):
    for b in state["buckets"]:
        if b["id"] == bucket_id:
            return b["name"]
    return bucket_id


# ---- MCP tool implementations ------------------------------------------

class ToolError(Exception):
    pass


def log_income(state, args):
    try:
        amount = round2(args.get("amount"))
    except (TypeError, ValueError):
        amount = 0
    if not amount or amount <= 0:
        raise ToolError("amount must be a positive number.")
    description = str(args.get("description") or "").strip()
    if not description:
        raise ToolError("description is required.")
    tx_date = args.get("date") or today_str()
    account_id = resolve_account_id(state, args.get("account"))
    cap_sum = sum(float(b.get("cap") or 0) for b in splittable_buckets(state))
    if cap_sum <= 0:
        raise ToolError("No bucket CAP percentages are set yet - set those up in the app first.")

    split = compute_income_split(state, amount)
    tx = {
        "id": uid(),
        "type": "income",
        "date": tx_date,
        "description": description,
        "notes": "Logged via Claude chat",
        "amount": amount,
        "materialsCost": 0,
        "accountId": account_id,
        "split": split,
        "pending": False,
        "historical": False,
        "recurring": None,
        "createdAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)
    bucket_lines = ", ".join(f"{bucket_label(state, bid)}: {amt:.2f}" for bid, amt in split.items())
    currency = state.get("currency", "")
    return f'Income logged: {amount:.2f} {currency} - "{description}" on {tx_date}. Split -> {bucket_lines}.'


def log_expense(state, args):
    try:
        amount = round2(args.get("amount"))
    except (TypeError, ValueError):
        amount = 0
    if not amount or amount <= 0:
        raise ToolError("amount must be a positive number.")
    description = str(args.get("description") or "").strip()
    if not description:
        raise ToolError("description is required.")
    if not args.get("bucket"):
        raise ToolError("bucket is required (e.g. Opex, Profit, Tax, Owner's Pay).")
    bucket_id = resolve_bucket_id(state, args.get("bucket"))
    if not bucket_id:
        names = ", ".join(b["name"] for b in state["buckets"])
        raise ToolError(f'Unknown bucket "{args.get("bucket")}". Available buckets: {names}')
    tx_date = args.get("date") or today_str()
    account_id = resolve_account_id(state, args.get("account"))

    tx = {
        "id": uid(),
        "type": "expense",
        "date": tx_date,
        "description": description,
        "amount": amount,
        "bucketId": bucket_id,
        "accountId": account_id,
        "affectsBucket": True,
        "affectsAccount": True,
        "historical": False,
        "createdAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)
    currency = state.get("currency", "")
    return f'Expense logged: {amount:.2f} {currency} from {bucket_label(state, bucket_id)} - "{description}" on {tx_date}.'


def list_transactions(state, args):
    limit = args.get("limit")
    try:
        limit = int(limit) if limit is not None else 20
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))

    type_filter = args.get("type")
    bucket_id_filter = None
    if args.get("bucket"):
        bucket_id_filter = resolve_bucket_id(state, args["bucket"])
        if not bucket_id_filter:
            names = ", ".join(b["name"] for b in state["buckets"])
            raise ToolError(f'Unknown bucket "{args["bucket"]}". Available buckets: {names}')
    since = args.get("since")

    def matches(t):
        if type_filter and t.get("type") != type_filter:
            return False
        if bucket_id_filter:
            if t.get("type") == "income":
                if not (t.get("split") and bucket_id_filter in t["split"]):
                    return False
            elif t.get("bucketId") != bucket_id_filter:
                return False
        if since and t.get("date", "") < since:
            return False
        return True

    txs = [t for t in state["transactions"] if matches(t)]
    txs.sort(key=lambda t: (t.get("date", ""), t.get("createdAt", 0)), reverse=True)
    total_matches = len(txs)
    shown = txs[:limit]

    if not shown:
        return "No transactions match."

    currency = state.get("currency", "")

    def account_name(acc_id):
        return next((a["name"] for a in state["accounts"] if a["id"] == acc_id), acc_id or "")

    lines = []
    for t in shown:
        acc = account_name(t.get("accountId"))
        if t.get("type") == "income":
            lines.append(f'{t.get("date")} | income | +{t.get("amount", 0):.2f} {currency} | "{t.get("description", "")}" | {acc}')
        elif t.get("type") == "expense":
            b = bucket_label(state, t.get("bucketId"))
            lines.append(f'{t.get("date")} | expense | -{t.get("amount", 0):.2f} {currency} | {b} | "{t.get("description", "")}" | {acc}')
        else:
            lines.append(f'{t.get("date")} | {t.get("type")} | {t.get("amount", 0):.2f} {currency} | "{t.get("description", "")}"')

    header = f"Showing {len(shown)} of {total_matches} matching transaction(s), newest first:"
    return header + "\n" + "\n".join(lines)


def get_summary(state):
    totals = {b["id"]: 0.0 for b in state["buckets"]}
    for t in state["transactions"]:
        if t.get("type") == "income" and t.get("split"):
            for bid, amt in t["split"].items():
                if bid in totals:
                    totals[bid] += amt
        elif t.get("type") == "expense" and t.get("bucketId") and t.get("affectsBucket", True):
            if t["bucketId"] in totals:
                totals[t["bucketId"]] -= t["amount"]
        elif t.get("type") == "payout" and t.get("bucketId"):
            if t["bucketId"] in totals:
                totals[t["bucketId"]] -= t["amount"]

    currency = state.get("currency", "")
    lines = [
        f'{b["name"]}: {round2(totals.get(b["id"], 0)):.2f} {currency}'
        for b in state["buckets"]
        if b.get("tier") != "secondary"
    ]
    return (
        "Approximate balances (from raw splits - open the app for the exact live view, "
        "which also accounts for cross-bucket borrowing when a bucket runs negative):\n"
        + "\n".join(lines)
    )


# ---- MCP JSON-RPC plumbing ----------------------------------------------

TOOLS = [
    {
        "name": "log_income",
        "description": "Log a new income transaction. Splits it across the tracker's Profit First "
        "buckets using their current CAP percentages, same as logging it by hand in the app.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount received"},
                "description": {"type": "string", "description": "What this income was for / client name"},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "account": {
                    "type": "string",
                    "description": 'Account name it landed in, e.g. "Main Account". Defaults to the app\'s default account.',
                },
            },
            "required": ["amount", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_expense",
        "description": "Log a new expense transaction drawn from one Profit First bucket "
        "(e.g. Opex, Profit, Tax, Owner's Pay - or any custom bucket name in this tracker).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount spent"},
                "description": {"type": "string", "description": "What this expense was for / vendor"},
                "bucket": {"type": "string", "description": 'Which bucket to draw from, by name (e.g. "Opex")'},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "account": {
                    "type": "string",
                    "description": "Account it was paid from. Defaults to the app's default account.",
                },
            },
            "required": ["amount", "description", "bucket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_summary",
        "description": "Get approximate current balances for every primary Profit First bucket in this tracker.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_transactions",
        "description": "List individual transactions (newest first), optionally filtered by type, bucket, or a start date. Use this for specific questions like \"what did I spend on X\" or \"show my last few transactions\" - get_summary only gives bucket totals, not line items.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Max transactions to return, default 20, max 200"},
                "type": {"type": "string", "enum": ["income", "expense"], "description": "Filter to just income or just expenses"},
                "bucket": {"type": "string", "description": 'Filter to transactions touching one bucket, by name (e.g. "Opex")'},
                "since": {"type": "string", "description": "YYYY-MM-DD - only transactions on or after this date"},
            },
            "additionalProperties": False,
        },
    },
]


def handle_mcp(body):
    method = body.get("method")
    req_id = body.get("id")
    params = body.get("params") or {}

    if method == "notifications/initialized":
        return 202, None

    if method == "initialize":
        return 200, {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "profit-first-tracker", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        state = read_state()
        if not state:
            return 200, {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "No tracker data found yet - open the app once first."}],
                    "isError": True,
                },
            }
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "log_income":
                text = log_income(state, args)
            elif name == "log_expense":
                text = log_expense(state, args)
            elif name == "get_summary":
                text = get_summary(state)
            elif name == "list_transactions":
                text = list_transactions(state, args)
            else:
                raise ToolError(f"Unknown tool: {name}")
            write_state(state)
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}}
        except ToolError as e:
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
        except Exception as e:
            return 200, {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": f"Storage error, transaction not saved: {e}"}], "isError": True}}

    return 400, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


# ---- HTTP server ---------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status, body):
        payload = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _authorized(self):
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {TOKEN}"

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self):
        self._send(204, None)

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/api/state":
            state = read_state()
            return self._send(200 if state else 404, state or {"error": "No state saved yet"})
        self._send(404, {"error": "Not found"})

    def do_PUT(self):
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/api/state":
            try:
                state = json.loads(self._body())
            except json.JSONDecodeError:
                return self._send(400, {"error": "Invalid JSON body"})
            try:
                write_state(state)
            except Exception as e:
                return self._send(502, {"error": f"Storage write failed: {e}"})
            return self._send(200, {"ok": True})
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if self.path == "/mcp":
            try:
                body = json.loads(self._body())
            except json.JSONDecodeError:
                return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            status, resp_body = handle_mcp(body)
            return self._send(status, resp_body)
        self._send(404, {"error": "Not found"})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Tracker sync + MCP server listening on :{PORT}")
    server.serve_forever()
