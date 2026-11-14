# toolchaos

[![CI](https://github.com/aesthetss441/toolchaos/actions/workflows/ci.yml/badge.svg)](https://github.com/aesthetss441/toolchaos/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Record/replay and fault injection for tool-using LLM agents. Zero dependencies.

Agent tests have two problems. They call real models and tools, so they are slow, cost money and give a different answer each run. And they only cover the happy path: nobody checks what the agent does when a tool times out or the model returns half a JSON object, which is exactly what happens in production.

toolchaos fixes both with one mechanism:

1. **Record** an agent run once. Every tool and model call is saved to a JSONL cassette.
2. **Replay** it in tests. Nothing real is called, so the test is deterministic, offline and free.
3. **Inject faults** into the replay (errors, timeouts, rate limits, empty, garbage and truncated responses) and get a **resilience matrix** showing where the agent recovers, degrades or crashes.

## Example

[`examples/support_agent.py`](examples/support_agent.py) has two versions of a refund agent with three dependencies. This is the actual output from CI.

A typical prototype, one fault on the first call to each dependency:

| dependency | error | timeout | rate_limit | empty | garbage | truncate |
|---|---|---|---|---|---|---|
| `search_orders` | ❌ crashed | ❌ crashed | ❌ crashed | ❌ crashed | ❌ crashed | ❌ crashed |
| `refund_policy` | ❌ crashed | ❌ crashed | ❌ crashed | ❌ crashed | ❌ crashed | ✅ recovered |
| `llm` | ❌ crashed | ❌ crashed | ❌ crashed | ❌ crashed | ❌ crashed | ❌ crashed |

The same agent with retries, output validation and a human fallback, under **persistent** faults (the dependency never comes back):

| dependency | error | timeout | rate_limit | empty | garbage | truncate |
|---|---|---|---|---|---|---|
| `search_orders` | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded |
| `refund_policy` | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ✅ recovered |
| `llm` | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded | ⚠️ degraded |

Under one-off faults the hardened agent recovers in all 18 cells. "Degraded" here means it returned `escalate to human` instead of a wrong refund decision, which is the behaviour you want.

## Install

```bash
pip install git+https://github.com/aesthetss441/toolchaos
```

Python 3.10+.

## Use

Wrap whatever your agent calls. Any callable works, sync or async, including an SDK client method.

```python
from toolchaos import Session

def run_agent(session):
    search = session.wrap(search_orders)                    # a tool
    llm = session.wrap(client.messages.create, name="llm")  # a model call
    return my_agent(question, tools=[search], llm=llm)

# Once, against the real services:
with Session("cassettes/refund.jsonl", mode="record") as s:
    run_agent(s)

# In tests, forever after (mode="auto" replays when the cassette exists):
def test_refund_flow():
    assert run_agent(Session("cassettes/refund.jsonl")) == "refund A-1042"
```

Inject a fault into a replay:

```python
from toolchaos import Fault, Session

s = Session("cassettes/refund.jsonl", faults=[Fault("search_orders", "timeout", at=0)])
assert run_agent(s) == "refund A-1042"   # the agent must retry
assert s.injected == ["search_orders:timeout"]
```

Or test every dependency against every fault kind and fail the build on a crash:

```python
from toolchaos import resilience_matrix, to_markdown

def test_agent_never_crashes():
    outcomes = resilience_matrix(run_agent, "cassettes/refund.jsonl", persistent=True)
    print(to_markdown(outcomes))
    assert not [o for o in outcomes if o.status == "crashed"]
```

`toolchaos inspect cassettes/refund.jsonl` summarises a cassette.

## Fault kinds

| Kind | Effect |
|---|---|
| `error` | raises `InjectedFault` |
| `timeout` | raises `ToolTimeout` (a `TimeoutError`) |
| `rate_limit` | raises `RateLimited` |
| `empty` | returns `""`, `[]`, `{}` or `None` |
| `garbage` | returns `"<<corrupted response>>"` |
| `truncate` | returns the first half of a string, list or dict |

`Fault(target, kind, at=n)` hits the n-th call; `every=True` hits every call from there on.

## How it works

- A call is identified by the SHA-1 of its name and JSON-normalised arguments. Replay looks calls up by that key, so the agent may make them in a different order than when recorded.
- Error faults are raised *before* the cassette is consulted, so the agent's retry of the same call finds its recording. The last recording for a key is reused, so loops and retries replay.
- A call that is not in the cassette raises `ReplayMiss`. Under fault injection that shows up as `crashed`, which is correct: the agent went somewhere you have no recording for.
- Exceptions raised by the real dependency while recording are stored and replayed as `RecordedError`.

The whole library is about 300 lines in [`core.py`](src/toolchaos/core.py) and [`matrix.py`](src/toolchaos/matrix.py).

## Limitations

- Results are stored as JSON. Replay returns dicts and lists, not the original objects; pydantic models (OpenAI and Anthropic SDK responses) are recorded through `model_dump()`. If your agent reads attributes (`resp.content`), convert in your wrapper.
- Streaming responses are not supported.
- Latency is not simulated: a `timeout` raises immediately.
- Cassettes store arguments and results in plain text. Do not record sessions that contain secrets or personal data, or scrub them before committing.
- The example's model is a rule-based stand-in so it runs without an API key. The library has not yet been exercised against a live provider in this repository's tests.

## Development

```bash
pip install -e ".[dev]" && pytest -q && python examples/support_agent.py
```

MIT licensed.
