"""Push of the pending changes of a working copy to the P6 EPPM server (docs/EPPM_PLAN.md section 4).

Version 1 sends what was measured on P6 EPPM 25.12: activity fields (PUT /activity), relationship creation, update
and deletion (POST, PUT, DELETE /relationship) and assignment units (PUT /resourceAssignment). The server has no
optimistic lock (a stale LastUpdateDate is accepted), so the conflict check is ours: each object is read live just
before the push and compared with the value pulled:
  live = pulled -> the call is sent; live = local -> already done (nothing sent); else -> conflict, and the whole
  push is refused until the user pulls again (or drops the local change).
REST has no transaction: the calls go in a fixed order (relationship deletes, activity updates, relationship
creations, relationship updates, assignments), one object per call, and the first failure stops the push. A journal
line per call (`push_<stamp>.jsonl` beside the working copy) records what reached the server; a new push sends the
rest, since the pushed edits no longer count as pending.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Optional

from p6mcp.eppm import mapping

ORDER = (("TASKPRED", "delete"), ("TASK", "update"), ("TASKPRED", "create"), ("TASKPRED", "update"),
         ("TASKRSRC", "update"))
ENTITY = {"TASK": "activity", "TASKPRED": "relationship", "TASKRSRC": "resourceAssignment"}
SPEC = {"TASK": mapping.ACTIVITY, "TASKPRED": mapping.RELATIONSHIP, "TASKRSRC": mapping.ASSIGNMENT}
# local column -> REST field, for the columns the edit tools write (others are recomputed by P6: rollups, costs)
PUSHED = {
    "TASK": {"task_name": "Name", "target_drtn_hr_cnt": "PlannedDuration", "remain_drtn_hr_cnt": "RemainingDuration",
             "cstr_type": "PrimaryConstraintType", "cstr_date": "PrimaryConstraintDate",
             "act_start_date": "ActualStartDate", "act_end_date": "ActualFinishDate",
             "phys_complete_pct": "PhysicalPercentComplete", "status_code": "Status"},
    "TASKPRED": {"pred_type": "Type", "lag_hr_cnt": "Lag"},
    "TASKRSRC": {"target_qty": "PlannedUnits", "act_reg_qty": "ActualRegularUnits", "remain_qty": "RemainingUnits"},
}
TEXT = {  # P6 code -> the REST text
    "pred_type": {"PR_FS": "Finish to Start", "PR_SS": "Start to Start", "PR_FF": "Finish to Finish",
                  "PR_SF": "Start to Finish"},
    "cstr_type": {"CS_MSO": "Start On", "CS_MSOB": "Start On or Before", "CS_MSOA": "Start On or After",
                  "CS_MEO": "Finish On", "CS_MEOB": "Finish On or Before", "CS_MEOA": "Finish On or After",
                  "CS_ALAP": "As Late As Possible", "CS_MANDSTART": "Mandatory Start",
                  "CS_MANDFIN": "Mandatory Finish"},
    "status_code": {"TK_NotStart": "Not Started", "TK_Active": "In Progress", "TK_Complete": "Completed"},
}
ID_FIELD = {"TASK": "ObjectId", "TASKPRED": "ObjectId", "TASKRSRC": "ObjectId"}


class PushError(ValueError):
    """The push cannot be planned (an unsupported change); the message is meant for the user."""


def _kind(table: str, col: str) -> Any:
    return next((k for _f, c, k in SPEC[table] if c == col), "text")


def to_rest(table: str, col: str, v: Any) -> Any:
    """A working copy value -> what the REST API takes (text enums, ISO dates, numbers as numbers)."""
    if v is None:
        return None
    if col in TEXT:
        return TEXT[col].get(v, v)
    k = _kind(table, col)
    if k == "date":
        s = str(v).strip().replace(" ", "T")
        return s[:19] + (":00" if len(s) == 16 else "")
    if k in ("num", "int", "id"):
        return float(v) if k == "num" else int(v)
    return v


def same(table: str, col: str, a: Any, b: Any) -> bool:
    """Equal as P6 stores them: dates to the minute (XER exports drop seconds), numbers within 1e-6."""
    if a is None or b is None:
        return a is None and b is None
    k = _kind(table, col)
    if k == "date":
        return str(a).replace("T", " ")[:16] == str(b).replace("T", " ")[:16]
    if k in ("num", "int", "id"):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    return str(a) == str(b)


def _label(c: dict) -> str:
    lb = c.get("labels") or {}
    if c["table"] == "TASK":
        return f"activity {lb.get('task_code', c['keys'])}"
    if c["table"] == "TASKPRED":
        return f"relationship {lb.get('predecessor', '?')} -> {lb.get('successor', '?')}"
    return f"assignment {lb.get('task_code', '')} {lb.get('resource') or lb.get('role') or ''}".strip()


def plan(sess, changes: list[dict], local_base: int) -> dict:
    """Read the live objects and classify every change. Returns {calls, already_done, conflicts, unsupported, plan}
    (plan = the lines shown to the user; the confirmation token is bound to them)."""
    unsupported = [c for c in changes if c["table"] not in ENTITY or (c["table"], c["action"]) not in ORDER]
    todo = [c for c in changes if c not in unsupported]
    live: dict[tuple, Optional[dict]] = {}
    for table in ENTITY:
        ids = sorted({int(c["keys"][list(c["keys"])[0]]) for c in todo if c["table"] == table
                      and c["action"] != "create"})
        if not ids:
            continue
        fields = ["ObjectId"] + sorted({f for f in PUSHED[table].values()})
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            for r in sess.read(ENTITY[table], fields, " :or: ".join(f"ObjectId :eq: {x}" for x in chunk)):
                live[(table, int(r["ObjectId"]))] = r
    calls, done, conflicts = [], [], []
    for table, action in ORDER:
        for c in (c for c in todo if c["table"] == table and c["action"] == action):
            oid = int(c["keys"][list(c["keys"])[0]])
            fields = {col: f for col, f in PUSHED[table].items() if col in c["fields"]}
            skipped = sorted(set(c["fields"]) - set(fields))
            item = {"table": table, "action": action, "object_id": oid, "label": _label(c), "project": c.get("project"),
                    "change": c}
            if skipped:
                item["not_sent"] = skipped  # recomputed by P6 from what is sent
            if action == "create":
                refs = c.get("refs") or {}
                if not refs.get("task_id") or not refs.get("pred_task_id"):
                    conflicts.append(dict(item, reason="the activities of the new relationship are unknown"))
                    continue
                body = {"PredecessorActivityObjectId": int(refs["pred_task_id"]),
                        "SuccessorActivityObjectId": int(refs["task_id"])}
                body.update({f: to_rest(table, col, c["fields"][col]["local"]) for col, f in fields.items()})
                existing = sess.read("relationship", ["ObjectId", "Type", "Lag"],
                                     f"PredecessorActivityObjectId :eq: {body['PredecessorActivityObjectId']} :and: "
                                     f"SuccessorActivityObjectId :eq: {body['SuccessorActivityObjectId']}")
                hit = next((r for r in existing if r.get("Type") == body.get("Type")), None)
                if hit:
                    if same(table, "lag_hr_cnt", mapping.value(hit.get("Lag"), "num"),
                            c["fields"].get("lag_hr_cnt", {}).get("local", 0.0)):
                        done.append(dict(item, server_id=int(hit["ObjectId"]),
                                         reason="the same relationship exists on the server"))
                    else:
                        conflicts.append(dict(item, reason="a relationship of this type already links these "
                                                           "activities on the server, with another lag"))
                    continue
                calls.append(dict(item, method="POST", path=f"/{ENTITY[table]}", body=[body]))
                continue
            row = live.get((table, oid))
            if row is None:
                if action == "delete":
                    done.append(dict(item, reason="already gone from the server"))
                else:
                    conflicts.append(dict(item, reason="no longer on the server (or not readable by this user)"))
                continue
            diffs, already = [], True
            for col, f in (fields.items() if action != "delete" else PUSHED[table].items()):
                fc = c["fields"].get(col)
                if fc is None:
                    continue
                cur = mapping.value(row.get(f), _kind(table, col) if col not in TEXT else
                                    {v.lower(): k for k, v in TEXT[col].items()})
                if not same(table, col, cur, fc["pulled"]):
                    if action != "delete" and same(table, col, cur, fc["local"]):
                        continue  # this field already holds the local value
                    diffs.append(f"{f}: pulled {fc['pulled']!r}, now {cur!r} on the server")
                if action != "delete" and not same(table, col, cur, fc["local"]):
                    already = False
            if diffs:
                conflicts.append(dict(item, reason="changed on the server since the pull: " + "; ".join(diffs)))
            elif action == "delete":
                calls.append(dict(item, method="DELETE", path=f"/{ENTITY[table]}?ObjectId={oid}", body=None))
            elif already:
                done.append(dict(item, reason="the server already holds the local values"))
            else:
                body = {ID_FIELD[table]: oid}
                body.update({f: to_rest(table, col, c["fields"][col]["local"]) for col, f in fields.items()})
                calls.append(dict(item, method="PUT", path=f"/{ENTITY[table]}", body=[body]))
    lines = [f"{i + 1}. {x['method']} {x['label']}"
             + (f" ({x['project']})" if x.get("project") else "")
             + (": " + ", ".join(f"{k} = {v!r}" for k, v in x["body"][0].items() if not k.endswith("ObjectId"))
                if x["body"] else "") for i, x in enumerate(calls)]
    return {"calls": calls, "already_done": done, "conflicts": conflicts, "unsupported": unsupported,
            "plan": lines or ["Nothing to send."]}


def apply(sess, calls: list[dict], journal: str) -> dict:
    """Send the calls in order, stop at the first failure. Every outcome is appended to the journal (JSON lines).
    Returns {sent: [...], failed: {...} or None, created: {local id: server id}}."""
    sent, created = [], {}
    os.makedirs(os.path.dirname(journal), exist_ok=True)
    with open(journal, "a", encoding="utf-8") as fh:
        for x in calls:
            rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "method": x["method"],
                   "table": x["table"], "action": x["action"], "object_id": x["object_id"], "label": x["label"],
                   "body": x["body"]}
            try:
                res = sess.call(x["method"], x["path"], body=x["body"], name=f"push {x['label']}")
            except ValueError as e:
                rec.update(ok=False, error=str(e))
                fh.write(json.dumps(rec, default=str) + "\n")
                return {"sent": sent, "failed": dict(rec, label=x["label"]), "created": created}
            if x["action"] == "create":
                ids = [int(v) for v in (res or []) if str(v).strip().lstrip("-").isdigit()]
                if not ids:
                    rec.update(ok=False, error=f"the server did not return the new ObjectId: {res!r}")
                    fh.write(json.dumps(rec, default=str) + "\n")
                    return {"sent": sent, "failed": rec, "created": created}
                created[x["object_id"]] = ids[0]
                rec["server_id"] = ids[0]
            rec["ok"] = True
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            sent.append(x)
    return {"sent": sent, "failed": None, "created": created}
