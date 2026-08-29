#!/usr/bin/env python3
"""Turn the AgentCore harness's cross-session memory on or off.

    python set_harness_memory.py --mode disabled   # isolate every session
    python set_harness_memory.py --mode managed    # restore the default
    python set_harness_memory.py --show            # just report current state

By default AgentCore attaches a *managed memory* resource to a harness. That
memory persists across `runtimeSessionId` values, so a "fresh" session can
still recall earlier conversations. Two consequences:

  1. The chatbot may report a ticket ID from a previous conversation instead
     of filing a new one (observed: a reply citing a ticket with no matching
     Lambda invocation and no new DynamoDB row).
  2. generate-eval-dataset.py claims a fresh session per test case "keeps
     every test independent". With managed memory attached that is not true,
     and eval scores pick up test-order effects.

Note the asymmetric API shape: CreateHarness takes `memory={...}` directly,
while UpdateHarness wraps it as `memory={"optionalValue": {...}}`.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import boto3

MODES = {
    "disabled": {"disabled": {}},
    "managed": {"managedMemoryConfiguration": {}},
}


def describe(acc, harness_id):
    h = acc.get_harness(harnessId=harness_id)["harness"]
    return h.get("status"), h.get("harnessVersion"), h.get("memory")


def wait_ready(acc, harness_id, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, version, memory = describe(acc, harness_id)
        if status == "READY":
            return status, version, memory
        if "FAILED" in (status or "") or status == "DELETING":
            sys.exit(f"Harness entered status {status}.")
        print(f"  status: {status} — waiting...")
        time.sleep(10)
    sys.exit("Timed out waiting for the harness.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=sorted(MODES))
    p.add_argument("--show", action="store_true",
                   help="Report the current memory config and exit.")
    p.add_argument("--config", default="agentcore_config.json")
    args = p.parse_args()

    if not args.show and not args.mode:
        p.error("pass --mode {disabled|managed} or --show")

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    acc = boto3.client("bedrock-agentcore-control", region_name=cfg["region"])
    harness_id = cfg["harness_id"]

    status, version, memory = describe(acc, harness_id)
    print(f"current: status={status} version={version}")
    print(f"current memory: {json.dumps(memory, default=str)}")

    if args.show:
        return

    print(f"\nSetting memory -> {args.mode} ...")
    acc.update_harness(
        harnessId=harness_id,
        memory={"optionalValue": MODES[args.mode]},
    )

    status, version, memory = wait_ready(acc, harness_id)
    print(f"\nharness is {status} (version {version})")
    print(f"new memory: {json.dumps(memory, default=str)}")
    print("\nStart a NEW chat.py session to test — an already-open session "
          "keeps its old behaviour.")


if __name__ == "__main__":
    main()
