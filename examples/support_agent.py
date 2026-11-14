"""Two versions of the same support agent, and what fault injection says about each.

Run:  python examples/support_agent.py

The "model" here is a small rule-based function so the example needs no API key.
In a real project you wrap your client call instead, e.g.
``llm = session.wrap(client.messages.create, name="llm")``.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from toolchaos import Session, resilience_matrix, to_markdown

ORDERS = {"rahim": [{"id": "A-1042", "item": "keyboard", "days_since_delivery": 5, "price": 3200}]}


# ---- real dependencies (recorded once, never called again in tests)
def search_orders(customer: str) -> list[dict]:
    return ORDERS.get(customer.lower(), [])


def refund_policy() -> dict:
    return {"window_days": 7, "restocking_fee_percent": 0}


def llm(prompt: str) -> str:
    """Stand-in for a chat model that must answer with a JSON decision."""
    facts = json.loads(prompt.split("FACTS:", 1)[1])
    eligible = facts["days_since_delivery"] <= facts["window_days"]
    return json.dumps({"decision": "refund" if eligible else "deny", "order": facts["order"]})


# ---- agent 1: the version most prototypes ship with
def naive_agent(session: Session, customer: str = "Rahim") -> str:
    orders = session.wrap(search_orders)(customer)
    policy = session.wrap(refund_policy)()
    order = orders[0]
    facts = {"order": order["id"], "days_since_delivery": order["days_since_delivery"], "window_days": policy["window_days"]}
    decision = json.loads(session.wrap(llm)("Decide. FACTS:" + json.dumps(facts)))
    return f"{decision['decision']} {decision['order']}"


# ---- agent 2: retries transient failures, validates every result, falls back to a human
ESCALATE = "escalate to human"


def call(fn, *args, validate=lambda result: True, attempts: int = 3):
    for _ in range(attempts):
        try:
            result = fn(*args)
        except Exception:
            continue
        if validate(result):
            return result
    return None


def parse_decision(text: object) -> dict | None:
    try:
        data = json.loads(text) if isinstance(text, str) else None
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("decision") in ("refund", "deny") and data.get("order") else None


def robust_agent(session: Session, customer: str = "Rahim") -> str:
    is_orders = lambda r: isinstance(r, list) and r and all(isinstance(o, dict) and {"id", "days_since_delivery"} <= o.keys() for o in r)
    orders = call(session.wrap(search_orders), customer, validate=is_orders)
    policy = call(session.wrap(refund_policy), validate=lambda r: isinstance(r, dict) and isinstance(r.get("window_days"), int))
    if not orders or not policy:
        return ESCALATE
    order = orders[0]
    facts = {"order": order["id"], "days_since_delivery": order["days_since_delivery"], "window_days": policy["window_days"]}
    reply = call(session.wrap(llm), "Decide. FACTS:" + json.dumps(facts), validate=lambda r: parse_decision(r) is not None)
    decision = parse_decision(reply)
    return f"{decision['decision']} {decision['order']}" if decision else ESCALATE


def main() -> str:
    cassette = Path(tempfile.mkdtemp()) / "support.jsonl"
    with Session(cassette, mode="record") as session:
        print("recorded answer:", naive_agent(session))

    out = []
    for title, agent in [("naive_agent", naive_agent), ("robust_agent", robust_agent)]:
        for persistent in (False, True):
            heading = f"### {title}, {'persistent' if persistent else 'one-off'} faults"
            table = to_markdown(resilience_matrix(agent, cassette, persistent=persistent))
            out.append(f"{heading}\n\n{table}")
    report = "\n\n".join(out)
    print(report)
    return report


if __name__ == "__main__":
    main()
