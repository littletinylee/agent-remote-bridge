"""Ask the human one question through the hub, then wait for the answer.

Runs *on the hub host* (which can reach the hub over loopback). Used to ask the
owner something when the local bridge is offline - e.g. "please open a port".

    python3 scripts/hub_ask_once.py --code HUB1 --prompt "..." --option A --option B
"""

from __future__ import annotations

import argparse
import json
import ssl
import time
import urllib.error
import urllib.request
import uuid

CTX = ssl._create_unverified_context()


def post(base: str, path: str, payload: dict, timeout: float = 30) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"HTTP {exc.code} on {path}: {body}", flush=True)
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub", default="https://127.0.0.1:18443")
    ap.add_argument("--code", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--option", action="append", default=[])
    ap.add_argument("--timeout", type=float, default=900)
    args = ap.parse_args()

    name = "ops-console"
    token = "ops-" + uuid.uuid4().hex[:12]
    reg = post(args.hub, "/v1/register", {"name": name, "token": token})
    node_id = reg["node_id"]
    print(f"registered {name} -> {node_id}", flush=True)

    code = args.code.upper()
    prompt = args.prompt
    if args.option:
        prompt += "\n" + "\n".join(f"{i}) {o}" for i, o in enumerate(args.option, 1))
    post(args.hub, "/v1/question", {"node_id": node_id, "token": token, "code": code, "prompt": prompt})
    print(f"asked #{code}, waiting for the phone...", flush=True)

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        resp = post(args.hub, "/v1/poll", {"node_id": node_id, "token": token, "timeout": 25}, timeout=40)
        for msg in resp.get("messages") or []:
            print(f"ANSWERED: {json.dumps(msg, ensure_ascii=False)}", flush=True)
            return 0
    print("TIMEOUT: no answer", flush=True)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
