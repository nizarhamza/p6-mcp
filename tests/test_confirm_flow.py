"""Two-step write flow: dry run, token, confirmation text, backups and edit log."""

import glob
import json
import os

import pytest

from p6mcp.confirm import TokenError

UPD = {"project": "DEMO-A", "task_code": "A1020", "remaining_duration_days": 12}


def rem_hours(p6, code="A1020"):
    return p6.sql("SELECT remain_drtn_hr_cnt FROM TASK WHERE task_code = ?", code)[0][0]


def log_entries(p6):
    out = []
    for path in glob.glob(os.path.join(p6.logs, "edits_*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            out += [json.loads(line) for line in fh]
    return out


def test_dry_run_changes_nothing_and_returns_a_token(p6):
    before = p6.hash()
    dry = p6.call("p6_update_activity", **UPD)
    assert dry["dry_run"] is True and dry["would_do"]
    assert dry["confirmation_token"].startswith("p6c_") and dry["token_expires_in_s"] == 900
    assert "dry_run=false" in dry["next"] and dry["confirmation_token"] in dry["next"]
    assert "confirmation_text_required" not in dry
    assert p6.hash() == before and not os.path.isdir(p6.backups)


def test_apply_needs_the_token(p6):
    with pytest.raises(TokenError, match="confirmation_token is required"):
        p6.call("p6_update_activity", **UPD, dry_run=False)
    with pytest.raises(TokenError, match="Unknown or already used"):
        p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token="p6c_forged")
    assert rem_hours(p6) == 120


def test_legacy_confirm_alias_also_needs_the_token(p6):
    with pytest.raises(TokenError):
        p6.call("p6_update_activity", **UPD, confirm=True)
    dry = p6.call("p6_update_activity", **UPD)
    res = p6.call("p6_update_activity", **UPD, confirm=True, confirmation_token=dry["confirmation_token"])
    assert res["applied"] is True and rem_hours(p6) == 96


def test_token_is_bound_to_tool_and_arguments(p6):
    dry = p6.call("p6_update_activity", **UPD)
    tok = dry["confirmation_token"]
    with pytest.raises(TokenError, match="arguments differ"):
        p6.call("p6_update_activity", **dict(UPD, remaining_duration_days=30), dry_run=False, confirmation_token=tok)
    with pytest.raises(TokenError, match="issued for p6_update_activity"):
        p6.call("p6_add_relationship", project="DEMO-A", predecessor_code="A1060", successor_code="A1020",
                dry_run=False, confirmation_token=tok)
    assert rem_hours(p6) == 120


def test_apply_writes_backs_up_and_logs(p6):
    res = p6.apply("p6_update_activity", **UPD)
    assert res["applied"] is True and rem_hours(p6) == 96
    backups = glob.glob(os.path.join(p6.backups, "demo_*.db"))
    assert len(backups) == 1
    entries = [e for e in log_entries(p6) if e.get("tool") == "p6_update_activity"]
    assert entries and entries[-1]["params"]["remaining_duration_days"] == 12
    assert entries[-1]["confirmation"].startswith("p6c_") and entries[-1]["backup"] == backups[0]


def test_token_is_single_use(p6):
    dry = p6.call("p6_update_activity", **UPD)
    p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token=dry["confirmation_token"])
    with pytest.raises(TokenError, match="already used"):
        p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token=dry["confirmation_token"])


def test_expired_token(p6, monkeypatch):
    dry = p6.call("p6_update_activity", **UPD)
    p6.s.TOKENS._grants[dry["confirmation_token"]].expires = 0
    with pytest.raises(TokenError, match="expired"):
        p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token=dry["confirmation_token"])


def test_database_changed_since_the_dry_run(p6):
    dry = p6.call("p6_update_activity", **UPD)  # plan: remaining 15 d -> 12 d
    # another change lands in between: the same call now has another plan (10 d -> 12 d)
    p6.apply("p6_update_activity", **dict(UPD, remaining_duration_days=10))
    before = p6.hash()
    res = p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] is False and "changed since the dry run" in res["error"]
    assert p6.hash() == before and rem_hours(p6) == 80
    with pytest.raises(TokenError):  # consumed: a new dry run is needed
        p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token=dry["confirmation_token"])


def test_logic_refusal_on_apply_writes_nothing(p6):
    dry = p6.call("p6_add_relationship", project="DEMO-A", predecessor_code="A1060", successor_code="A1020")
    p6.apply("p6_add_relationship", project="DEMO-A", predecessor_code="A1060", successor_code="A1020", lag_days=2)
    before = p6.hash()
    res = p6.call("p6_add_relationship", project="DEMO-A", predecessor_code="A1060", successor_code="A1020",
                  dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] is False and p6.hash() == before


def test_plan_mismatch_is_refused(p6):
    dry = p6.call("p6_update_activity", **UPD)
    p6.s.TOKENS._grants[dry["confirmation_token"]].plan = "0" * 64  # the previewed plan was something else
    before = p6.hash()
    res = p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] is False and "changed since the dry run" in res["error"]
    assert p6.hash() == before


def test_p6_running_keeps_the_token_for_a_retry(p6, monkeypatch):
    dry = p6.call("p6_update_activity", **UPD)
    monkeypatch.setattr(p6.s, "_p6_running", lambda: True)
    p6.set(block_when_p6_running=True)
    with pytest.raises(RuntimeError, match="PM.exe"):
        p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert rem_hours(p6) == 120
    monkeypatch.setattr(p6.s, "_p6_running", lambda: False)
    res = p6.call("p6_update_activity", **UPD, dry_run=False, confirmation_token=dry["confirmation_token"])
    assert res["applied"] is True


def test_refused_dry_run_gives_no_token(p6):
    dry = p6.call("p6_delete_activity", project="DEMO-A", task_code="A1010")  # has an actual start
    assert dry["would_refuse"] and "confirmation_token" not in dry


def test_destructive_tool_needs_the_confirmation_text(p6):
    dry = p6.call("p6_delete_activity", project="DEMO-A", task_code="A1060")
    assert dry["confirmation_text_required"] == "DELETE ACTIVITY DEMO-A A1060"
    tok = dry["confirmation_token"]
    with pytest.raises(ValueError, match="confirmation_text"):
        p6.call("p6_delete_activity", project="DEMO-A", task_code="A1060", dry_run=False, confirmation_token=tok)
    with pytest.raises(ValueError, match="confirmation_text"):
        p6.call("p6_delete_activity", project="DEMO-A", task_code="A1060", dry_run=False, confirmation_token=tok,
                confirmation_text="yes")
    assert p6.sql("SELECT COUNT(*) FROM TASK WHERE task_code = 'A1060'")[0][0] == 1
    res = p6.call("p6_delete_activity", project="DEMO-A", task_code="A1060", dry_run=False, confirmation_token=tok,
                  confirmation_text="DELETE ACTIVITY DEMO-A A1060")
    assert res["applied"] is True
    assert p6.sql("SELECT COUNT(*) FROM TASK WHERE task_code = 'A1060'")[0][0] == 0


def test_delete_project_text_names_every_project(p6):
    dry = p6.call("p6_delete_project", project=["DEMO-A"])
    assert dry["confirmation_text_required"] == "DELETE PROJECT DEMO-A"


def test_backup_and_restore_round_trip(p6):
    res = p6.apply("p6_backup_database")
    name = os.path.basename(res["backup"])
    assert "_manual" in name
    p6.apply("p6_update_activity", **UPD)
    assert rem_hours(p6) == 96
    res = p6.apply("p6_restore_backup", name=name)
    assert res["applied"] is True and res["safety_backup"]
    assert rem_hours(p6) == 120


def test_a_backup_before_every_write_and_retention(p6):
    p6.set(backup_retention=2)
    p6.apply("p6_backup_database")  # manual: never pruned
    for days in (11, 12, 13):
        p6.apply("p6_update_activity", **dict(UPD, remaining_duration_days=days))
    plain = [n for n in os.listdir(p6.backups) if "_manual" not in n]
    assert len(plain) == 2  # three automatic backups taken, the oldest pruned
    assert len([n for n in os.listdir(p6.backups) if "_manual" in n]) == 1
    assert p6.call("p6_diagnose")["backups_pruned_last"]


def test_backup_can_be_turned_off(p6):
    p6.set(backup_before_write=False)
    p6.apply("p6_update_activity", **UPD)
    assert not os.path.isdir(p6.backups) or not os.listdir(p6.backups)


def test_edit_log_of_v2_folder_is_still_read(p6):
    legacy = os.path.join(os.path.dirname(p6.db), "p6_mcp_logs")
    os.makedirs(legacy)
    entry = {"ts": "2026-03-02 10:00:00.000", "commit": "c1", "db": "demo.db", "tool": "p6_update_activity",
             "operation": "update_activity", "project": "DEMO-A", "proj_id": 2, "summary": "old v2 edit",
             "stale": {}, "stale_projects": []}
    with open(os.path.join(legacy, "edits_20260302.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    p6.set()  # refresh: the legacy folder is derived from the database folder
    rows = p6.call("p6_edit_log")["rows"]
    assert any(r.get("summary") == "old v2 edit" for r in rows)
