"""Step-27 smoke test: ONE trivial real Devin session via the v3 API.

Proves create/poll/structured-output against the live API and dumps every raw
response to state/smoke_raw.json so the mock can be synced to the real shapes.
Costs ~1 ACU. Hard-capped via max_acu_limit=2.
"""

import json
import os
import time
from pathlib import Path

import requests

from orchestrator import load_env_file

load_env_file()

API = f"https://api.devin.ai/v3/organizations/{os.environ['DEVIN_ORG_ID']}/sessions"
HEADERS = {"Authorization": f"Bearer {os.environ['DEVIN_API_KEY']}"}
REPO = os.environ.get("GITHUB_REPO", "madelinehpark/superset")

PROMPT = (
    "This is an API connectivity test. Do not clone any repository, do not "
    "modify any code, and do not open a pull request. Simply reply 'ping' and "
    "set the structured output to: pr_url='', status='ok', summary='pong'. "
    "Then finish the session."
)

raw_log = []

create = requests.post(
    API,
    headers=HEADERS,
    json={
        "prompt": PROMPT,
        "max_acu_limit": 1,
        "idempotent": True,
        "tags": ["api-smoke-test"],
        "structured_output_schema": {
            "type": "object",
            "properties": {
                "pr_url": {"type": "string"},
                "status": {"type": "string"},
                "summary": {"type": "string"},
            },
        },
    },
    timeout=30,
)
print("CREATE:", create.status_code)
create_data = create.json()
raw_log.append({"call": "create", "status_code": create.status_code, "body": create_data})
print(json.dumps(create_data, indent=2)[:800])
create.raise_for_status()

session_id = create_data.get("id") or create_data.get("session_id")
print(f"\nsession_id = {session_id}\nweb url    = {create_data.get('url', '(none)')}\n")

deadline = time.monotonic() + 30 * 60
delay = 10.0
while True:
    resp = requests.get(f"{API}/{session_id}", headers=HEADERS, timeout=30)
    data = resp.json()
    raw_log.append({"call": "get", "status_code": resp.status_code, "body": data})
    status = data.get("status_enum") or data.get("status")
    print(f"{time.strftime('%H:%M:%S')}  status={status}")
    if str(status).lower() in ("blocked", "finished"):
        print("\nFINAL raw get_session JSON (entire body):")
        print(json.dumps(data, indent=2))
        break
    if time.monotonic() > deadline:
        print("TIMEOUT after 30 min — check the session in the Devin app")
        break
    time.sleep(delay)
    delay = min(delay * 1.5, 30)

Path("state").mkdir(exist_ok=True)
Path("state/smoke_raw.json").write_text(json.dumps(raw_log, indent=2))
print(f"\nraw responses ({len(raw_log)} calls) -> state/smoke_raw.json")
