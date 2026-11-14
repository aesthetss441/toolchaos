"""Sessions wrap the callables an agent depends on (tools, model clients).

In ``record`` mode every call goes to the real function and is appended to a
JSONL cassette. In ``replay`` mode nothing real is called: results come from the
cassette, so a test is deterministic, free and offline. In either mode a list of
``Fault`` objects can make chosen calls fail or return damaged data, which is how
you find out what the agent does when a dependency misbehaves.
"""
from __future__ import annotations

import dataclasses
import functools
import hashlib
import inspect
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Callable, Iterable

ERROR_KINDS = ("error", "timeout", "rate_limit")
CORRUPT_KINDS = ("empty", "garbage", "truncate")
KINDS = ERROR_KINDS + CORRUPT_KINDS


class InjectedFault(Exception):
    """Base class of every exception raised by an injected fault."""


class ToolTimeout(InjectedFault, TimeoutError):
    pass


class RateLimited(InjectedFault):
    pass


class ReplayMiss(LookupError):
    """The agent made a call that is not in the cassette."""


class RecordedError(Exception):
    """An exception that the real dependency raised while recording."""

    def __init__(self, type_name: str, message: str):
        super().__init__(f"{type_name}: {message}")
        self.type_name = type_name


@dataclasses.dataclass(frozen=True)
class Fault:
    """Make the ``at``-th call (0-based) to ``target`` misbehave; ``every`` hits all calls from ``at`` on."""

    target: str
    kind: str
    at: int = 0
    every: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown fault kind {self.kind!r}; expected one of {KINDS}")

    def hits(self, name: str, call_index: int) -> bool:
        return name == self.target and (call_index >= self.at if self.every else call_index == self.at)

    def label(self) -> str:
        return f"{self.target}:{self.kind}" + (" (persistent)" if self.every else "")


def to_jsonable(obj: Any) -> Any:
    """Best-effort conversion to JSON types. Unknown objects are stored as their repr."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    if hasattr(obj, "model_dump"):  # pydantic v2: OpenAI / Anthropic SDK response objects
        return to_jsonable(obj.model_dump())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(dataclasses.asdict(obj))
    return {"__repr__": repr(obj)}


def call_key(name: str, args: tuple, kwargs: dict) -> str:
    """Stable identity of a call: same name and arguments give the same key."""
    payload = json.dumps([name, to_jsonable(args), to_jsonable(kwargs)], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def corrupt(kind: str, value: Any) -> Any:
    """Damage a successful result the way flaky tools and models do."""
    if kind == "empty":
        return type(value)() if isinstance(value, (str, list, dict)) else None
    if kind == "truncate":
        if isinstance(value, (str, list)):
            return value[: len(value) // 2]
        if isinstance(value, dict):
            return dict(list(value.items())[: len(value) // 2])
        return value
    return "<<corrupted response>>"  # garbage


class Session:
    """One run of an agent against a cassette.

    mode: ``record`` (call real functions, write the cassette on ``save``/exit),
    ``replay`` (never call real functions), or ``auto`` (replay if the cassette
    exists, otherwise record).
    """

    def __init__(self, cassette: str | Path, mode: str = "auto", faults: Iterable[Fault] = ()):
        self.path = Path(cassette)
        if mode == "auto":
            mode = "replay" if self.path.exists() else "record"
        if mode not in ("record", "replay"):
            raise ValueError("mode must be 'record', 'replay' or 'auto'")
        self.mode = mode
        self.faults = list(faults)
        self.calls: list[dict] = []  # every call made in this session, for assertions
        self.injected: list[str] = []  # labels of faults that actually fired
        self._counts: dict[str, int] = defaultdict(int)
        self._tape: dict[str, deque] = defaultdict(deque)
        if mode == "replay":
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._tape[entry["key"]].append(entry)

    # ------------------------------------------------------------ wrapping
    def wrap(self, fn: Callable, name: str | None = None) -> Callable:
        """Return ``fn`` routed through this session. Works for sync and async callables."""
        name = name or getattr(fn, "__name__", "call")
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                fault, entry = self._before(name, args, kwargs)
                if entry is None:
                    entry = self._capture(name, args, kwargs)
                    try:
                        entry["result"] = to_jsonable(await fn(*args, **kwargs))
                    except Exception as exc:  # recorded so replay reproduces the failure
                        entry["error"] = {"type": type(exc).__name__, "message": str(exc)}
                        self._after(entry, None)
                        raise
                return self._after(entry, fault)
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            fault, entry = self._before(name, args, kwargs)
            if entry is None:
                entry = self._capture(name, args, kwargs)
                try:
                    entry["result"] = to_jsonable(fn(*args, **kwargs))
                except Exception as exc:
                    entry["error"] = {"type": type(exc).__name__, "message": str(exc)}
                    self._after(entry, None)
                    raise
            return self._after(entry, fault)
        return wrapper

    # ------------------------------------------------------------ internals
    def _before(self, name: str, args: tuple, kwargs: dict) -> tuple[Fault | None, dict | None]:
        """Apply error faults and, in replay mode, look the call up. Returns (fault, recorded entry)."""
        index = self._counts[name]
        self._counts[name] += 1
        fault = next((f for f in self.faults if f.hits(name, index)), None)
        if fault and fault.kind in ERROR_KINDS:
            # Raised before the tape is consulted, so a retry still finds its recording.
            self.injected.append(fault.label())
            self.calls.append({"name": name, "fault": fault.kind})
            if fault.kind == "timeout":
                raise ToolTimeout(f"injected timeout in {name}")
            if fault.kind == "rate_limit":
                raise RateLimited(f"injected rate limit in {name}")
            raise InjectedFault(f"injected error in {name}")
        if self.mode == "record":
            return fault, None
        queue = self._tape.get(call_key(name, args, kwargs))
        if not queue:
            raise ReplayMiss(f"{name} was called with arguments that are not in {self.path.name}: "
                             f"{json.dumps(to_jsonable([args, kwargs]), ensure_ascii=False)[:200]}")
        # The last recording for a key is reused, so retries and loops can replay it again.
        return fault, queue.popleft() if len(queue) > 1 else queue[0]

    def _capture(self, name: str, args: tuple, kwargs: dict) -> dict:
        return {"name": name, "key": call_key(name, args, kwargs),
                "args": to_jsonable(args), "kwargs": to_jsonable(kwargs)}

    def _after(self, entry: dict, fault: Fault | None) -> Any:
        if self.mode == "record":
            self.calls.append(entry)
        elif self.mode == "replay":
            self.calls.append({"name": entry["name"], "key": entry["key"]})
        if "error" in entry:
            if self.mode == "replay":
                raise RecordedError(entry["error"]["type"], entry["error"]["message"])
            return None
        result = entry["result"]
        if fault:  # corrupting kinds only; error kinds already raised
            self.injected.append(fault.label())
            return corrupt(fault.kind, result)
        return result

    # ------------------------------------------------------------ persistence
    def save(self) -> None:
        if self.mode != "record":
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            for entry in self.calls:
                if "key" in entry:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.save()
