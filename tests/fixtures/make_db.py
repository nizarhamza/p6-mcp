"""Synthetic P6 Professional standalone database for the tests.

Every value here is invented. The schema keeps only the tables and columns the server reads or
writes (names as P6 uses them, generic SQLite types); it is not a copy of P6's schema.

    python tests/fixtures/make_db.py out.db
"""

from __future__ import annotations

import os
import sqlite3
import sys

SCHEMA = {
    "PREFER": [
        ("prefer_id", "INTEGER PRIMARY KEY"), ("week_start_day_num", "INTEGER"), ("task_code_len", "INTEGER"),
        ("proj_short_len", "INTEGER"), ("wbs_short_len", "INTEGER"), ("rsrc_short_len", "INTEGER"),
        ("day_hr_cnt", "REAL"), ("week_hr_cnt", "REAL"), ("name_sep_char", "TEXT"), ("db_name", "TEXT"),
        ("curr_id", "INTEGER"), ("ev_compute_type", "TEXT"), ("create_date", "TEXT"), ("create_user", "TEXT"),
        ("update_date", "TEXT"), ("update_user", "TEXT"), ("delete_session_id", "INTEGER"),
        ("delete_date", "TEXT"),
    ],
    "CURRTYPE": [
        ("curr_id", "INTEGER PRIMARY KEY"), ("curr_short_name", "TEXT"), ("base_exch_rate", "REAL"),
        ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "OBS": [
        ("obs_id", "INTEGER PRIMARY KEY"), ("parent_obs_id", "INTEGER"), ("guid", "TEXT"), ("seq_num", "INTEGER"),
        ("obs_name", "TEXT"), ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"),
        ("update_user", "TEXT"), ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "CALENDAR": [
        ("clndr_id", "INTEGER PRIMARY KEY"), ("clndr_name", "TEXT"), ("proj_id", "INTEGER"),
        ("base_clndr_id", "INTEGER"), ("clndr_type", "TEXT"), ("day_hr_cnt", "REAL"), ("week_hr_cnt", "REAL"),
        ("clndr_data", "BLOB"), ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"),
        ("update_user", "TEXT"), ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "PROJECT": [
        ("proj_id", "INTEGER PRIMARY KEY"), ("project_flag", "TEXT"), ("name_sep_char", "TEXT"),
        ("def_complete_pct_type", "TEXT"), ("proj_short_name", "TEXT"), ("orig_proj_id", "INTEGER"),
        ("source_proj_id", "INTEGER"), ("base_type_id", "INTEGER"), ("clndr_id", "INTEGER"),
        ("sum_base_proj_id", "INTEGER"), ("critical_drtn_hr_cnt", "REAL"), ("last_recalc_date", "TEXT"),
        ("plan_start_date", "TEXT"), ("plan_end_date", "TEXT"), ("scd_end_date", "TEXT"), ("guid", "TEXT"),
        ("critical_path_type", "TEXT"), ("fintmpl_id", "INTEGER"), ("last_schedule_date", "TEXT"),
        ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "PROJPROP": [
        ("proj_id", "INTEGER"), ("prop_name", "TEXT"), ("prop_value", "BLOB"), ("create_date", "TEXT"),
        ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "PROJWBS": [
        ("wbs_id", "INTEGER PRIMARY KEY"), ("proj_id", "INTEGER"), ("obs_id", "INTEGER"), ("seq_num", "INTEGER"),
        ("proj_node_flag", "TEXT"), ("sum_data_flag", "TEXT"), ("status_code", "TEXT"), ("wbs_short_name", "TEXT"),
        ("wbs_name", "TEXT"), ("parent_wbs_id", "INTEGER"), ("ev_compute_type", "TEXT"), ("guid", "TEXT"),
        ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "TASK": [
        ("task_id", "INTEGER PRIMARY KEY"), ("proj_id", "INTEGER"), ("wbs_id", "INTEGER"), ("clndr_id", "INTEGER"),
        ("phys_complete_pct", "REAL"), ("complete_pct_type", "TEXT"), ("task_type", "TEXT"),
        ("status_code", "TEXT"), ("task_code", "TEXT"), ("task_name", "TEXT"), ("rsrc_id", "INTEGER"),
        ("total_float_hr_cnt", "REAL"), ("free_float_hr_cnt", "REAL"), ("remain_drtn_hr_cnt", "REAL"),
        ("target_drtn_hr_cnt", "REAL"), ("cstr_date", "TEXT"), ("act_start_date", "TEXT"),
        ("act_end_date", "TEXT"), ("late_start_date", "TEXT"), ("late_end_date", "TEXT"),
        ("expect_end_date", "TEXT"), ("early_start_date", "TEXT"), ("early_end_date", "TEXT"),
        ("restart_date", "TEXT"), ("reend_date", "TEXT"), ("target_start_date", "TEXT"),
        ("target_end_date", "TEXT"), ("cstr_type", "TEXT"), ("priority_type", "TEXT"), ("suspend_date", "TEXT"),
        ("resume_date", "TEXT"), ("float_path", "INTEGER"), ("float_path_order", "INTEGER"), ("guid", "TEXT"),
        ("cstr_date2", "TEXT"), ("cstr_type2", "TEXT"), ("driving_path_flag", "TEXT"), ("create_date", "TEXT"),
        ("update_date", "TEXT"), ("create_user", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "TASKPRED": [
        ("task_pred_id", "INTEGER PRIMARY KEY"), ("task_id", "INTEGER"), ("pred_task_id", "INTEGER"),
        ("proj_id", "INTEGER"), ("pred_proj_id", "INTEGER"), ("pred_type", "TEXT"), ("lag_hr_cnt", "REAL"),
        ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "NEXTKEY": [
        ("key_name", "TEXT PRIMARY KEY"), ("key_seq_num", "INTEGER"), ("create_date", "TEXT"),
        ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "RSRC": [
        ("rsrc_id", "INTEGER PRIMARY KEY"), ("parent_rsrc_id", "INTEGER"), ("clndr_id", "INTEGER"),
        ("role_id", "INTEGER"), ("guid", "TEXT"), ("rsrc_name", "TEXT"), ("rsrc_short_name", "TEXT"),
        ("curr_id", "INTEGER"), ("unit_id", "INTEGER"), ("rsrc_type", "TEXT"), ("create_date", "TEXT"),
        ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "ROLES": [
        ("role_id", "INTEGER PRIMARY KEY"), ("parent_role_id", "INTEGER"), ("seq_num", "INTEGER"),
        ("role_short_name", "TEXT"), ("role_name", "TEXT"), ("role_descr", "BLOB"), ("create_date", "TEXT"),
        ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "TASKRSRC": [
        ("taskrsrc_id", "INTEGER PRIMARY KEY"), ("task_id", "INTEGER"), ("proj_id", "INTEGER"),
        ("cost_qty_link_flag", "TEXT"), ("role_id", "INTEGER"), ("rsrc_id", "INTEGER"), ("remain_qty", "REAL"),
        ("target_qty", "REAL"), ("act_ot_qty", "REAL"), ("act_reg_qty", "REAL"), ("target_cost", "REAL"),
        ("act_reg_cost", "REAL"), ("act_ot_cost", "REAL"), ("remain_cost", "REAL"), ("act_start_date", "TEXT"),
        ("act_end_date", "TEXT"), ("restart_date", "TEXT"), ("reend_date", "TEXT"), ("target_start_date", "TEXT"),
        ("target_end_date", "TEXT"), ("guid", "TEXT"), ("rsrc_type", "TEXT"), ("create_user", "TEXT"),
        ("create_date", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "ACTVTYPE": [
        ("actv_code_type_id", "INTEGER PRIMARY KEY"), ("seq_num", "INTEGER"), ("actv_code_type", "TEXT"),
        ("proj_id", "INTEGER"), ("actv_code_type_scope", "TEXT"), ("create_date", "TEXT"), ("create_user", "TEXT"),
        ("update_date", "TEXT"), ("update_user", "TEXT"), ("delete_session_id", "INTEGER"),
        ("delete_date", "TEXT"),
    ],
    "ACTVCODE": [
        ("actv_code_id", "INTEGER PRIMARY KEY"), ("parent_actv_code_id", "INTEGER"),
        ("actv_code_type_id", "INTEGER"), ("actv_code_name", "TEXT"), ("short_name", "TEXT"),
        ("seq_num", "INTEGER"), ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"),
        ("update_user", "TEXT"), ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "TASKACTV": [
        ("task_id", "INTEGER"), ("actv_code_type_id", "INTEGER"), ("actv_code_id", "INTEGER"),
        ("proj_id", "INTEGER"), ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"),
        ("update_user", "TEXT"), ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "UDFTYPE": [
        ("udf_type_id", "INTEGER PRIMARY KEY"), ("table_name", "TEXT"), ("udf_type_label", "TEXT"),
        ("logical_data_type", "TEXT"), ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"),
        ("update_user", "TEXT"), ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "UDFVALUE": [
        ("udf_type_id", "INTEGER"), ("fk_id", "INTEGER"), ("proj_id", "INTEGER"), ("udf_date", "TEXT"),
        ("udf_number", "REAL"), ("udf_text", "TEXT"), ("udf_code_id", "INTEGER"), ("table_name", "TEXT"),
        ("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "MEMOTYPE": [
        ("memo_type_id", "INTEGER PRIMARY KEY"), ("seq_num", "INTEGER"), ("eps_flag", "TEXT"), ("proj_flag", "TEXT"),
        ("wbs_flag", "TEXT"), ("task_flag", "TEXT"), ("memo_type", "TEXT"), ("create_date", "TEXT"),
        ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
        ("delete_session_id", "INTEGER"), ("delete_date", "TEXT"),
    ],
    "TASKMEMO": [
        ("memo_id", "INTEGER PRIMARY KEY"), ("task_id", "INTEGER"), ("memo_type_id", "INTEGER"),
        ("proj_id", "INTEGER"), ("task_memo", "BLOB"), ("create_date", "TEXT"), ("create_user", "TEXT"),
        ("update_date", "TEXT"), ("update_user", "TEXT"), ("delete_session_id", "INTEGER"),
        ("delete_date", "TEXT"),
    ],
    "REFRDEL": [
        ("delete_date", "TEXT"), ("table_name", "TEXT"), ("proj_id", "INTEGER"),
    ],
}
SCHEMA["PREFER"] += [("database_version", "TEXT"), ("min_pro_version", "TEXT")]
_STAMPS = [("create_date", "TEXT"), ("create_user", "TEXT"), ("update_date", "TEXT"), ("update_user", "TEXT"),
           ("delete_session_id", "INTEGER"), ("delete_date", "TEXT")]
SCHEMA["FINTMPL"] = [("fintmpl_id", "INTEGER PRIMARY KEY"), ("fintmpl_name", "TEXT")] + _STAMPS
SCHEMA["RSRCRATE"] = [("rsrc_rate_id", "INTEGER PRIMARY KEY"), ("rsrc_id", "INTEGER"),
                      ("cost_per_qty", "REAL")] + _STAMPS
# Activity steps, expenses, role prices and risks (XER round trip, 3.5.0)
SCHEMA["TASKPROC"] = [("proc_id", "INTEGER PRIMARY KEY"), ("task_id", "INTEGER"), ("proj_id", "INTEGER"),
                      ("seq_num", "INTEGER"), ("proc_name", "TEXT"), ("complete_flag", "TEXT"), ("proc_wt", "REAL"),
                      ("complete_pct", "REAL"), ("proc_descr", "BLOB")] + _STAMPS
SCHEMA["COSTTYPE"] = [("cost_type_id", "INTEGER PRIMARY KEY"), ("seq_num", "INTEGER"), ("cost_type", "TEXT")] + _STAMPS
SCHEMA["PROJCOST"] = [("cost_item_id", "INTEGER PRIMARY KEY"), ("acct_id", "INTEGER"), ("cost_type_id", "INTEGER"),
                      ("proj_id", "INTEGER"), ("task_id", "INTEGER"), ("cost_name", "TEXT"), ("po_number", "TEXT"),
                      ("vendor_name", "TEXT"), ("act_cost", "REAL"), ("cost_per_qty", "REAL"), ("remain_cost", "REAL"),
                      ("target_cost", "REAL"), ("cost_load_type", "TEXT"), ("auto_compute_act_flag", "TEXT"),
                      ("target_qty", "REAL"), ("qty_name", "TEXT"), ("cost_descr", "BLOB")] + _STAMPS
SCHEMA["ROLERATE"] = [("role_rate_id", "INTEGER PRIMARY KEY"), ("role_id", "INTEGER"), ("cost_per_qty", "REAL"),
                      ("start_date", "TEXT"), ("max_qty_per_hr", "REAL")] + _STAMPS
SCHEMA["RISKTYPE"] = [("risk_type_id", "INTEGER PRIMARY KEY"), ("seq_num", "INTEGER"), ("risk_type", "TEXT"),
                      ("parent_risk_type_id", "INTEGER")] + _STAMPS
SCHEMA["PROJRISK"] = [("risk_id", "INTEGER PRIMARY KEY"), ("risk_code", "TEXT"), ("proj_id", "INTEGER"),
                      ("status_code", "TEXT"), ("risk_name", "TEXT"), ("risk_type_id", "INTEGER"),
                      ("rsrc_id", "INTEGER"), ("identified_by_id", "INTEGER"),
                      ("risk_desc", "TEXT"), ("risk_descr", "BLOB")] + _STAMPS
SCHEMA["TASKRISK"] = [("task_id", "INTEGER"), ("risk_id", "INTEGER"), ("proj_id", "INTEGER")] + _STAMPS

STAMP = "2026-01-05 09:00:00"
DATA_DATE = "2026-03-02 08:00:00"
# Monday to Friday 08:00-12:00 and 13:00-17:00, one holiday (2026-04-06 = serial 46118)
CAL_STD = ("(0||CalendarData()((0||DaysOfWeek()((0||1()())"
           + "".join(f"(0||{d}()((0||0(s|08:00|f|12:00)())(0||1(s|13:00|f|17:00)())))" for d in range(2, 7))
           + "(0||7()())))(0||Exceptions()((0||0(d|46118)())))))")
SCHED = ("(0||(sched_calendar_on_relationship_lag|rcal_Predecessor|sched_retained_logic|Y|"
         "sched_progress_override|N|sched_lag_early_start_flag|Y|sched_float_type|FT_TotFloat)())")

# task_id, code, name, wbs, type, status, orig h, rem h, ES, EF, LS, LF, TF h, AS, AF
ACTIVITIES = [
    (101, "A1000", "Notice to proceed", 11, "TT_Mile", "TK_Complete", 0, 0,
     "2026-02-02 08:00:00", "2026-02-02 08:00:00", "2026-02-02 08:00:00", "2026-02-02 08:00:00", 0,
     "2026-02-02 08:00:00", "2026-02-02 08:00:00"),
    (102, "A1010", "Detailed design", 11, "TT_Task", "TK_Active", 160, 80,
     "2026-03-02 08:00:00", "2026-03-13 17:00:00", "2026-03-02 08:00:00", "2026-03-13 17:00:00", 0,
     "2026-02-02 08:00:00", None),
    (103, "A1020", "Procurement", 11, "TT_Task", "TK_NotStart", 120, 120,
     "2026-03-16 08:00:00", "2026-04-03 17:00:00", "2026-03-16 08:00:00", "2026-04-03 17:00:00", 0, None, None),
    (104, "A1030", "Site works", 12, "TT_Task", "TK_NotStart", 200, 200,
     "2026-04-07 08:00:00", "2026-05-11 17:00:00", "2026-04-07 08:00:00", "2026-05-11 17:00:00", 0, None, None),
    (105, "A1040", "Commissioning", 12, "TT_Task", "TK_NotStart", 80, 80,
     "2026-05-12 08:00:00", "2026-05-25 17:00:00", "2026-05-12 08:00:00", "2026-05-25 17:00:00", 0, None, None),
    (106, "A1050", "Project complete", 12, "TT_FinMile", "TK_NotStart", 0, 0,
     "2026-05-25 17:00:00", "2026-05-25 17:00:00", "2026-05-25 17:00:00", "2026-05-25 17:00:00", 0, None, None),
    (107, "A1060", "Permits", 12, "TT_Task", "TK_NotStart", 40, 40,
     "2026-03-02 08:00:00", "2026-03-06 17:00:00", "2026-03-30 08:00:00", "2026-04-03 17:00:00", 160, None, None),
]
LINKS = [(201, 102, 101), (202, 103, 102), (203, 104, 103), (204, 105, 104), (205, 106, 105), (206, 107, 101),
         (207, 104, 107)]  # task_pred_id, successor, predecessor (FS, no lag)


def memo(text: str) -> bytes:
    """Memo text as P6 stores it in a BLOB: UTF-8 BOM, the HTML, a NUL."""
    return b"\xef\xbb\xbf" + text.encode("utf-8") + b"\x00"


def _insert(con, table, **values):
    cols = {c for c, _ in SCHEMA[table]}
    values = {k: v for k, v in values.items() if k in cols}
    for k in ("create_date", "update_date"):
        if k in cols:
            values.setdefault(k, STAMP)
    for k in ("create_user", "update_user"):
        if k in cols:
            values.setdefault(k, "admin")
    con.execute(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                list(values.values()))


def build(path: str) -> str:
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    for table, cols in SCHEMA.items():
        con.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in cols)})")
    _insert(con, "PREFER", prefer_id=1, week_start_day_num=1, task_code_len=20, proj_short_len=40,
            wbs_short_len=20, day_hr_cnt=8, week_hr_cnt=40, name_sep_char=".", curr_id=1,
            ev_compute_type="EC_Cmp_pct", database_version="SYNTHETIC,2512.0000.0000.0001",
            min_pro_version="25.10.00")
    _insert(con, "CURRTYPE", curr_id=1, curr_short_name="USD", base_exch_rate=1.0)
    _insert(con, "OBS", obs_id=1, obs_name="Enterprise", seq_num=0)
    _insert(con, "CALENDAR", clndr_id=1, clndr_name="Standard 5 day", clndr_type="CA_Base", day_hr_cnt=8,
            week_hr_cnt=40, clndr_data=CAL_STD.encode("utf-8"))
    # EPS node 1 (project_flag N + its WBS node), project 2 below it
    _insert(con, "PROJECT", proj_id=1, project_flag="N", proj_short_name="EPS-DEMO", clndr_id=1)
    _insert(con, "PROJWBS", wbs_id=1, proj_id=1, obs_id=1, seq_num=0, proj_node_flag="Y", wbs_short_name="EPS-DEMO",
            wbs_name="Demo EPS", parent_wbs_id=None, status_code="WS_Open")
    _insert(con, "PROJECT", proj_id=2, project_flag="Y", proj_short_name="DEMO-A", clndr_id=1, name_sep_char=".",
            def_complete_pct_type="CP_Drtn", critical_drtn_hr_cnt=0, critical_path_type="CT_TotFloat",
            last_recalc_date=DATA_DATE, last_schedule_date="2026-03-02 09:15:00",
            plan_start_date="2026-02-02 08:00:00", plan_end_date=None, scd_end_date="2026-05-25 17:00:00")
    _insert(con, "PROJPROP", proj_id=2, prop_name="scheduling", prop_value=SCHED)
    _insert(con, "PROJWBS", wbs_id=10, proj_id=2, obs_id=1, seq_num=0, proj_node_flag="Y", wbs_short_name="DEMO-A",
            wbs_name="Demo project A", parent_wbs_id=1, status_code="WS_Open", ev_compute_type="EC_Cmp_pct")
    _insert(con, "PROJWBS", wbs_id=11, proj_id=2, obs_id=1, seq_num=1, proj_node_flag="N", wbs_short_name="ENG",
            wbs_name="Engineering", parent_wbs_id=10, status_code="WS_Open", ev_compute_type="EC_Cmp_pct")
    _insert(con, "PROJWBS", wbs_id=12, proj_id=2, obs_id=1, seq_num=2, proj_node_flag="N", wbs_short_name="CON",
            wbs_name="Construction", parent_wbs_id=10, status_code="WS_Open", ev_compute_type="EC_Cmp_pct")
    for (tid, code, name, wbs, ttype, status, orig, rem, es, ef, ls, lf, tf, ast, aend) in ACTIVITIES:
        pct = 100.0 if status == "TK_Complete" else (50.0 if status == "TK_Active" else 0.0)
        _insert(con, "TASK", task_id=tid, proj_id=2, wbs_id=wbs, clndr_id=1, phys_complete_pct=pct,
                complete_pct_type="CP_Drtn", task_type=ttype, status_code=status, task_code=code, task_name=name,
                total_float_hr_cnt=None if status == "TK_Complete" else tf,
                free_float_hr_cnt=None if status == "TK_Complete" else 0, remain_drtn_hr_cnt=rem,
                target_drtn_hr_cnt=orig, act_start_date=ast, act_end_date=aend, early_start_date=es,
                early_end_date=ef, late_start_date=ls, late_end_date=lf, target_start_date=es,
                target_end_date=ef, priority_type="PT_Normal", driving_path_flag="N" if tf else "Y")
    for pid, succ, pred in LINKS:
        _insert(con, "TASKPRED", task_pred_id=pid, task_id=succ, pred_task_id=pred, proj_id=2, pred_proj_id=2,
                pred_type="PR_FS", lag_hr_cnt=0)
    # notebooks (one BLOB as P6 writes them, one TEXT), steps, expenses, roles and risks of DEMO-A
    for mid, name in ((1, "Scope"), (2, "Site notes")):
        _insert(con, "MEMOTYPE", memo_type_id=mid, seq_num=mid, eps_flag="Y", proj_flag="Y", wbs_flag="Y",
                task_flag="Y", memo_type=name)
    _insert(con, "TASKMEMO", memo_id=1, task_id=102, memo_type_id=1, proj_id=2,
            task_memo=memo('<html><body>Design "basis" v2\n<p>Two lines &amp; a quote</p></body></html>'))
    _insert(con, "TASKMEMO", memo_id=2, task_id=104, memo_type_id=2, proj_id=2,
            task_memo="<p>Access by the north gate</p>")
    for sid, seq, name, done, wt, descr in ((1, 10, "Concept", "Y", 1.0, memo("<p>Agreed with the client</p>")),
                                            (2, 20, "Detail", "N", 2.0, None), (3, 30, "Approval", "N", 1.0, None)):
        _insert(con, "TASKPROC", proc_id=sid, task_id=102, proj_id=2, seq_num=seq, proc_name=name, complete_flag=done,
                proc_wt=wt, complete_pct=100.0 if done == "Y" else 0.0, proc_descr=descr)
    for cid, name in ((1, "Materials"), (2, "Travel")):
        _insert(con, "COSTTYPE", cost_type_id=cid, seq_num=cid, cost_type=name)
    _insert(con, "PROJCOST", cost_item_id=1, cost_type_id=1, proj_id=2, task_id=103, cost_name="Switchgear", acct_id=99,
            po_number="PO-17", vendor_name="Vendor A", act_cost=0.0, cost_per_qty=2500.0, remain_cost=10000.0,
            target_cost=10000.0, cost_load_type="CL_Uniform", auto_compute_act_flag="N", target_qty=4.0, qty_name="ea",
            cost_descr=memo("<p>Four panels</p>"))
    _insert(con, "PROJCOST", cost_item_id=2, cost_type_id=2, proj_id=2, task_id=104, cost_name="Site visits",
            act_cost=0.0, cost_per_qty=0.0, remain_cost=800.0, target_cost=800.0, cost_load_type="CL_End",
            auto_compute_act_flag="N", target_qty=0.0)
    for rid, parent, short, name in ((1, None, "ENG", "Engineers"), (2, 1, "LEAD", "Lead engineer"),
                                     (3, None, "LEAD", "Site lead")):  # the Role ID LEAD twice, under other parents
        _insert(con, "ROLES", role_id=rid, parent_role_id=parent, seq_num=rid, role_short_name=short, role_name=name)
    _insert(con, "ROLERATE", role_rate_id=1, role_id=2, cost_per_qty=95.0, start_date="2026-01-01 00:00:00",
            max_qty_per_hr=1.0)
    _insert(con, "RISKTYPE", risk_type_id=1, seq_num=1, risk_type="Technical", parent_risk_type_id=None)
    _insert(con, "RISKTYPE", risk_type_id=2, seq_num=2, risk_type="Design", parent_risk_type_id=1)
    _insert(con, "PROJRISK", risk_id=1, risk_code="R-01", proj_id=2, status_code="RS_Open",
            risk_name="Late design data", risk_type_id=2, risk_desc="Vendor drawings late",
            risk_descr=memo("<p>Watch list</p>"))
    _insert(con, "TASKRISK", task_id=104, risk_id=1, proj_id=2)
    for key, n in (("project_proj_id", 10), ("projwbs_wbs_id", 50), ("task_task_id", 200),
                   ("taskpred_task_pred_id", 300), ("calendar_clndr_id", 10), ("rsrc_rsrc_id", 10),
                   ("taskrsrc_taskrsrc_id", 10), ("memotype_memo_type_id", 10), ("taskmemo_memo_id", 10),
                   ("taskproc_proc_id", 10), ("costtype_cost_type_id", 10), ("projcost_cost_item_id", 10),
                   ("roles_role_id", 10), ("rolerate_role_rate_id", 10), ("risktype_risk_type_id", 10),
                   ("projrisk_risk_id", 10)):
        _insert(con, "NEXTKEY", key_name=key, key_seq_num=n)
    con.commit()
    con.close()
    return path


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "synthetic_p6.db"))
