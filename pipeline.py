import csv
import json
import os
import re
import time
from datetime import datetime

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

load_dotenv()

# ── credentials ──────────────────────────────────────────────────────────────
OUTCRAFT_EMAIL         = os.getenv("OUTCRAFT_EMAIL")
OUTCRAFT_PASSWORD      = os.getenv("OUTCRAFT_PASSWORD")
HUBSPOT_TOKEN          = os.getenv("HUBSPOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

OUTCRAFT_BASE = "https://app.outcraft.ai"
HUBSPOT_BASE  = "https://api.hubapi.com"

OUTPUT_FILE = "all_campaigns.csv"

SHEET_ID    = "1ByYID5jY0P_zwR_YL17sRk6hgZpKYX4Lo3LDPnWqgGY"
SHEET_TAB   = "List"

FIELDS = [
    "Source",
    "Name",
    "Tag",
    "Repeatable",
    "Trigger",
    "Trigger Field",
    "Trigger Field Value",
    "Stop Trigger Field Value",
    "Outreach Sequence",
    "Sequence IDs",
    "Created Date",
    "Is Enabled",
    "Link",
    "Type (Sequence/Workflow)",
    "Description",
    "Communication Included",
]

_DATE_FORMATS = [
    "%d-%m-%Y",   # 05-06-2026
    "%d-%m-%y",   # 19-06-26  →  2026-06-19
    "%Y-%m-%d",   # 2026-06-19 (already correct)
    "%d/%m/%Y",   # 05/06/2026
    "%d/%m/%y",   # 19/06/26
    "%m-%d-%Y",   # 06-05-2026
    "%m/%d/%Y",   # 06/05/2026
    "%m-%d-%y",   # 06-19-26
]

def parse_date(date_str):
    if not date_str:
        return ""
    date_str = date_str.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return date_str  # unrecognised format — return as-is

# ═══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

REPEATABLE_VALUES = {"System", "One-Time"}

def parse_campaign_name(raw_name):
    """
    'Sign Ups From 2025 [Sign Up, System, 05-06-2026]'
    -> name, tags_str, repeatable, seq_ids_str, date
    "System"/"One-Time" -> repeatable; purely digits -> sequence IDs;
    other items with letters -> tags; remainder -> date.
    """
    m = re.search(r'^(.*?)\s*\[([^\]]+)\]', raw_name.strip())
    if m:
        name  = m.group(1).strip()
        parts = [p.strip() for p in m.group(2).split(",")]
        repeatable = next((p for p in parts if p in REPEATABLE_VALUES), "")
        seq_ids    = [p for p in parts if re.match(r'^\d+$', p)]
        tags       = [p for p in parts if re.search(r'[a-zA-Z]', p) and p not in REPEATABLE_VALUES]
        other      = [p for p in parts if p not in tags and p not in seq_ids and p not in REPEATABLE_VALUES]
        date       = other[0] if other else ""
        return name, ", ".join(tags), repeatable, ", ".join(seq_ids), date
    return raw_name.strip(), "", "", "", ""


# ═══════════════════════════════════════════════════════════════════════════════
# OUTCRAFT SCRAPER
# ═══════════════════════════════════════════════════════════════════════════════

def oc_login(page):
    print("  [Outcraft] Logging in...")
    page.goto(f"{OUTCRAFT_BASE}/auth/login")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    page.wait_for_selector('input[type="email"], input[name="email"]', timeout=10000)
    page.fill('input[type="email"], input[name="email"]', OUTCRAFT_EMAIL)
    page.click('button:has-text("Continue"), button:has-text("Next"), button[type="submit"]')
    time.sleep(2)

    page.wait_for_selector('input[type="password"], input[name="password"]', timeout=10000)
    page.fill('input[type="password"], input[name="password"]', OUTCRAFT_PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle")
    time.sleep(3)
    print(f"  [Outcraft] Logged in, on: {page.url}")


def oc_get_campaign_list(page):
    print("  [Outcraft] Collecting campaign list...")
    page.goto(f"{OUTCRAFT_BASE}/campaigns")
    page.wait_for_load_state("networkidle")
    time.sleep(3)

    campaigns = []
    while True:
        for record in page.query_selector_all("div.fi-ta-record"):
            name_el = record.query_selector("div.fi-size-sm.fi-font-medium")
            name    = name_el.inner_text().strip() if name_el else ""
            link_el = record.query_selector("a.fi-ta-record-content")
            href    = link_el.get_attribute("href") if link_el else ""
            status_el = record.query_selector("span.fi-badge, [class*='badge']")
            status  = status_el.inner_text().strip() if status_el else ""
            m = re.search(r"/campaigns/(\d+)/", href)
            if m:
                campaigns.append({
                    "id":         m.group(1),
                    "name":       name,
                    "link":       OUTCRAFT_BASE + href,
                    "is_enabled": status.lower() == "running",
                })

        next_btn = page.query_selector('[aria-label="Next page"], button:has-text("Next")')
        if next_btn and next_btn.is_enabled():
            next_btn.click()
            page.wait_for_load_state("networkidle")
            time.sleep(2)
        else:
            break

    print(f"  [Outcraft] Found {len(campaigns)} campaigns")
    return campaigns


def oc_get_full_name(page):
    el = page.query_selector("span.text-xs.font-medium.truncate, span.font-medium.truncate")
    return el.inner_text().strip() if el else ""


def oc_scrape_hubspot_trigger(page, campaign_id):
    page.goto(f"{OUTCRAFT_BASE}/campaigns/{campaign_id}/onboarding/apps/hubspot")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    trigger_field = trigger_value = stop_value = ""
    try:
        btn = page.query_selector('button:has-text("How to connect HubSpot events")')
        if not btn:
            btn = page.get_by_text("How to connect HubSpot events").first
        if btn:
            btn.click()
            time.sleep(1.5)

            code_els = page.query_selector_all("code, pre, .font-mono, [class*='copy'], [class*='code']")
            texts = [el.inner_text().strip() for el in code_els if el.inner_text().strip()]
            for t in texts:
                if "outcraft_" in t or "_campaign_" in t:
                    trigger_field = t
                elif re.match(r"^\d+$", t):
                    trigger_value = t
                elif t.endswith("-stop"):
                    stop_value = t

            if not trigger_value:
                modal = page.query_selector('[role="dialog"], [class*="modal"], [class*="popup"], [class*="panel"]')
                if modal:
                    modal_text = modal.inner_text()
                    m = re.search(r"set\s+(\S+)\s+to[:\s]+(\S+)", modal_text)
                    if m:
                        trigger_field = m.group(1)
                        trigger_value = m.group(2)
                    m2 = re.search(r"force-stop value[:\s]+(\S+)", modal_text, re.IGNORECASE)
                    if m2:
                        stop_value = m2.group(1)
    except Exception as e:
        print(f"    WARNING: trigger extraction failed for {campaign_id}: {e}")

    full_name = oc_get_full_name(page)
    return full_name, trigger_field, trigger_value, stop_value


def oc_get_outreach_sequence(page, campaign_id):
    page.goto(f"{OUTCRAFT_BASE}/campaigns/{campaign_id}/onboarding/campaign/sequence")
    page.wait_for_load_state("networkidle")
    time.sleep(2)

    steps = []
    for row in page.query_selector_all("tr.fi-ta-row"):
        cells = row.query_selector_all("td")
        if len(cells) < 3:
            continue
        channel = cells[0].inner_text().strip()
        delay   = cells[1].inner_text().strip()
        action_el = cells[2].query_selector("span.font-semibold")
        action  = action_el.inner_text().strip() if action_el else cells[2].inner_text().strip()
        steps.append(f"{channel} / {delay} / {action}")

    return " | ".join(steps)


def run_outcraft():
    print("\n── OUTCRAFT ─────────────────────────────────────────────────────────")
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page    = browser.new_page()

        oc_login(page)
        campaign_list = oc_get_campaign_list(page)

        for i, c in enumerate(campaign_list):
            cid = c["id"]
            print(f"  [{i+1}/{len(campaign_list)}] Campaign {cid}: {c['name']}")

            full_name, tf, tv, stop = oc_scrape_hubspot_trigger(page, cid)
            outreach = oc_get_outreach_sequence(page, cid)

            raw_name = full_name or c["name"]
            name, tag, repeatable, seq_ids_str, created = parse_campaign_name(raw_name)

            rows.append({
                "Source":                   "Outcraft",
                "Name":                     name,
                "Tag":                      tag,
                "Repeatable":               repeatable,
                "Trigger":                  "Changed field",
                "Trigger Field":            tf,
                "Trigger Field Value":      tv,
                "Stop Trigger Field Value": stop,
                "Outreach Sequence":        outreach,
                "Sequence IDs":             seq_ids_str,
                "Created Date":             parse_date(created),
                "Is Enabled":               c.get("is_enabled", False),
                "Link":                     c["link"],
                "Type (Sequence/Workflow)": "Sequence",
                "Description":              "",
                "Communication Included":   True,
            })
            print(f"    Name={name} | Tags={tag} | Repeatable={repeatable} | SeqIDs={seq_ids_str} | TF={tf}={tv} | Created={created}")
            time.sleep(0.3)

        browser.close()

    print(f"  [Outcraft] Done — {len(rows)} campaigns")
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# HUBSPOT API
# ═══════════════════════════════════════════════════════════════════════════════

hs_headers = {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type":  "application/json",
}

BUILTIN_ACTION_NAMES = {
    "0-1":         "Delay",
    "0-2":         "Branch (if/then)",
    "0-3":         "Create task",
    "0-4":         "Send marketing email",
    "0-5":         "Set property value",
    "0-6":         "Delay",
    "0-7":         "Create deal",
    "0-8":         "Send internal email notification",
    "0-9":         "Rotate to owner",
    "0-10":        "Create ticket",
    "0-11":        "Create quote",
    "0-12":        "Trigger a webhook",
    "0-14":        "Create record",
    "0-18":        "Send SMS",
    "0-31":        "Set contact marketing status",
    "0-4702372":   "Unenroll from sequence",
    "0-18224765":  "Unenroll from workflow",
    "0-40900952":  "Enroll in marketing campaign",
    "0-43347357":  "Update communication subscription",
    "0-46510720":  "Enroll in sequence",
    "0-63809083":  "Add to active list",
    "0-230189361": "Send WhatsApp message",
    "1-2796901":   "Add row to Google Sheets",
    "1-76528805":  "Send HTTP webhook",
    "1-179507819": "Send Slack notification",
    "1-219033748": "Outcraft AI call",
}


def hs_fetch_all_workflows():
    workflows = []
    after = None
    while True:
        params = {"limit": 100}
        if after:
            params["after"] = after
        resp = requests.get(f"{HUBSPOT_BASE}/automation/v4/flows", headers=hs_headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        workflows.extend(data.get("results", []))
        print(f"    Fetched {len(workflows)} workflows so far...")
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.1)
    return workflows


def hs_fetch_detail(flow_id):
    resp = requests.get(f"{HUBSPOT_BASE}/automation/v4/flows/{flow_id}", headers=hs_headers)
    resp.raise_for_status()
    return resp.json()


_sequence_cache = {}


def hs_fetch_sequence(sequence_id, user_id):
    """GET /automation/v4/sequences/{sequenceId} — cached, since the same
    sequence is often reused by several workflows."""
    if sequence_id in _sequence_cache:
        return _sequence_cache[sequence_id]
    resp = requests.get(
        f"{HUBSPOT_BASE}/automation/v4/sequences/{sequence_id}",
        headers=hs_headers,
        params={"userId": user_id},
    )
    data = resp.json() if resp.status_code == 200 else None
    _sequence_cache[sequence_id] = data
    time.sleep(0.1)
    return data


def hs_get_sequence_email_steps(sequence_id, user_id):
    """
    Returns [(cumulative_delay_minutes, label), ...] for each EMAIL step in
    the sequence, in order. TASK / FINISH_ENROLLMENT steps are internal to
    the sequence (not contact-facing) and are skipped.
    """
    seq = hs_fetch_sequence(sequence_id, user_id)
    if not seq:
        return None

    seq_name = seq.get("name") or f"Sequence {sequence_id}"
    steps = []
    cumulative_ms = 0
    for step in seq.get("steps", []):
        cumulative_ms += step.get("delayMillis") or 0
        if step.get("actionType") == "EMAIL":
            order = step.get("stepOrder", len(steps))
            steps.append((cumulative_ms // 60000, f"{seq_name} — email {order + 1}"))
    return steps


def hs_fetch_action_name(action_type_id):
    if action_type_id in BUILTIN_ACTION_NAMES:
        return BUILTIN_ACTION_NAMES[action_type_id]
    try:
        app_id, definition_id = action_type_id.split("-", 1)
    except ValueError:
        return None
    if app_id == "0":
        return None
    resp = requests.get(
        f"{HUBSPOT_BASE}/automation/v4/actions/{app_id}/{definition_id}",
        headers=hs_headers,
    )
    if resp.status_code in (403, 404):
        return None
    resp.raise_for_status()
    data = resp.json()
    for locale_data in data.get("labels", {}).values():
        if isinstance(locale_data, dict):
            name = locale_data.get("actionName") or locale_data.get("stepTitle")
            if name:
                return name
    return None


def hs_collect_type_ids(obj, found=None):
    if found is None:
        found = set()
    if isinstance(obj, dict):
        if "actionTypeId" in obj:
            found.add(obj["actionTypeId"])
        for v in obj.values():
            hs_collect_type_ids(v, found)
    elif isinstance(obj, list):
        for item in obj:
            hs_collect_type_ids(item, found)
    return found


def hs_inject_names(obj, lookup):
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            if k == "actionTypeName":
                continue
            new[k] = hs_inject_names(v, lookup)
            if k == "actionTypeId" and v in lookup:
                new["actionTypeName"] = lookup[v]
        return new
    elif isinstance(obj, list):
        return [hs_inject_names(item, lookup) for item in obj]
    return obj


# ── HubSpot workflow → CSV row ────────────────────────────────────────────────

NAME_CHANNEL = {
    "Send marketing email":  "Email",
    "Enroll in sequence":    "Email",
    "Outcraft AI call":      "Call",
    "Send WhatsApp message": "WhatsApp",
    "Send SMS":              "SMS",
}
ID_CHANNEL = {
    "0-4":         "Email",
    "0-46510720":  "Email",
    "1-219033748": "Call",
    "0-230189361": "WhatsApp",
    "0-18":        "SMS",
}


def hs_format_delay(fields):
    delta_str = fields.get("delta", "")
    time_unit = fields.get("time_unit", "MINUTES").upper()
    if not delta_str:
        return "-"
    try:
        delta = int(delta_str)
    except ValueError:
        return delta_str
    if time_unit == "MINUTES":
        if delta == 0:
            return "-"
        if delta % 1440 == 0:
            d = delta // 1440
            return f"{d} day{'s' if d != 1 else ''}"
        if delta % 60 == 0:
            h = delta // 60
            return f"{h} hour{'s' if h != 1 else ''}"
        return f"{delta} minutes"
    if time_unit == "HOURS":
        return f"{delta} hour{'s' if delta != 1 else ''}"
    if time_unit == "DAYS":
        return f"{delta} day{'s' if delta != 1 else ''}"
    return f"{delta} {time_unit.lower()}"


def _to_minutes(fields):
    """Convert delay fields to total minutes (for sorting)."""
    try:
        delta = int(fields.get("delta", 0))
    except (ValueError, TypeError):
        return 0
    unit = fields.get("time_unit", "MINUTES").upper()
    if unit == "HOURS":
        return delta * 60
    if unit == "DAYS":
        return delta * 1440
    return delta  # MINUTES


def _minutes_to_str(minutes):
    """Format a minute count as a human-readable delay string."""
    if minutes <= 0:
        return "-"
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} day{'s' if d != 1 else ''}"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{minutes} minutes"


def hs_get_channel(action):
    return NAME_CHANNEL.get(action.get("actionTypeName", "")) or ID_CHANNEL.get(action.get("actionTypeId", ""))


def hs_get_step_label(action):
    type_name = action.get("actionTypeName", "")
    type_id   = action.get("actionTypeId", "")
    fields    = action.get("fields", {}) or {}
    if type_id == "0-46510720" or type_name == "Enroll in sequence":
        seq_id = fields.get("sequenceId", "")
        return f"Sequence {seq_id}" if seq_id else "Enroll in Sequence"
    if type_id == "1-219033748" or type_name == "Outcraft AI call":
        return "AI Call"
    if type_id == "0-230189361" or type_name == "Send WhatsApp message":
        return "WhatsApp Message"
    if type_id == "0-4" or type_name == "Send marketing email":
        content_id = fields.get("content_id", "")
        return f"Email {content_id}" if content_id else "Marketing Email"
    return type_name or type_id or "Unknown"


def hs_get_action_flow(wf):
    """
    Walk a single path through the action graph: at every IF/THEN fork
    (staticBranches / listBranches), follow only the first ("left") branch
    and ignore the alternatives. Those branches are mutually exclusive at
    runtime for a given contact (e.g. "has mobilephone" vs "has phone" call
    fallback), so exploring every branch double-counts communications that
    a contact only ever experiences one way.
    """
    actions_list = wf.get("actions", [])
    if not actions_list:
        return ""

    action_map = {str(a["actionId"]): a for a in actions_list if "actionId" in a}

    # Find the start action: the one no other action points to.
    pointed_to = set()
    for a in actions_list:
        for key in ("connection", "defaultBranch"):
            nxt = (a.get(key) or {}).get("nextActionId")
            if nxt:
                pointed_to.add(str(nxt))
        for branch_key in ("staticBranches", "listBranches"):
            for sb in (a.get(branch_key) or []):
                nxt = (sb.get("connection") or {}).get("nextActionId")
                if nxt:
                    pointed_to.add(str(nxt))

    candidates = [aid for aid in action_map if aid not in pointed_to]
    start_id = (
        sorted(candidates, key=lambda x: int(x) if x.isdigit() else 9999)[0]
        if candidates else sorted(action_map)[0]
    )

    def first_branch_next(action):
        if action.get("connection"):
            nxt = action["connection"].get("nextActionId")
            if nxt:
                return str(nxt)
        for branch_key in ("staticBranches", "listBranches"):
            branches = action.get(branch_key) or []
            if branches:
                nxt = (branches[0].get("connection") or {}).get("nextActionId")
                if nxt:
                    return str(nxt)
        if action.get("defaultBranch"):
            nxt = action["defaultBranch"].get("nextActionId")
            if nxt:
                return str(nxt)
        return None

    result = []
    current_id = start_id
    visited = set()
    prev_delay = 0
    cumulative_delay = 0
    while current_id and current_id not in visited:
        visited.add(current_id)
        action = action_map.get(current_id)
        if not action:
            break

        fields = action.get("fields", {}) or {}
        if "delta" in fields and "time_unit" in fields:
            cumulative_delay += _to_minutes(fields)

        type_id = action.get("actionTypeId", "")
        type_name = action.get("actionTypeName", "")
        seq_steps = None
        if type_id == "0-46510720" or type_name == "Enroll in sequence":
            seq_id, user_id = fields.get("sequenceId"), fields.get("userId")
            if seq_id and user_id:
                seq_steps = hs_get_sequence_email_steps(seq_id, user_id)

        if seq_steps is not None:
            # Expand the sequence into its real email steps instead of a
            # single generic placeholder. The sequence runs on its own
            # independent timeline once enrolled, so its steps are offset
            # from the enrollment moment but don't push out this workflow's
            # own subsequent delay.
            for step_delay, label in seq_steps:
                step_absolute = cumulative_delay + step_delay
                diff = step_absolute - prev_delay
                result.append(f"Email / {_minutes_to_str(diff)} / {label}")
                prev_delay = step_absolute
        else:
            channel = hs_get_channel(action)
            if channel:
                label = hs_get_step_label(action)
                diff = cumulative_delay - prev_delay
                result.append(f"{channel} / {_minutes_to_str(diff)} / {label}")
                prev_delay = cumulative_delay

        current_id = first_branch_next(action)

    return " | ".join(result)


_HS_TRIGGER_LABELS = {
    "EVENT_BASED":     "Property changed",
    "LIST_BASED":      "List membership",
    "MANUAL":          "Manual enrollment",
    "FORM_SUBMISSION": "Form submission",
    "SCHEDULE":        "Scheduled",
    "PAGE_VIEW":       "Page view",
}


def hs_get_trigger(enrollment_criteria):
    """Return (trigger_label, trigger_field, trigger_value)."""
    if not enrollment_criteria:
        return "", "", ""
    criteria_type = enrollment_criteria.get("type", "")
    label = _HS_TRIGGER_LABELS.get(criteria_type, criteria_type)

    if criteria_type == "EVENT_BASED":
        for branch in enrollment_criteria.get("eventFilterBranches", []):
            hs_name_val = hs_value_val = ""
            for f in branch.get("filters", []):
                if f.get("property") == "hs_name":
                    hs_name_val = f.get("operation", {}).get("value", "")
                elif f.get("property") == "hs_value":
                    op = f.get("operation", {})
                    hs_value_val = ", ".join(op.get("values", [str(op.get("value", ""))]))
            if hs_name_val:
                return label, hs_name_val, hs_value_val

    elif criteria_type == "LIST_BASED":
        branch = enrollment_criteria.get("listFilterBranch", {})
        for fb in branch.get("filterBranches", []):
            for f in fb.get("filters", []):
                prop = f.get("property", "")
                op   = f.get("operation", {})
                val  = ", ".join(op.get("values", [str(op.get("value", ""))]))
                if prop:
                    return label, prop, val

    elif criteria_type == "MANUAL":
        return label, "", ""

    return label, "", ""


def hs_parse_name(raw_name):
    """
    'COMMUNICATION AUTOMATION Failed Payments [Failed Payment, 304019637, ...]'
    -> name, tags_str, seq_ids
    Items with letters -> tags (joined by ", "); purely digits -> sequence IDs.
    """
    prefix = "COMMUNICATION AUTOMATION"
    after  = raw_name[len(prefix):].strip()
    m = re.search(r'\[([^\]]*)', after)
    if m:
        name    = after[:m.start()].strip()
        parts   = [p.strip() for p in m.group(1).split(",")]
        tags    = [p for p in parts if re.search(r'[a-zA-Z]', p)]
        seq_ids = [p for p in parts if re.match(r'^\d+$', p)]
    else:
        name    = after
        tags    = []
        seq_ids = []
    return name, ", ".join(tags), seq_ids


def run_hubspot():
    print("\n── HUBSPOT ──────────────────────────────────────────────────────────")

    # 1. Fetch all workflow stubs
    print("  Fetching workflow list...")
    stubs = hs_fetch_all_workflows()
    print(f"  Total: {len(stubs)} workflows")

    # 2. Fetch full details
    print("  Fetching workflow details...")
    workflows = []
    for i, stub in enumerate(stubs):
        flow_id = stub["id"]
        print(f"  [{i+1}/{len(stubs)}] {flow_id}: {stub.get('name','')}")
        try:
            workflows.append(hs_fetch_detail(flow_id))
        except requests.HTTPError as e:
            print(f"    ERROR {e.response.status_code}")
            workflows.append({**stub, "error": str(e)})
        time.sleep(0.1)

    # 3. Resolve actionTypeId → actionTypeName
    print("  Resolving action type names...")
    ids    = hs_collect_type_ids(workflows)
    lookup = {}
    for action_type_id in sorted(ids):
        name = hs_fetch_action_name(action_type_id)
        lookup[action_type_id] = name or action_type_id
        time.sleep(0.1)
    workflows = hs_inject_names(workflows, lookup)
    print(f"  Resolved {len(lookup)} action type IDs")

    # 4. Build rows for all workflows; flag those under COMMUNICATION AUTOMATION
    rows = []
    for wf in workflows:
        raw_name = wf.get("name", "")
        is_comm_auto = raw_name.upper().startswith("COMMUNICATION AUTOMATION")

        if is_comm_auto:
            name, tag, seq_ids = hs_parse_name(raw_name)
        else:
            name, tag, _, seq_ids_str, _ = parse_campaign_name(raw_name)
            seq_ids = [s.strip() for s in seq_ids_str.split(",") if s.strip()]

        trigger, trigger_field, trigger_value = hs_get_trigger(wf.get("enrollmentCriteria"))
        outreach = hs_get_action_flow(wf)
        wf_id    = wf.get("id", "")

        rows.append({
            "Source":                   "HubSpot",
            "Name":                     name,
            "Tag":                      tag,
            "Repeatable":               "",
            "Trigger":                  trigger,
            "Trigger Field":            trigger_field,
            "Trigger Field Value":      trigger_value,
            "Stop Trigger Field Value": "",
            "Outreach Sequence":        outreach,
            "Sequence IDs":             ", ".join(seq_ids),
            "Created Date":             (wf.get("createdAt") or "").split("T")[0],
            "Is Enabled":               wf.get("isEnabled", ""),
            "Link":                     f"https://app.hubspot.com/workflows/19511446/platform/flow/{wf_id}/edit",
            "Type (Sequence/Workflow)": "Workflow",
            "Description":              wf.get("description") or "",
            "Communication Included":   is_comm_auto,
        })
        print(f"    {name} | tag={tag} | comm_auto={is_comm_auto} | trigger={trigger} | {trigger_field}={trigger_value}")

    print(f"  [HubSpot] Done — {len(rows)} workflows ({sum(1 for r in rows if r['Communication Included'])} COMMUNICATION AUTOMATION)")
    return rows


# ═══════════════════════════════════════════════════════════════════════════════
# MERGE & SAVE
# ═══════════════════════════════════════════════════════════════════════════════

def save_csv(rows):
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n✓ Saved {len(rows)} rows to {OUTPUT_FILE}")


def save_sheet(rows):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = Credentials.from_service_account_info(json.loads(GOOGLE_CREDENTIALS_JSON), scopes=scopes)
    client = gspread.authorize(creds)

    sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_TAB)

    data = [FIELDS] + [[str(row.get(f, "")) for f in FIELDS] for row in rows]
    sheet.clear()
    sheet.update(data, "A1")

    print(f"\n✓ Written {len(rows)} rows to Google Sheet '{SHEET_TAB}'")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    outcraft_rows = run_outcraft()
    hubspot_rows  = run_hubspot()

    all_rows = outcraft_rows + hubspot_rows
    save_csv(all_rows)
    save_sheet(all_rows)


if __name__ == "__main__":
    main()
