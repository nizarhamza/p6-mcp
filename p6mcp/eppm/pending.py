"""Pending changes of a P6 EPPM working copy (docs/EPPM_PLAN.md section 4.1).

A confirmed edit on the working copy writes edit log entries (operation, table, keys, before, after), exactly as on
a P6 Professional database. The pending changes are those entries of the working copy made since its pull, not
undone by a restore and not pushed yet, netted per server object and field: the first `before` (the pulled value)
against the last `after` (the local value). A field set back to its pulled value drops out; a relationship created
then deleted locally drops out; an object updated then deleted becomes a delete.
"""

from __future__ import annotations

import json
from typing import Callable, Iterable, Optional

# edit log operation -> what it does to its object (the push supports these in version 1)
OPERATIONS = {"update_activity": "update", "update_assignment": "update", "update_udf": "set", "clear_udf": "delete",
              "add_relationship": "create", "update_relationship": "update", "delete_relationship": "delete"}
ID_KEYS = {"TASK": ("task_id",), "TASKPRED": ("task_pred_id",), "TASKRSRC": ("taskrsrc_id",),
           "UDFVALUE": ("udf_type_id", "fk_id")}
# labels kept for the user next to the ids (Activity IDs, resource, UDF name)
LABEL_KEYS = ("task_code", "successor", "predecessor", "resource", "role", "udf", "label")
# the other ids the push needs (a new relationship: its two activities and the predecessor's project)
REF_KEYS = ("task_id", "pred_task_id", "pred_proj_id", "udf_type_id", "fk_id", "taskrsrc_id")


def _stamp(ts: str) -> str:
    return str(ts or "").replace("T", " ")[:23]


def ident(e: dict) -> str:
    """One edit log entry's identity (a commit can hold several objects: a batch tool)."""
    return f"{e.get('commit')}|{e.get('table')}|{json.dumps(_key(e))}"


def entries_since(entries: Iterable[dict], pulled_at: str, undone: Callable[[dict], bool],
                  pushed: Iterable[str] = ()) -> list[dict]:
    """The edit log entries that count: made after the pull, not undone, not pushed (by identity), supported."""
    since, done = _stamp(pulled_at), set(pushed)
    return [e for e in entries if _stamp(e.get("ts")) >= since and not undone(e)
            and ident(e) not in done and e.get("operation") in OPERATIONS]


def _key(e: dict) -> Optional[tuple]:
    t, keys = e.get("table"), e.get("keys") or {}
    ids = ID_KEYS.get(t)
    if not ids or any(keys.get(k) is None for k in ids):
        return None
    return (t,) + tuple(keys[k] for k in ids)


def net(entries: Iterable[dict]) -> list[dict]:
    """Net changes per object, in the order of their first edit."""
    objs: dict[tuple, dict] = {}
    for e in entries:
        k = _key(e)
        if k is None:
            continue
        kind = OPERATIONS[e["operation"]]
        before, after = e.get("before") or {}, e.get("after") or {}
        o = objs.get(k)
        if o is None:
            o = objs[k] = {"table": k[0], "keys": {n: v for n, v in zip(ID_KEYS[k[0]], k[1:], strict=True)},
                           "labels": {}, "project": e.get("project"), "proj_id": e.get("proj_id"),
                           "created": kind == "create", "deleted": False, "pulled": {}, "local": {},
                           "first_ts": e.get("ts"), "last_ts": e.get("ts"), "edits": 0}
        o["labels"].update({n: v for n, v in (e.get("keys") or {}).items() if n in LABEL_KEYS and v is not None})
        o.setdefault("refs", {}).update({n: v for n, v in (e.get("keys") or {}).items()
                                         if n in REF_KEYS and v is not None and n not in o["keys"]})
        o["edits"] += 1
        o["last_ts"] = e.get("ts")
        o.setdefault("entries", []).append(ident(e))
        if kind == "delete":
            o["deleted"] = True
            for f, v in before.items():  # the values the object had (for the conflict check of a delete)
                o["pulled"].setdefault(f, v)
            continue
        if o["deleted"]:  # a UDF value cleared then set again: a set
            o["deleted"] = False
        for f, v in before.items():
            o["pulled"].setdefault(f, v)
        for f, v in after.items():
            o["pulled"].setdefault(f, before.get(f))
            o["local"][f] = v
    out = []
    for o in objs.values():
        if o["created"] and o["deleted"]:
            continue  # created and deleted locally: nothing to send
        if o["created"]:
            action = "create"
        elif o["deleted"]:
            action = "delete"
        else:
            action = "update"
        fields = {}
        if action != "delete":
            for f, v in o["local"].items():
                if f.startswith("TASK."):  # rollups P6 recomputes from the assignment
                    continue
                if action == "update" and _same(o["pulled"].get(f), v):
                    continue
                fields[f] = {"pulled": None if action == "create" else o["pulled"].get(f), "local": v}
            if action == "update" and not fields:
                continue  # every field back to its pulled value
        else:
            fields = {f: {"pulled": v, "local": None} for f, v in o["pulled"].items() if not f.startswith("TASK.")}
            if all(v["pulled"] is None for v in fields.values()):
                continue  # nothing existed at the pull (a UDF value set then cleared locally)
        rollups = {f[5:]: {"pulled": o["pulled"].get(f), "local": v} for f, v in o["local"].items()
                   if f.startswith("TASK.") and action == "update" and not _same(o["pulled"].get(f), v)}
        out.append({"id": change_id(o["table"], o["keys"]), "table": o["table"], "action": action,
                    "keys": o["keys"], "refs": o.get("refs", {}),
                    "labels": o["labels"],
                    "project": o["project"], "proj_id": o["proj_id"], "fields": fields, "edits": o["edits"],
                    "first_edit": str(o["first_ts"])[:19], "last_edit": str(o["last_ts"])[:19],
                    "entries": o["entries"], "rollups": rollups})
    return out


def change_id(table: str, keys: dict) -> str:
    """The id p6_eppm_status shows for a pending change and p6_eppm_discard takes: table and key values
    ('TASK:36272', 'TASKPRED:2247', 'UDFVALUE:12:36272')."""
    return ":".join([table] + [str(v) for v in keys.values()])


def _same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def summary(changes: list[dict]) -> dict:
    """Counts per table and action, for p6_eppm_status."""
    out: dict[str, dict[str, int]] = {}
    for c in changes:
        t = out.setdefault(c["table"], {})
        t[c["action"]] = t.get(c["action"], 0) + 1
    return out
