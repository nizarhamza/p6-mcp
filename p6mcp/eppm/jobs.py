"""Scheduling on the P6 EPPM server (docs/EPPM_PLAN.md section 4.4), measured on P6 EPPM 25.12:
POST /job/schedule {ProjectObjectId: [..], NewDataDate?} -> [{"JobId": "1:21", "JobStatus": "Pending"}];
GET /job/readJobStatus?JobId=.. -> {"JobStatus": "Complete"} (5 s for 214 activities);
POST /job/readJobLog [{"JobId": ..}] -> [{"JobLog": "Primavera P6 Scheduling Log ..."}] (settings, open ends,
loops, out of sequence, critical count, finish).
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Callable, Optional

from p6mcp.messages import _t

POLL_S = 3.0          # between two status reads (tests lower it)
TIMEOUT_S = 900       # a schedule job that runs longer is reported, not waited for
TERMINAL = re.compile(r"(?i)complet|fail|error|cancel|abort")
RUNNING = re.compile(r"(?i)pending|running|queued|progress|start")
LOG_MAX = 6000        # characters of the scheduling log returned


class JobError(ValueError):
    """The job could not be started or followed; the message is meant for the user."""


def _status(obj) -> str:
    if isinstance(obj, list) and obj:
        obj = obj[0]
    if isinstance(obj, dict):
        return str(obj.get("JobStatus") or obj.get("Status") or "")
    return str(obj or "")


def schedule(sess, project_oids: list[int], new_data_date: Optional[str] = None, timeout_s: float = TIMEOUT_S,
             sleep: Callable[[float], None] = time.sleep) -> dict:
    """Start a schedule job and wait for its end. Returns {job_id, status, seconds, log, finished}."""
    body: dict = {"ProjectObjectId": [int(x) for x in project_oids]}
    if new_data_date:
        body["NewDataDate"] = new_data_date
    started = time.perf_counter()
    res = sess.call("POST", "/job/schedule", body=body, name="schedule job")
    first = res[0] if isinstance(res, list) and res else res
    job = (first or {}).get("JobId") if isinstance(first, dict) else None
    if not job:
        raise JobError(_t("The server did not return a job id for the schedule: {answer!r}", answer=res))
    status = _status(first)
    while not TERMINAL.search(status) and time.perf_counter() - started < timeout_s:
        sleep(POLL_S)
        status = _status(sess.call("GET", "/job/readJobStatus?JobId=" + urllib.parse.quote(str(job)),
                                   name="job status"))
        if status and not TERMINAL.search(status) and not RUNNING.search(status):
            break  # a status this module does not know: report it rather than wait
    finished = bool(TERMINAL.search(status)) and not re.search(r"(?i)fail|error|cancel|abort", status)
    log = ""
    try:
        got = sess.call("POST", "/job/readJobLog", body=[{"JobId": str(job)}], name="job log")
        entry = got[0] if isinstance(got, list) and got else got
        log = str((entry or {}).get("JobLog") or "") if isinstance(entry, dict) else str(entry or "")
    except ValueError as e:
        log = _t("(the scheduling log could not be read: {error})", error=e)
    return {"job_id": str(job), "status": status, "finished": finished,
            "seconds": round(time.perf_counter() - started, 1), "log": log if len(log) <= LOG_MAX else log[:LOG_MAX],
            "log_truncated": len(log) > LOG_MAX}


def log_facts(log: str) -> dict:
    """A few figures from P6's scheduling log text (None when a line is absent)."""
    def num(label: str) -> Optional[int]:
        m = re.search(rf"{label}:\s*(\d+)", log)
        return int(m.group(1)) if m else None

    def text(label: str) -> Optional[str]:
        m = re.search(rf"{label}:\s*([^\r\n]+)", log)
        return m.group(1).strip() if m else None
    return {"activities": num("Number of Activities"), "critical": num("Critical Activities"),
            "data_date": text("Earliest Data Date"), "latest_early_finish": text("Latest Early Finish Date"),
            "loops": None if "invalid relationship loops" not in log else
            not re.search(r"invalid relationship loops\s*\r?\n\s*none", log)}
