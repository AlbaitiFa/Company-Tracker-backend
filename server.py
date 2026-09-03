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

import calendar
import json
import math
import os
import sys
import time
import random
import string
import urllib.request
import urllib.error
from datetime import date, datetime
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


# Fields that travel together as one "Settings" bundle - whichever side has
# the newer stateUpdatedAt wins the whole bundle, since these aren't a list
# of independent items the way transactions are; there's no sane per-field
# merge for "the business name" or "the current bucket list".
_SETTINGS_FIELDS = [
    "business", "accounts", "buckets", "currency", "trackMaterials", "companyDebt", "owners",
    "reinvestPct", "distributionCadence", "fiscalYearStart", "distributionAnchorOverride",
    "milestoneMode", "milestoneAmount", "milestonePercent", "milestoneWindowStart",
    "milestoneAccumulated", "milestoneStepsApplied", "milestoneStepsLog", "defaultAccountId",
    "version",
]


def reconcile_state(base, incoming):
    """Merges two full state snapshots into one, symmetrically - either can
    be passed as base or incoming and the result is the same. This is what
    makes every write path safe against a stale write clobbering a newer
    one, instead of relying on whoever pushes last winning outright:

    - deletedTransactionIds: union of both sides.
    - transactions: per-id union; a transaction present on only one side is
      kept; present on both, the one with the later updatedAt (falling back
      to createdAt for old records) wins; a tombstoned id is dropped
      entirely regardless of which side has it.
    - the settings bundle (business/accounts/buckets/etc.): taken wholesale
      from whichever side has the newer stateUpdatedAt.

    Mirrors tracker.html's reconcileState() - keep both in sync.
    """
    result = dict(base)

    base_deleted = base.get("deletedTransactionIds") or []
    incoming_deleted = incoming.get("deletedTransactionIds") or []
    tombstones = set(base_deleted) | set(incoming_deleted)
    result["deletedTransactionIds"] = list(tombstones)

    merged = {}
    for t in base.get("transactions") or []:
        merged[t["id"]] = t
    for t in incoming.get("transactions") or []:
        existing = merged.get(t["id"])
        if not existing:
            merged[t["id"]] = t
            continue
        existing_time = existing.get("updatedAt") or existing.get("createdAt") or 0
        incoming_time = t.get("updatedAt") or t.get("createdAt") or 0
        if incoming_time > existing_time:
            merged[t["id"]] = t
    result["transactions"] = [t for tid, t in merged.items() if tid not in tombstones]

    base_stamp = base.get("stateUpdatedAt") or 0
    incoming_stamp = incoming.get("stateUpdatedAt") or 0
    settings_source = incoming if incoming_stamp > base_stamp else base
    for key in _SETTINGS_FIELDS:
        if key in settings_source:
            result[key] = settings_source[key]
    result["stateUpdatedAt"] = max(base_stamp, incoming_stamp)

    return result


def write_state(state):
    # Always reconcile against whatever's currently stored rather than
    # blindly overwriting - this is what makes every write path safe (the
    # HTTP PUT from the app's own sync, an MCP tool call, or a direct fix),
    # not just the client's own pull-before-push sequence. A write that
    # tries to silently drop a transaction by just not including it no
    # longer deletes it - deletion only happens through
    # deletedTransactionIds now (see delete_transaction).
    current = read_state()
    merged = reconcile_state(current, state) if current else state
    body = json.dumps(merged).encode("utf-8")
    result = _upstash_request("POST", "/set/" + STATE_KEY, body=body)
    if result.get("result") != "OK":
        raise RuntimeError(f"Upstash write did not confirm OK: {result}")


# Python's round() uses banker's rounding (.5 -> nearest even); JS's
# Math.round() always rounds .5 up. These two mirror
# profit-first-tracker.html's round2()/Math.round() exactly so a split like
# "5% of 250 = 12.5" rounds to 13 here too, not 12.
_JS_EPSILON = 2.220446049250313e-16


def js_round(x):
    if x >= 0:
        return math.floor(x + 0.5)
    return -math.floor(-x + 0.5)


def round2(v):
    v = float(v)
    return js_round((v + _JS_EPSILON) * 100) / 100


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
            a = js_round(base * (float(b.get("cap") or 0)) / 100)
            amounts[b["id"]] = a
            running += a
    return amounts


def default_account_id(state):
    default_id = state.get("defaultAccountId")
    if default_id and any(a["id"] == default_id for a in state["accounts"]):
        return default_id
    return state["accounts"][0]["id"] if state["accounts"] else "cash"


def find_account_id(state, account_name):
    """Strict lookup only - no fallback. Returns None if account_name doesn't
    match any real account, so callers can tell 'not given' apart from
    'given but wrong' and raise a clear error on the latter instead of
    silently substituting the default account (a real bug this caused once -
    a payout meant for a personal account landed in the company's main
    account because the name didn't match anything)."""
    for a in state["accounts"]:
        if a["id"] == account_name or a["name"].lower() == str(account_name).lower():
            return a["id"]
    return None


def resolve_account_id(state, account_name, tool_error_context=None):
    """account_name not given -> the app's default account, silently, as
    intended. account_name given but no match -> raises ToolError (unless
    tool_error_context is None, kept only for back-compat call sites that
    haven't been reviewed for strictness yet)."""
    if not account_name:
        return default_account_id(state)
    found = find_account_id(state, account_name)
    if found:
        return found
    if tool_error_context is None:
        return default_account_id(state)
    names = ", ".join(a["name"] for a in state["accounts"])
    raise ToolError(f'Unknown account "{account_name}" for {tool_error_context}. Available accounts: {names}')


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


def bucket_by_id(state, bucket_id):
    return next((b for b in state["buckets"] if b["id"] == bucket_id), None)


# ---- Derivation engine ---------------------------------------------------
# Faithful port of profit-first-tracker.html's computeDerived() (~line 701)
# and the milestone-stepping functions (~line 1413-1477). Both sides need to
# agree on this, or chat-logged and app-logged transactions render
# differently - if the app's version of these functions changes, mirror the
# change here too.

_FLOOR_BASE_PRIORITY = ["opex", "profit", "tax", "ownerspay"]


def floor_priority(state):
    order = list(_FLOOR_BASE_PRIORITY)
    for b in state["buckets"]:
        if b["id"] not in order and b.get("tier") == "secondary":
            order.append(b["id"])
    return order


def compute_derived(state):
    bucket_confirmed = {b["id"]: 0.0 for b in state["buckets"]}
    bucket_pending = {b["id"]: 0.0 for b in state["buckets"]}
    account_balances = {a["id"]: 0.0 for a in state["accounts"]}

    sorted_tx = sorted(state["transactions"], key=lambda t: (t.get("date", ""), t.get("createdAt", 0)))
    priority = floor_priority(state)
    tx_floor_notes = {}

    def draw_from_bucket(bucket_id, amount):
        if bucket_id not in bucket_confirmed:
            return None
        own_avail = max(bucket_confirmed[bucket_id], 0)
        take = min(own_avail, amount)
        bucket_confirmed[bucket_id] = round2(bucket_confirmed[bucket_id] - take)
        remaining = round2(amount - take)
        if remaining <= 0.004:
            return None
        shortfall = remaining
        covered_by = []
        for bid in priority:
            if bid == bucket_id:
                continue
            if remaining <= 0.004 or bid not in bucket_confirmed:
                continue
            avail = max(bucket_confirmed[bid], 0)
            pull = min(avail, remaining)
            if pull > 0:
                bucket_confirmed[bid] = round2(bucket_confirmed[bid] - pull)
                covered_by.append({"id": bid, "amount": pull})
                remaining = round2(remaining - pull)
        return {"shortfall": shortfall, "coveredBy": covered_by, "stillShort": remaining}

    for tx in sorted_tx:
        ttype = tx.get("type")
        if ttype == "income":
            if not tx.get("historical") and tx.get("accountId") in account_balances:
                account_balances[tx["accountId"]] += tx["amount"]
            for bid, amt in (tx.get("split") or {}).items():
                if bid not in bucket_confirmed:
                    continue
                if tx.get("pending"):
                    bucket_pending[bid] += amt
                else:
                    bucket_confirmed[bid] += amt
        elif ttype == "expense":
            if tx.get("affectsAccount") and not tx.get("historical") and tx.get("accountId") in account_balances:
                account_balances[tx["accountId"]] -= tx["amount"]
            if tx.get("affectsBucket") and tx.get("bucketId") in bucket_confirmed:
                r = draw_from_bucket(tx["bucketId"], tx["amount"])
                if r:
                    tx_floor_notes[tx["id"]] = r
        elif ttype == "transfer":
            if not tx.get("historical"):
                if tx.get("accountId") in account_balances:
                    account_balances[tx["accountId"]] -= tx["amount"]
                if tx.get("toAccountId") in account_balances:
                    account_balances[tx["toAccountId"]] += tx["amount"]
        elif ttype == "payout":
            if tx.get("bucketId") in bucket_confirmed:
                r = draw_from_bucket(tx["bucketId"], tx["amount"])
                if r:
                    tx_floor_notes[tx["id"]] = r
            if tx.get("toBucketId") and tx["toBucketId"] in bucket_confirmed:
                bucket_confirmed[tx["toBucketId"]] += tx["amount"]
            if not tx.get("historical"):
                if tx.get("toAccountId") and tx["toAccountId"] in account_balances:
                    account_balances[tx["toAccountId"]] += tx["amount"]
                if tx.get("fromAccountId") and tx["fromAccountId"] in account_balances:
                    account_balances[tx["fromAccountId"]] -= tx["amount"]
        elif ttype == "opening_balance":
            # Mirrors tracker.html's computeDerived() opening_balance branch -
            # pre-existing cash, not income: never counted here as revenue
            # (monthly_real_revenue only sums type "income"), just an
            # account-balance seed that lands directly in the bucket(s) it's
            # assigned to.
            if tx.get("accountId") in account_balances:
                account_balances[tx["accountId"]] += tx["amount"]
            for bid, amt in (tx.get("allocation") or {}).items():
                if bid in bucket_confirmed:
                    bucket_confirmed[bid] += amt

    return {
        "bucketConfirmed": bucket_confirmed,
        "bucketPending": bucket_pending,
        "accountBalances": account_balances,
        "txFloorNotes": tx_floor_notes,
    }


def month_start_str(date_str):
    return date_str[:7] + "-01"


def monthly_real_revenue(state):
    now = datetime.utcnow()
    month_start = f"{now.year:04d}-{now.month:02d}-01"
    total = 0.0
    for t in state["transactions"]:
        if t.get("type") == "income" and t.get("date", "") >= month_start:
            total += t["amount"] - (t.get("materialsCost") or 0)
    return round2(total)


def get_milestone_amount(state):
    if state.get("milestoneMode") == "percent":
        return round2((state.get("milestonePercent") or 0) / 100 * monthly_real_revenue(state))
    return state["milestoneAmount"] if state.get("milestoneAmount") is not None else 100000


def ensure_milestone_window_current(state):
    today_month_start = month_start_str(today_str())
    if state.get("milestoneWindowStart") != today_month_start:
        state["milestoneWindowStart"] = today_month_start
        state["milestoneStepsApplied"] = 0
        state["milestoneStepsLog"] = []
    state["milestoneAccumulated"] = monthly_real_revenue(state)


_INCREASE_PRIORITY = ["profit", "ownerspay", "tax", "opex"]
_DECREASE_PRIORITY = ["opex", "tax", "ownerspay", "profit"]


def apply_one_milestone_step(state):
    if not bucket_by_id(state, "opex"):
        return None
    target = None
    for bid in _INCREASE_PRIORITY:
        b = bucket_by_id(state, bid)
        if b and float(b.get("cap") or 0) < float(b.get("tap") or 0):
            target = b
            break
    if not target:
        return None
    source = None
    for bid in _DECREASE_PRIORITY:
        b = bucket_by_id(state, bid)
        if not b or b is target:
            continue
        if float(b.get("cap") or 0) > float(b.get("tap") or 0):
            source = b
            break
    if not source:
        return None
    source["cap"] = round2(float(source.get("cap") or 0) - 1)
    target["cap"] = round2(float(target.get("cap") or 0) + 1)
    return {"sourceId": source["id"], "targetId": target["id"]}


def check_milestone_steps(state, tx_real_revenue, tx_id):
    if not tx_real_revenue or tx_real_revenue <= 0:
        return 0
    milestone_amount = get_milestone_amount(state)
    if not milestone_amount or milestone_amount <= 0:
        return 0
    if not bucket_by_id(state, "opex"):
        return 0
    ensure_milestone_window_current(state)
    target_steps = int(state.get("milestoneAccumulated", 0) // milestone_amount)
    already = state.get("milestoneStepsApplied") or 0
    new_steps = target_steps - already
    applied = 0
    if new_steps > 0:
        if state.get("milestoneStepsLog") is None:
            state["milestoneStepsLog"] = []
        for _ in range(new_steps):
            step = apply_one_milestone_step(state)
            if step:
                applied += 1
                state["milestoneStepsLog"].append(
                    {"txId": tx_id, "sourceId": step["sourceId"], "targetId": step["targetId"]}
                )
        state["milestoneStepsApplied"] = already + applied
        if applied > 0:
            # Milestone steps mutate bucket.cap, which is part of the
            # settings bundle reconcile_state() decides by stateUpdatedAt -
            # without this, a step applied here could lose to a stale
            # settings push from elsewhere that never saw it.
            state["stateUpdatedAt"] = int(time.time() * 1000)
    return applied


def revert_milestone_steps_for_tx(state, tx_id):
    # Mirrors revertMilestoneStepsForTx() (~line 1483) - the app calls this
    # whenever an income transaction that triggered milestone steps is
    # deleted or has its amount edited, so those steps don't just stay
    # permanently applied against a transaction that no longer justifies them.
    log = state.get("milestoneStepsLog") or []
    if not log:
        return 0
    remaining = []
    reverted = 0
    for entry in log:
        if entry.get("txId") == tx_id:
            source = bucket_by_id(state, entry.get("sourceId"))
            target = bucket_by_id(state, entry.get("targetId"))
            if source:
                source["cap"] = round2(float(source.get("cap") or 0) + 1)
            if target:
                target["cap"] = round2(float(target.get("cap") or 0) - 1)
            reverted += 1
        else:
            remaining.append(entry)
    state["milestoneStepsLog"] = remaining
    if reverted:
        state["milestoneStepsApplied"] = max(0, (state.get("milestoneStepsApplied") or 0) - reverted)
        state["stateUpdatedAt"] = int(time.time() * 1000)
    return reverted


def milestone_status_text(state):
    # Mirrors renderMilestonePanel() (~line 1503) so this reads exactly like
    # the app's own "Milestone pacing" panel.
    milestone_amount = get_milestone_amount(state)
    if not milestone_amount or milestone_amount <= 0 or not bucket_by_id(state, "opex"):
        return "No milestone tracking configured."
    ensure_milestone_window_current(state)
    accumulated = state.get("milestoneAccumulated") or 0
    into_window = accumulated - (math.floor(accumulated / milestone_amount) * milestone_amount)
    remaining = round2(max(0, milestone_amount - into_window))
    steps_so_far = state.get("milestoneStepsApplied") or 0
    window_start = state.get("milestoneWindowStart")
    if window_start:
        y, m = window_start.split("-")[0], window_start.split("-")[1]
        window_note = f"{calendar.month_name[int(m)]} {y}"
    else:
        window_note = "No income logged yet"
    currency = state.get("currency", "")
    return (
        f"{window_note} - {steps_so_far} step{'s' if steps_so_far != 1 else ''} applied this window - "
        f"{remaining:.2f} {currency} of Real Revenue until the next automatic step."
    )


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
    account_id = resolve_account_id(state, args.get("account"), tool_error_context="log_income's account")
    cap_sum = sum(float(b.get("cap") or 0) for b in splittable_buckets(state))
    if cap_sum <= 0:
        raise ToolError("No bucket CAP percentages are set yet - set those up in the app first.")

    # Matches the app's own Add Transaction default: Pending is checked
    # unless told otherwise. Pending income shows as a "+amount pending"
    # badge on each bucket instead of landing in the confirmed balance,
    # until confirm_pending() (or the app's "Confirm all") clears it.
    pending = args.get("pending", True)

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
        "pending": bool(pending),
        "historical": False,
        "recurring": None,
        "createdAt": int(time.time() * 1000),
        "updatedAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)

    # Mirrors the app's income-save handler: milestone stepping fires right
    # after the split is computed, using the same real-revenue figure.
    steps_applied = check_milestone_steps(state, amount, tx["id"])

    bucket_lines = ", ".join(f"{bucket_label(state, bid)}: {amt:.2f}" for bid, amt in split.items())
    currency = state.get("currency", "")
    pending_note = " (pending - not yet confirmed)" if pending else ""
    msg = f'Income logged: {amount:.2f} {currency} - "{description}" on {tx_date}{pending_note}. Split -> {bucket_lines}.'
    if steps_applied > 0:
        msg += f" {steps_applied} CAP milestone step{'s' if steps_applied != 1 else ''} applied."
    return msg


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
    account_id = resolve_account_id(state, args.get("account"), tool_error_context="log_expense's account")
    affects_bucket = args.get("affects_bucket", True)
    affects_account = args.get("affects_account", True)

    tx = {
        "id": uid(),
        "type": "expense",
        "date": tx_date,
        "description": description,
        "amount": amount,
        "bucketId": bucket_id,
        "accountId": account_id,
        "affectsBucket": bool(affects_bucket),
        "affectsAccount": bool(affects_account),
        "historical": False,
        "createdAt": int(time.time() * 1000),
        "updatedAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)
    currency = state.get("currency", "")
    flags_note = ""
    if not affects_bucket:
        flags_note = f" (real cash left the account, but {bucket_label(state, bucket_id)}'s allocation stays intact)"
    elif not affects_account:
        flags_note = " (bucket allocation only, no real account balance change)"
    return f'Expense logged: {amount:.2f} {currency} from {bucket_label(state, bucket_id)} - "{description}" on {tx_date}{flags_note}.'


def log_payout(state, args):
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
        raise ToolError("bucket is required - which bucket this draw is against (e.g. Owner's Pay, Profit).")
    bucket_id = resolve_bucket_id(state, args.get("bucket"))
    if not bucket_id:
        names = ", ".join(b["name"] for b in state["buckets"])
        raise ToolError(f'Unknown bucket "{args.get("bucket")}". Available buckets: {names}')
    tx_date = args.get("date") or today_str()
    from_account_id = resolve_account_id(state, args.get("from_account"), tool_error_context="log_payout's from_account")
    to_account_id = None
    if args.get("to_account"):
        to_account_id = find_account_id(state, args["to_account"])
        if not to_account_id:
            names = ", ".join(a["name"] for a in state["accounts"])
            raise ToolError(f'Unknown account "{args["to_account"]}" for log_payout\'s to_account. Available accounts: {names}')

    tx = {
        "id": uid(),
        "type": "payout",
        "date": tx_date,
        "description": description,
        "amount": amount,
        "bucketId": bucket_id,
        "fromAccountId": from_account_id,
        "toAccountId": to_account_id,
        "toBucketId": None,
        "createdAt": int(time.time() * 1000),
        "updatedAt": int(time.time() * 1000),
    }
    state["transactions"].append(tx)
    currency = state.get("currency", "")
    dest = ""
    if to_account_id:
        to_name = next((a["name"] for a in state["accounts"] if a["id"] == to_account_id), to_account_id)
        dest = f" to {to_name}"
    return f'Payout logged: {amount:.2f} {currency} drawn from {bucket_label(state, bucket_id)}{dest} - "{description}" on {tx_date}.'


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
    derived = compute_derived(state)
    tx_floor_notes = derived["txFloorNotes"]

    def account_name(acc_id):
        return next((a["name"] for a in state["accounts"] if a["id"] == acc_id), acc_id or "")

    def notes_suffix(t):
        notes = (t.get("notes") or "").strip()
        return f' | notes: "{notes}"' if notes else ""

    def floor_warning(bucket_id, r):
        text = f"exceeded {bucket_label(state, bucket_id)}'s balance by {r['shortfall']:.2f} {currency}."
        if r["coveredBy"]:
            covered = ", ".join(f"{bucket_label(state, c['id'])} {c['amount']:.2f}" for c in r["coveredBy"])
            text += f" covered by {covered}."
        if r["stillShort"] > 0.004:
            text += f" {r['stillShort']:.2f} {currency} still short - no bucket had enough left."
        return f" | WARNING: {text}"

    lines = []
    for t in shown:
        acc = account_name(t.get("accountId"))
        tid = t.get("id", "")
        ttype = t.get("type")
        warning = floor_warning(t.get("bucketId"), tx_floor_notes[tid]) if tid in tx_floor_notes else ""
        if ttype == "income":
            pending_tag = " (pending)" if t.get("pending") else ""
            lines.append(
                f'[{tid}] {t.get("date")} | income | +{t.get("amount", 0):.2f} {currency}{pending_tag} | '
                f'"{t.get("description", "")}" | into {acc}{notes_suffix(t)}'
            )
        elif ttype == "expense":
            b = bucket_label(state, t.get("bucketId"))
            lines.append(
                f'[{tid}] {t.get("date")} | expense | -{t.get("amount", 0):.2f} {currency} | {b} | '
                f'"{t.get("description", "")}" | from {acc}{notes_suffix(t)}{warning}'
            )
        elif ttype == "transfer":
            to_acc = account_name(t.get("toAccountId"))
            lines.append(
                f'[{tid}] {t.get("date")} | transfer | {t.get("amount", 0):.2f} {currency} | '
                f'"{t.get("description", "")}" | {acc} → {to_acc}{notes_suffix(t)}'
            )
        elif ttype == "payout":
            b = bucket_label(state, t.get("bucketId"))
            from_acc = account_name(t.get("fromAccountId")) if t.get("fromAccountId") else None
            to_acc = account_name(t.get("toAccountId")) if t.get("toAccountId") else None
            to_bucket = bucket_label(state, t["toBucketId"]) if t.get("toBucketId") else None
            route = f"out of {b}"
            if from_acc:
                route += f" ({from_acc})"
            if to_bucket:
                route += f" → into {to_bucket}"
            elif to_acc:
                route += f" → into {to_acc}"
            lines.append(
                f'[{tid}] {t.get("date")} | payout | -{t.get("amount", 0):.2f} {currency} | '
                f'"{t.get("description", "")}" | {route}{notes_suffix(t)}{warning}'
            )
        elif ttype == "opening_balance":
            allocation = t.get("allocation") or {}
            alloc_str = ", ".join(f"{bucket_label(state, bid)} {amt:.2f}" for bid, amt in allocation.items())
            lines.append(
                f'[{tid}] {t.get("date")} | opening_balance | +{t.get("amount", 0):.2f} {currency} | '
                f'"{t.get("description", "")}" | into {acc}'
                + (f" | allocated: {alloc_str}" if alloc_str else "")
                + notes_suffix(t)
            )
        else:
            lines.append(
                f'[{tid}] {t.get("date")} | {ttype} | {t.get("amount", 0):.2f} {currency} | '
                f'"{t.get("description", "")}"{notes_suffix(t)}{warning}'
            )

    header = f"Showing {len(shown)} of {total_matches} matching transaction(s), newest first. [id] is what delete_transaction needs:"
    return header + "\n" + "\n".join(lines)


def confirm_pending(state, args):
    any_confirmed = False
    for t in state["transactions"]:
        if t.get("type") == "income" and t.get("pending"):
            t["pending"] = False
            any_confirmed = True
    if not any_confirmed:
        return "Nothing pending."
    return "All pending income confirmed."


def get_summary(state):
    derived = compute_derived(state)
    currency = state.get("currency", "")
    lines = []
    for b in state["buckets"]:
        if b.get("tier") == "secondary":
            continue
        confirmed = round2(derived["bucketConfirmed"].get(b["id"], 0))
        pending = round2(derived["bucketPending"].get(b["id"], 0))
        cap = b.get("cap")
        tap = b.get("tap")
        line = f'{b["name"]}: {confirmed:.2f} {currency} (CAP {cap}% / TAP {tap}%)'
        if pending:
            line += f" (+{pending:.2f} pending)"
        lines.append(line)
    return (
        "Current bucket balances (floor-and-cascade applied, same as the app):\n"
        + "\n".join(lines)
        + "\n\nMilestone pacing: "
        + milestone_status_text(state)
    )


def delete_transaction(state, args):
    tx_id = args.get("id")
    if not tx_id:
        raise ToolError("id is required - get it from list_transactions' [id] prefix.")
    confirm_description = str(args.get("confirm_description") or "").strip().lower()
    if not confirm_description:
        raise ToolError(
            "confirm_description is required - pass the transaction's exact description back, "
            "as a safety check that this is really the entry meant to be deleted."
        )
    match = next((t for t in state["transactions"] if t.get("id") == tx_id), None)
    if not match:
        raise ToolError(f'No transaction found with id "{tx_id}".')
    actual_description = str(match.get("description") or "").strip().lower()
    if confirm_description != actual_description:
        raise ToolError(
            f'confirm_description doesn\'t match - this transaction\'s description is "{match.get("description", "")}". '
            "Pass it back exactly to confirm you have the right one."
        )
    state["transactions"] = [t for t in state["transactions"] if t.get("id") != tx_id]
    # Tombstone the id so this deletion survives a stale client (a browser
    # tab, or any session) that still has the old entry cached and would
    # otherwise silently push it back on its next save. See tracker.html's
    # syncFromServer() for the client-side half of this.
    if state.get("deletedTransactionIds") is None:
        state["deletedTransactionIds"] = []
    if tx_id not in state["deletedTransactionIds"]:
        state["deletedTransactionIds"].append(tx_id)
    reverted = revert_milestone_steps_for_tx(state, tx_id)
    currency = state.get("currency", "")
    msg = f'Deleted: {match.get("amount", 0):.2f} {currency} - "{match.get("description", "")}" on {match.get("date", "")}.'
    if reverted:
        msg += f" {reverted} CAP milestone step{'s' if reverted != 1 else ''} reversed."
    return msg


def edit_transaction(state, args):
    tx_id = args.get("id")
    if not tx_id:
        raise ToolError("id is required - get it from list_transactions' [id] prefix.")
    confirm_description = str(args.get("confirm_description") or "").strip().lower()
    if not confirm_description:
        raise ToolError(
            "confirm_description is required - pass the transaction's current exact description "
            "back, as a safety check that this is really the entry meant to be edited."
        )
    tx = next((t for t in state["transactions"] if t.get("id") == tx_id), None)
    if not tx:
        raise ToolError(f'No transaction found with id "{tx_id}".')
    actual_description = str(tx.get("description") or "").strip().lower()
    if confirm_description != actual_description:
        raise ToolError(
            f'confirm_description doesn\'t match - this transaction\'s current description is "{tx.get("description", "")}". '
            "Pass it back exactly to confirm you have the right one."
        )

    changes = []

    if "amount" in args and args["amount"] is not None:
        try:
            new_amount = round2(args["amount"])
        except (TypeError, ValueError):
            new_amount = 0
        if not new_amount or new_amount <= 0:
            raise ToolError("amount must be a positive number.")
        if tx.get("type") == "income" and new_amount != tx.get("amount"):
            # Same sequence the app's own edit flow uses: undo whatever
            # milestone steps this transaction's old amount had triggered
            # before recomputing the split and rechecking steps against the
            # new amount - otherwise stale CAP shifts stick around forever.
            revert_milestone_steps_for_tx(state, tx_id)
        tx["amount"] = new_amount
        changes.append("amount")
        if tx.get("type") == "income":
            tx["split"] = compute_income_split(state, new_amount)
            changes.append("split")

    if "description" in args and args["description"] is not None:
        new_desc = str(args["description"]).strip()
        if not new_desc:
            raise ToolError("description can't be blank.")
        tx["description"] = new_desc
        changes.append("description")

    if "date" in args and args["date"]:
        tx["date"] = args["date"]
        changes.append("date")

    if "bucket" in args and args["bucket"] and tx.get("type") in ("expense", "payout"):
        bucket_id = resolve_bucket_id(state, args["bucket"])
        if not bucket_id:
            names = ", ".join(b["name"] for b in state["buckets"])
            raise ToolError(f'Unknown bucket "{args["bucket"]}". Available buckets: {names}')
        tx["bucketId"] = bucket_id
        changes.append("bucket")

    if "account" in args and args["account"] and tx.get("type") in ("income", "expense"):
        tx["accountId"] = resolve_account_id(state, args["account"], tool_error_context="edit_transaction's account")
        changes.append("account")

    if "from_account" in args and args["from_account"] and tx.get("type") == "payout":
        tx["fromAccountId"] = resolve_account_id(state, args["from_account"], tool_error_context="edit_transaction's from_account")
        changes.append("from_account")

    if "to_account" in args and args["to_account"] and tx.get("type") == "payout":
        found = find_account_id(state, args["to_account"])
        if not found:
            names = ", ".join(a["name"] for a in state["accounts"])
            raise ToolError(f'Unknown account "{args["to_account"]}" for edit_transaction\'s to_account. Available accounts: {names}')
        tx["toAccountId"] = found
        changes.append("to_account")

    if not changes:
        raise ToolError("Nothing to change - pass at least one field to update (amount, description, date, bucket, account, from_account, to_account).")

    tx["updatedAt"] = int(time.time() * 1000)

    steps_applied = 0
    if tx.get("type") == "income" and "amount" in changes:
        steps_applied = check_milestone_steps(state, tx["amount"], tx_id)

    currency = state.get("currency", "")
    msg = f'Updated: {tx.get("amount", 0):.2f} {currency} - "{tx.get("description", "")}" on {tx.get("date", "")}. Changed: {", ".join(c for c in changes if c != "split")}.'
    if steps_applied > 0:
        msg += f" {steps_applied} CAP milestone step{'s' if steps_applied != 1 else ''} applied."
    return msg


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
                "pending": {
                    "type": "boolean",
                    "description": "Defaults to true, matching the app. Set false only if this money is already fully allocated/wired - true means it shows as pending until confirm_pending is called.",
                },
            },
            "required": ["amount", "description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_expense",
        "description": "Log a new expense transaction drawn from one Profit First bucket "
        "(e.g. Opex, Profit, Tax, Owner's Pay - or any custom bucket name in this tracker). "
        "For a normal real business expense, leave affects_bucket/affects_account at their "
        "defaults (both true). Set affects_bucket false when real cash left an account but "
        "the bucket's allocation should stay intact (e.g. a personal draw not counted as a "
        "real business cost) - this is a judgment call, ask if unsure which applies.",
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
                "affects_bucket": {
                    "type": "boolean",
                    "description": "Default true. False if this shouldn't reduce the bucket's virtual allocation.",
                },
                "affects_account": {
                    "type": "boolean",
                    "description": "Default true. False if this is a bucket-only reclassification with no real cash movement.",
                },
            },
            "required": ["amount", "description", "bucket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "log_payout",
        "description": "Log a real draw/disbursement from a bucket that also moves actual money "
        "between two real accounts - e.g. an Owner's Pay draw wired out to a personal account. "
        "Different from log_expense: this represents genuine money movement between accounts, "
        "not a business cost.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Positive amount drawn"},
                "description": {"type": "string", "description": "What this draw was for"},
                "bucket": {"type": "string", "description": 'Which bucket this draw is against, by name (e.g. "Owner\'s Pay")'},
                "date": {"type": "string", "description": "YYYY-MM-DD, defaults to today"},
                "from_account": {"type": "string", "description": "Account the money left. Defaults to the app's default account."},
                "to_account": {"type": "string", "description": "Account the money landed in, e.g. a personal account name."},
            },
            "required": ["amount", "description", "bucket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_summary",
        "description": "Get current confirmed/pending balances, CAP/TAP percentages, and milestone pacing for every primary Profit First bucket - the same floor-and-cascade math the app itself uses, not an approximation.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_transactions",
        "description": "List individual transactions (newest first), optionally filtered by type, bucket, or a start date. Use this for specific questions like \"what did I spend on X\" or \"show my last few transactions\" - get_summary only gives bucket totals, not line items. Each line includes the real from/to account(s) or bucket(s) the money moved between, and a WARNING note when the transaction exceeded its bucket's balance and had to cascade from another bucket to cover it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Max transactions to return, default 20, max 200"},
                "type": {"type": "string", "enum": ["income", "expense", "payout", "transfer", "opening_balance"], "description": "Filter to just one transaction type"},
                "bucket": {"type": "string", "description": 'Filter to transactions touching one bucket, by name (e.g. "Opex")'},
                "since": {"type": "string", "description": "YYYY-MM-DD - only transactions on or after this date"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "confirm_pending",
        "description": "Confirm all pending income - moves every pending income split into each bucket's confirmed balance. Same as the app's \"Confirm all\" button. Use once Albaiti says a payment's split has actually been allocated or wired.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "delete_transaction",
        "description": "Permanently delete one transaction by id (from list_transactions' [id] prefix) - "
        "for an entry that shouldn't exist at all (a duplicate, something logged against the wrong "
        "tracker). For fixing a wrong field on an otherwise-real entry (wrong amount, wrong bucket, "
        "wrong account), use edit_transaction instead - it's safer and keeps the transaction's history. "
        "Requires echoing back the transaction's exact description as confirm_description - a safety "
        "check against deleting the wrong entry. Confirm with Albaiti before deleting anything real.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The transaction id, from list_transactions"},
                "confirm_description": {"type": "string", "description": "The transaction's exact description, echoed back to confirm this is the right one"},
            },
            "required": ["id", "confirm_description"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit_transaction",
        "description": "Correct a field on an existing transaction (amount, description, date, bucket, "
        "account) by id, from list_transactions' [id] prefix. Requires echoing back the transaction's "
        "current exact description as confirm_description - a safety check against editing the wrong "
        "entry. Editing an income transaction's amount recomputes its bucket split and re-checks CAP "
        "milestone stepping, same as the app itself. Only pass the fields you want to change.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "The transaction id, from list_transactions"},
                "confirm_description": {"type": "string", "description": "The transaction's current exact description, echoed back to confirm this is the right one"},
                "amount": {"type": "number", "description": "New amount, if changing it"},
                "description": {"type": "string", "description": "New description, if changing it"},
                "date": {"type": "string", "description": "New date (YYYY-MM-DD), if changing it"},
                "bucket": {"type": "string", "description": "New bucket, by name - expense/payout only"},
                "account": {"type": "string", "description": "New account, by name - income/expense only"},
                "from_account": {"type": "string", "description": "New source account, by name - payout only"},
                "to_account": {"type": "string", "description": "New destination account, by name - payout only"},
            },
            "required": ["id", "confirm_description"],
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
            elif name == "log_payout":
                text = log_payout(state, args)
            elif name == "get_summary":
                text = get_summary(state)
            elif name == "list_transactions":
                text = list_transactions(state, args)
            elif name == "confirm_pending":
                text = confirm_pending(state, args)
            elif name == "delete_transaction":
                text = delete_transaction(state, args)
            elif name == "edit_transaction":
                text = edit_transaction(state, args)
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
    server.serve_forever()
