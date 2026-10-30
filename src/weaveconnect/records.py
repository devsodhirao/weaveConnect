"""Weave calls as plain records.

Calls are read straight from the trace server, so references stay as `weave:///...` strings.
Reading them through `client.get_calls` resolves references and can try to re-import old
versions of the developer's code, which fails noisily for code Weave cannot reload.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from weave.trace.urls import redirect_call


@dataclass(frozen=True)
class CallRecord:
    id: str
    trace_id: str
    parent_id: str | None
    op: str
    started_at: datetime
    ended_at: datetime | None
    inputs: dict[str, Any] = field(default_factory=dict)
    output: Any = None
    exception: str | None = None
    url: str | None = None


def plain(value: Any) -> Any:
    """Convert containers to dicts, lists and strings, recursively."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, Sequence):
        return [plain(item) for item in value]
    return str(value)


def op_display(op_name: str) -> str:
    """`weave:///team/project/op/answer:digest` becomes `answer`."""
    if op_name.startswith("weave:///") and "/op/" in op_name:
        return op_name.split("/op/", 1)[1].split(":", 1)[0]
    return op_name


def object_name(ref: Any) -> str:
    """`weave:///team/project/object/name:digest` becomes `name`."""
    text = str(ref or "")
    return text.split("/object/", 1)[1].split(":", 1)[0] if "/object/" in text else text


def query_calls(
    client: Any,
    *,
    roots_only: bool = False,
    call_ids: list[str] | None = None,
    limit: int = 1000,
) -> Iterator[CallRecord]:
    """Calls in the client's project, newest first."""
    query_filter: dict[str, Any] = {}
    if roots_only:
        query_filter["trace_roots_only"] = True
    if call_ids:
        query_filter["call_ids"] = call_ids
    rows = client.server.calls_query_stream(
        {
            "project_id": client.project_id,
            "filter": query_filter,
            "limit": limit,
            "sort_by": [{"field": "started_at", "direction": "desc"}],
        }
    )
    for row in rows:
        yield from_row(row, client.entity, client.project)


def from_row(row: Any, entity: str, project: str) -> CallRecord:
    data = row.model_dump() if hasattr(row, "model_dump") else dict(row)
    return CallRecord(
        id=str(data["id"]),
        trace_id=str(data["trace_id"]),
        parent_id=str(data["parent_id"]) if data.get("parent_id") else None,
        op=op_display(str(data["op_name"])),
        started_at=data["started_at"],
        ended_at=data.get("ended_at"),
        inputs=plain(data.get("inputs") or {}),
        output=plain(data.get("output")),
        exception=str(data["exception"]) if data.get("exception") else None,
        url=redirect_call(entity, project, str(data["id"])),
    )


def fetch_since(client: Any, since: datetime, limit: int = 2000) -> list[CallRecord]:
    """Every call that started at or after `since`, newest first."""
    records = []
    for record in query_calls(client, limit=limit):
        if record.started_at < since:
            break
        records.append(record)
    return records
