import asyncio
import sys
from pathlib import Path

import pytest

from toolchaos import Fault, InjectedFault, RecordedError, ReplayMiss, Session, ToolTimeout, resilience_matrix

sys.path.insert(0, str(Path(__file__).parent.parent / "examples"))
import support_agent  # noqa: E402


def test_replay_never_calls_the_real_function(tmp_path):
    cassette = tmp_path / "c.jsonl"
    calls = []

    def lookup(city, units="C"):
        calls.append(city)
        return {"city": city, "temp": 31, "units": units}

    with Session(cassette, mode="record") as s:
        assert s.wrap(lookup)("Dhaka")["temp"] == 31
    replay = Session(cassette)  # auto: cassette exists, so replay
    assert replay.mode == "replay"
    assert replay.wrap(lookup)("Dhaka") == {"city": "Dhaka", "temp": 31, "units": "C"}
    assert calls == ["Dhaka"]
    with pytest.raises(ReplayMiss):
        replay.wrap(lookup)("Chattogram")


def test_recorded_exceptions_replay_as_errors(tmp_path):
    def flaky():
        raise ConnectionError("upstream down")

    with Session(tmp_path / "c.jsonl", mode="record") as s:
        with pytest.raises(ConnectionError):
            s.wrap(flaky)()
    with pytest.raises(RecordedError, match="ConnectionError: upstream down"):
        Session(tmp_path / "c.jsonl").wrap(flaky)()


def test_faults_hit_the_chosen_call_and_retries_still_replay(tmp_path):
    with Session(tmp_path / "c.jsonl", mode="record") as s:
        s.wrap(lambda q: ["a", "b", "c", "d"], name="search")("x")
    s = Session(tmp_path / "c.jsonl", faults=[Fault("search", "timeout", at=0)])
    search = s.wrap(lambda q: [], name="search")
    with pytest.raises(ToolTimeout):
        search("x")
    assert search("x") == ["a", "b", "c", "d"]
    assert s.injected == ["search:timeout"]

    truncated = Session(tmp_path / "c.jsonl", faults=[Fault("search", "truncate", every=True)])
    assert truncated.wrap(lambda q: [], name="search")("x") == ["a", "b"]
    assert isinstance(ToolTimeout("t"), (InjectedFault, TimeoutError))
    with pytest.raises(ValueError):
        Fault("search", "meteor")


def test_async_callables(tmp_path):
    async def fetch(url):
        return {"url": url, "status": 200}

    async def scenario():
        with Session(tmp_path / "c.jsonl", mode="record") as s:
            await s.wrap(fetch)("https://example.com")
        return await Session(tmp_path / "c.jsonl").wrap(fetch)("https://example.com")

    assert asyncio.run(scenario()) == {"url": "https://example.com", "status": 200}


def test_matrix_separates_a_fragile_agent_from_a_robust_one(tmp_path):
    cassette = tmp_path / "support.jsonl"
    with Session(cassette, mode="record") as s:
        assert support_agent.naive_agent(s) == "refund A-1042"

    def statuses(agent, persistent):
        return {o.fault.label(): o.status for o in resilience_matrix(agent, cassette, persistent=persistent)}

    naive = statuses(support_agent.naive_agent, False)
    assert naive["search_orders:timeout"] == "crashed"
    assert naive["llm:garbage"] == "crashed"
    robust = statuses(support_agent.robust_agent, False)
    assert set(robust.values()) == {"recovered"}, robust
    persistent = statuses(support_agent.robust_agent, True)
    assert "crashed" not in persistent.values(), persistent
    assert persistent["llm:garbage (persistent)"] == "degraded"
