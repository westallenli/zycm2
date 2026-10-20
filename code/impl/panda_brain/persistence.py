"""SQLite persistence for the offline workspace (doc 11 §6.4).

Design notes:
- State and explicit role appointments persist; login sessions do not.
  A reloaded workspace never inherits an
  open general entry: bind a real ``GeneralEntryAuth`` at load time to require
  fresh authentication, or rely on the local harness premise.
- Events live in their own table one row per event, so tampering with any row
  is detectable through ``verify_audit_chain`` after a round trip.
- Everything else goes through a generic typed JSON codec over the domain
  dataclasses (entity table), keeping the schema stable while models evolve.
"""

from __future__ import annotations

import json
import sqlite3
import types
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

from .ingestion import ObservationRecord
from .models import Event
from .workspace import ScenarioWorkspace

SCHEMA_VERSION = "1"

_DICT_COLLECTIONS = (
    "object_revisions",
    "claims",
    "intel_items",
    "information_gaps",
    "topics",
    "proposals",
    "feedback",
    "options",
    "decisions",
    "orders",
    "task_drafts",
    "reports",
    "evidence",
    "staff_plans",
    "staff_tasks",
    "goal_frames",
    "decision_briefs",
    "feasibility_verdicts",
    "action_contracts",
    "policy_checks",
    "approvals",
    "general_questions",
    "reviews",
    "memory_candidates",
    "verifications",
    "person_library",
    "roster_bindings",
    "annotations",
    "conflict_records",
    "command_flows",
    "situation_changes",
    "situation_reviews",
)

_LIST_COLLECTIONS = (
    "assessments",
    "disagreements",
    "orchestration_feedbacks",
)

#: Collections whose dict key is not the entity id.
_KEY_ATTR_OVERRIDES = {
    "claims": "subject",
    "person_library": "phone",
    "conflict_records": "conflict_id",
    "command_flows": "order_id",
    "situation_changes": "change_id",
    "situation_reviews": "review_batch_id",
}


# ---------------------------------------------------------------------------
# Typed JSON codec


def _unwrap_optional(hint: Any) -> Any:
    if get_origin(hint) is Union or isinstance(hint, types.UnionType):
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return hint


def _encode_value(value: Any, hint: Any) -> Any:
    if value is None:
        return None
    hint = _unwrap_optional(hint)
    if isinstance(hint, type) and issubclass(hint, Enum):
        return value.value
    origin = get_origin(hint)
    if origin is tuple:
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return [_encode_value(item, args[0]) for item in value]
        return [_encode_value(item, arg) for item, arg in zip(value, args)]
    if origin is list:
        item_type = get_args(hint)[0]
        return [_encode_value(item, item_type) for item in value]
    if origin is dict:
        value_type = get_args(hint)[1]
        return {key: _encode_value(item, value_type) for key, item in value.items()}
    if isinstance(hint, type) and is_dataclass(hint):
        hints = get_type_hints(hint)
        return {f.name: _encode_value(getattr(value, f.name), hints[f.name]) for f in fields(hint)}
    return value


def _decode_value(data: Any, hint: Any) -> Any:
    if data is None:
        return None
    # Unwrap Optional FIRST so every branch below sees the bare type;
    # otherwise ``Optional[SomeEnum]`` falls through to the raw passthrough.
    hint = _unwrap_optional(hint)
    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint(data)
    origin = get_origin(hint)
    if origin is tuple:
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_value(item, args[0]) for item in data)
        return tuple(_decode_value(item, arg) for item, arg in zip(data, args))
    if origin is list:
        item_type = get_args(hint)[0]
        return [_decode_value(item, item_type) for item in data]
    if origin is dict:
        value_type = get_args(hint)[1]
        return {key: _decode_value(item, value_type) for key, item in data.items()}
    if isinstance(hint, type) and is_dataclass(hint):
        hints = get_type_hints(hint)
        return hint(**{name: _decode_value(payload, hints[name]) for name, payload in data.items()})
    return data


def encode_entity(obj: Any) -> str:
    if not is_dataclass(obj):
        raise TypeError(f"Not a dataclass entity: {type(obj)!r}")
    hints = get_type_hints(type(obj))
    payload = {f.name: _encode_value(getattr(obj, f.name), hints[f.name]) for f in fields(obj)}
    return json.dumps({"__class__": type(obj).__name__, "data": payload}, ensure_ascii=False)


def decode_entity(raw: str) -> Any:
    envelope = json.loads(raw)
    from . import ingestion as ingestion_module
    from . import map_annotations as map_annotations_module
    from . import models as models_module
    from . import record_contracts as record_contracts_module
    from . import situation_monitor as situation_monitor_module
    from . import tactical_map as tactical_map_module
    from . import task_drafts as task_drafts_module

    cls = (
        getattr(models_module, envelope["__class__"], None)
        or getattr(record_contracts_module, envelope["__class__"], None)
        or getattr(ingestion_module, envelope["__class__"], None)
        or getattr(tactical_map_module, envelope["__class__"], None)
        or getattr(task_drafts_module, envelope["__class__"], None)
        or getattr(situation_monitor_module, envelope["__class__"], None)
        or getattr(map_annotations_module, envelope["__class__"])
    )
    hints = get_type_hints(cls)
    return cls(**{name: _decode_value(payload, hints[name]) for name, payload in envelope["data"].items()})


# ---------------------------------------------------------------------------
# Save / load


def save_workspace(
    workspace: ScenarioWorkspace,
    db_path: str | Path,
    *,
    observation_store: object | None = None,
) -> None:
    """Full-snapshot replace. Single-writer by premise; atomic via one transaction."""
    path = Path(db_path)
    from .durable_storage import before_write
    before_write(path, sqlite=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY,
                    id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entities (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (kind, id)
                );
                """
            )
            # Ordinary panel saves must retain a source store already belonging
            columns = {row[1] for row in connection.execute('PRAGMA table_info(events)')}
            if 'recorded_at' not in columns:
                connection.execute("ALTER TABLE events ADD COLUMN recorded_at TEXT NOT NULL DEFAULT ''")
            # No retroactive clocks are assigned to legacy events.
            # to this event. Passing a store explicitly still replaces it.
            old_meta = dict(connection.execute('SELECT key, value FROM meta'))
            preserved_observations = []
            if (observation_store is None and old_meta.get('event_id') == workspace.event_id
                    and old_meta.get('scenario_id') == workspace.scenario_id):
                preserved_observations = list(connection.execute(
                    "SELECT kind, id, ordinal, data FROM entities WHERE kind = 'observation'"))
            connection.execute("DELETE FROM meta")
            connection.execute("DELETE FROM events")
            connection.execute("DELETE FROM entities")

            connection.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("scenario_id", workspace.scenario_id),
                    ("event_id", workspace.event_id),
                    ("record_contract_version", "1"),
                ],
            )
            connection.executemany(
                "INSERT INTO events (sequence, id, kind, payload, prev_hash, hash, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        event.sequence,
                        event.id,
                        event.kind,
                        json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
                        event.prev_hash,
                        event.hash,
                        event.recorded_at,
                    )
                    for event in workspace.events
                ],
            )
            entity_rows: list[tuple[str, str, int, str]] = preserved_observations
            for attr in _DICT_COLLECTIONS:
                collection = getattr(workspace, attr)
                for ordinal, (key, obj) in enumerate(sorted(collection.items())):
                    entity_rows.append((attr, key, ordinal, encode_entity(obj)))
            for attr in _LIST_COLLECTIONS:
                for ordinal, obj in enumerate(getattr(workspace, attr)):
                    entity_rows.append((attr, getattr(obj, "id"), ordinal, encode_entity(obj)))
            if observation_store is not None:
                for ordinal, record in enumerate(observation_store.observations()):
                    entity_rows.append(
                        ("observation", record.observation_id, ordinal, encode_entity(record))
                    )
            connection.executemany(
                "INSERT INTO entities (kind, id, ordinal, data) VALUES (?, ?, ?, ?)",
                entity_rows,
            )
    finally:
        connection.close()


def load_workspace(
    db_path: str | Path,
    *,
    general_auth: object | None = None,
    observation_store: object | None = None,
) -> ScenarioWorkspace:
    """Rebuild a workspace from a snapshot.

    ``general_auth`` binds a real entry and the workspace starts locked;
    without it the local harness premise applies (ephemeral entry, open).
    General-entry unlock state is never restored; explicit V2 role records are
    restored and validated separately, and still require a fresh named session.
    """
    from .read_scope import reuse
    path = Path(db_path).resolve()
    stat = path.stat()
    return reuse('workspace-snapshot', (str(path), stat.st_mtime_ns, stat.st_size,
                 general_auth, observation_store),
                 lambda: _load_workspace(path, general_auth=general_auth, observation_store=observation_store))


def _load_workspace(db_path, *, general_auth=None, observation_store=None):
    connection = sqlite3.connect(Path(db_path))
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        columns = {row[1] for row in connection.execute('PRAGMA table_info(events)')}
        clock = 'recorded_at' if 'recorded_at' in columns else "''"
        event_rows = connection.execute(
            f"SELECT sequence, id, kind, payload, prev_hash, hash, {clock} FROM events ORDER BY sequence"
        ).fetchall()
        entity_rows = connection.execute(
            "SELECT kind, ordinal, data FROM entities ORDER BY kind, ordinal"
        ).fetchall()
    finally:
        connection.close()

    workspace = ScenarioWorkspace(meta.get("scenario_id", "restored"), general_auth=general_auth,
                                  event_id=meta.get("event_id", ""))
    workspace._events = [
        Event(
            id=row[1],
            sequence=int(row[0]),
            kind=row[2],
            payload=json.loads(row[3]),
            prev_hash=row[4],
            hash=row[5],
            recorded_at=row[6],
        )
        for row in event_rows
    ]
    for kind, _ordinal, raw in entity_rows:
        obj = decode_entity(raw)
        if kind == "observation":
            if observation_store is not None:
                observation_store._observations[obj.observation_id] = obj
            continue
        if kind in _LIST_COLLECTIONS:
            getattr(workspace, kind).append(obj)
        elif kind in _DICT_COLLECTIONS:
            key_attr = _KEY_ATTR_OVERRIDES.get(kind, "id")
            getattr(workspace, kind)[getattr(obj, key_attr)] = obj
        else:
            raise ValueError(f"Unknown entity kind in store: {kind}")
    # V2 authority must agree with its audit records before any caller can use it.
    from .event_authorization import event_grants
    event_grants(workspace)
    return workspace
