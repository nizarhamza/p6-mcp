"""Tool registration: the parameter types MCP clients see, the MCP server object, _tool (hints, policy gate,
SDK wrapper) and _gated (two-step write flow), the prompts of the P6 skills.

The SDK resolves a tool's string annotations against the globals of the module that defines the tool: each
tool module imports the parameter types it uses from here."""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import os
import sqlite3
from typing import Annotated, Any, Callable, Literal, Mapping, Optional, Sequence, TypedDict, Union, get_args

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP
try:  # mcp >= 1.8 (1.x and 2.x)
    from mcp.types import ToolAnnotations
except ImportError:  # older mcp or the test stub: tools are registered without hints
    ToolAnnotations = None
try:  # the only exception whose text mcp 2.x passes to the client
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:
    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:  # test stub: tools are called directly and raise their own errors
        ToolError = None
try:  # always installed with mcp
    from pydantic import Field
except ImportError:  # test stub without pydantic: annotations are never evaluated
    def Field(*_a, **_k):
        return None

from p6mcp import __version__ as _pkg_version
from p6mcp import config as _config
from p6mcp import confirm as _confirm
from p6mcp import instructions as _instructions
from p6mcp import policy as _policy
from p6mcp import skills as _skills

from p6mcp.core import state as _state
from p6mcp.core.state import _refresh, _ro_cons, _shop_on
from p6mcp.core.constants import CAL_CHECK_KINDS, CSTR_TYPES, EPPM_EDIT_TOOLS, MAX_ROWS, REL_TYPES, XER_READ_ONLY


# Parameter types shown to MCP clients (module level: the SDK evaluates the string annotations).
# The tools keep their own runtime checks because tests and internal calls bypass validation.
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
DATETIME_PATTERN = r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$"  # what _parse_dt accepts
Limit = Annotated[int, Field(ge=1, le=MAX_ROWS, description="Rows per page (max 200).")]
Offset = Annotated[int, Field(ge=0, description="Rows to skip: pass next_offset from the previous page.")]
ProjectId = Annotated[str, Field(min_length=1, description="Project ID as shown in P6 (p6_list_projects).")]
ActivityId = Annotated[str, Field(min_length=1, description="Activity ID (task_code).")]
Status = Literal["TK_NotStart", "TK_Active", "TK_Complete"]
RelType = Literal["FS", "SS", "FF", "SF"]
Direction = Literal["pred", "succ", "both"]
ImportMode = Literal["new", "update", "baseline"]
CpMode = Literal["project", "total_float", "longest_path"]
Removable = Literal["TASKPRED", "TASKRSRC"]
CstrName = Literal["none", "start_on_or_after", "start_on_or_before", "start_on", "finish_on_or_after",
                   "finish_on_or_before", "finish_on", "mandatory_start", "mandatory_finish",
                   "as_late_as_possible"]
XerTable = Literal["COSTTYPE", "CURRTYPE", "FINTMPL", "MEMOTYPE", "OBS", "RISKTYPE", "ROLES", "UDFTYPE", "PROJECT",
                   "CALENDAR", "SCHEDOPTIONS", "PROJWBS", "RSRC", "ACTVTYPE", "PROJRISK", "ROLERATE", "RSRCRATE",
                   "TASK", "ACTVCODE", "PROJCOST", "TASKMEMO", "TASKPRED", "TASKPROC", "TASKRSRC", "TASKACTV",
                   "TASKRISK", "UDFVALUE"]
DateStr = Annotated[str, Field(pattern=DATE_PATTERN, description="YYYY-MM-DD")]
DateTimeStr = Annotated[str, Field(pattern=DATETIME_PATTERN, description="YYYY-MM-DD HH:MM")]
Pct = Annotated[float, Field(ge=0, le=100)]
NonNegDays = Annotated[float, Field(ge=0)]
NonNegQty = Annotated[float, Field(ge=0, description="Units as P6 stores them (hours for labor).")]
Depth = Annotated[int, Field(ge=0, description="Levels below the start node (0 = the node only).")]
PathNo = Annotated[int, Field(ge=1, description="Float path number (1 = most critical).")]
BaselineDates = Literal["current", "planned"]  # baseline finish: actual else early, or its planned finish
DurationBasis = Literal["remaining", "original"]
CompareSection = Literal["activities", "relationships"]
DcmaCheck = Literal["logic", "logic_density", "leads", "lags", "long_lags", "fs_links", "hard_constraints", "high_float",
                    "negative_float", "high_duration", "invalid_dates", "resources", "missed_tasks",
                    "critical_path_test", "cpli", "bei"]
ThresholdCheck = Literal[tuple(c for c in get_args(DcmaCheck) if c != "critical_path_test")]  # not run: no threshold
Thresholds = Annotated[dict[ThresholdCheck, float], Field(description="Threshold per check, e.g. {'hard_constraints': 1}.")]
SampleSize = Annotated[int, Field(ge=0, le=50, description="Sample rows per check (max 50).")]
GroupBy = Literal["status", "task_type", "wbs", "code"]
EpsOrder = Literal["seq", "tree"]  # p6_list_eps: P6 sequence order, or the tree (each node before its children)
ProjOrder = Literal["project", "eps"]  # p6_list_projects: by Project ID, or by EPS (tree order) then Project ID
Period = Literal["month", "week"]
Measure = Literal["units", "cost"]
Level = Annotated[int, Field(ge=1, description="Hierarchy level (1 = top).")]
WindowDays = Annotated[int, Field(ge=1, le=366, description="Window length in calendar days (max 366).")]
WindowWeeks = Annotated[int, Field(ge=1, le=52, description="Window length in weeks (max 52).")]
CalCheckKind = Literal["calendar_missing", "unreadable", "base_missing", "no_working_time", "rsrc_dep_no_calendar",
                       "project_holiday_worked", "holiday_not_in_project", "no_exceptions_in_year", "day_hours_stale",
                       "week_hours_stale", "work_week_differs"]
assert tuple(CAL_CHECK_KINDS) == get_args(CalCheckKind)
CalCheckKinds = Annotated[list[CalCheckKind], Field(description="Finding kinds to list (default all).")]
Year = Annotated[int, Field(ge=1900, le=2200, description="Calendar year, e.g. 2026.")]
LaGrid = Literal["auto", "day", "week"]
FloatEdges = Annotated[list[float], Field(min_length=1, description="Bucket edges in days, increasing; 0 is always added.")]
ProjectIds = Annotated[list[ProjectId], Field(description="Project IDs (earlier updates of the same schedule).")]
ActivityIds = Annotated[list[ActivityId], Field(description="Activity IDs (task_code).")]
SchedValues = Annotated[dict[str, Any], Field(description="Option name -> value (names as p6_schedule_options shows them).")]
LevelPriority = Annotated[list[str], Field(description="Leveling priorities, 'field:ASC' or 'field:DESC', e.g. ['priority_type:ASC'].")]


class _NewEpsKeys(TypedDict):
    eps_id: str
    eps_name: str


class NewEps(_NewEpsKeys, total=False):
    """EPS node p6_import_xer creates (or reuses when eps_id exists) for a new project."""
    parent_eps: str


ProjectList = Annotated[Union[str, list[str]], Field(description="Project ID, or a list of Project IDs.")]
# Parameters the server adds to every write tool (two-step flow, see p6mcp.confirm)
DryRun = Annotated[bool, Field(description="true (default): preview only, returns the plan and a confirmation_token. "
                                           "false: apply the previewed change (needs confirmation_token).")]
ConfirmToken = Annotated[str, Field(description="confirmation_token returned by the dry run of this same call.")]
ConfirmText = Annotated[str, Field(description="The exact confirmation_text_required given by the dry run.")]
LegacyConfirm = Annotated[bool, Field(description="Deprecated alias of dry_run=false (still needs the token).")]
UdfOp = Literal["eq", "ne", "lt", "le", "gt", "ge", "contains", "empty", "not_empty"]
UdfValue = Annotated[Union[str, float], Field(description="Text, number, 'YYYY-MM-DD[ HH:MM]' date, indicator "
                                                          "(UDF_G0 to UDF_G4) or code short name, per the UDF type.")]


class _UdfFilterKeys(TypedDict):
    udf: str


class UdfFilter(_UdfFilterKeys, total=False):
    """Activities whose UDF value matches: op (default eq) against value (not for empty / not_empty)."""
    op: UdfOp
    value: Union[str, float]


UdfFilters = Annotated[list[UdfFilter], Field(description="UDF conditions, all must hold.")]
UdfNames = Annotated[list[str], Field(description="UDF labels (as in P6 columns) to add to each row under 'udfs'.")]
# Activity list row: the one list of field names (rows, fields= projection, MCP schema)
ActField = Literal["task_code", "task_name", "status_code", "task_type", "wbs_name",
                   "early_start_date", "early_end_date", "late_start_date", "late_end_date",
                   "act_start_date", "act_end_date", "target_start_date", "target_end_date",
                   "cstr_type", "cstr_label", "cstr_date", "phys_complete_pct", "driving_path_flag",
                   "orig_dur_d", "rem_dur_d", "total_float_d", "free_float_d", "float_path", "float_path_order"]
ActFields = Annotated[list[ActField], Field(description="Fields to return (task_code always first); default all.")]
Detail = Literal["full", "brief"]  # rows with every key (fixed shape), or without the null values
DetailArg = Annotated[Detail, Field(description="brief: rows without their null values; full (default): every key.")]
Explain = Annotated[bool, Field(description="True: also return the fixed texts in words (see the description).")]
ACT_FIELDS: tuple[str, ...] = get_args(ActField)
ACT_HOURS = {"orig_dur_d": "target_drtn_hr_cnt", "rem_dur_d": "remain_drtn_hr_cnt",
             "total_float_d": "total_float_hr_cnt", "free_float_d": "free_float_hr_cnt"}  # day fields, from hours
assert set(get_args(CstrName)) == set(CSTR_TYPES)
assert set(get_args(RelType)) == set(REL_TYPES)
assert set(ACT_HOURS) <= set(ACT_FIELDS)

# built-in lines the user kept plus their own ones (Settings, Instructions page), fixed at startup
INSTRUCTIONS = _instructions.build(_state.STARTUP.raw)

SERVER_VERSION = _pkg_version  # reported to clients as serverInfo.version where the SDK supports it
_version_kw = {"version": SERVER_VERSION} if "version" in inspect.signature(FastMCP.__init__).parameters else {}
mcp = FastMCP("primavera-p6", instructions=INSTRUCTIONS, **_version_kw)  # keywords: positional order differs in 1.x and 2.x


def _eppm_refusal(tool: str) -> str:
    if tool == "p6_delete_activity":
        return ("Activities are not deleted through a P6 EPPM working copy in this version: delete it in P6 EPPM, "
                "then pull again.")
    if tool in ("p6_update_activity_codes", "p6_move_activities"):
        what = "Activity code values" if tool == "p6_update_activity_codes" else "WBS moves"
        return (f"{what} are not sent to the P6 EPPM server by the push in this version ({tool} is not available on a "
                "P6 EPPM working copy): change them in P6 EPPM, then pull again.")
    if tool == "p6_add_activities":
        return ("Activities are not created through a P6 EPPM working copy in this version: add them in P6 EPPM, "
                "then pull again.")
    return (f"{tool} is not available on a P6 EPPM working copy: the push to the server supports activity, "
            "relationship and assignment edits only. Do it in P6 EPPM, then pull again.")


TOOL_HINTS: dict[str, dict[str, bool]] = {}
HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")  # MCP tool annotations, in order
REGISTERED: dict[str, Callable] = {}  # tool -> what a client call runs (policy gate + two-step write flow)
LISTED: set[str] = set()  # tools offered to clients (fixed at startup)
TOKENS = _confirm.TokenStore()
# Context of the client call being run (two-step flow): {tool, params, token, expected_plan, plan}.
# Direct calls (tests, internal use) have none and keep the plain confirm=True behaviour.
_CALL: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("p6_call", default=None)
WRITE_DOC = ("\n\nTwo steps: dry_run=true (default) writes nothing and returns the plan and a confirmation_token. "
             "Show the plan; only after the user's explicit yes, call again with the same arguments, dry_run=false "
             "and that token.")
DESTRUCTIVE_DOC = " This tool deletes or replaces data: step 2 also needs confirmation_text, given by the dry run."


def _before_call(tool: str) -> _config.Settings:
    """Settings of this call; raises when the policy refuses the tool or no database is usable."""
    st = _refresh()
    if st.xer_files and _policy.is_write(tool):
        raise ValueError(XER_READ_ONLY)
    if st.eppm and _policy.is_write(tool) and tool not in EPPM_EDIT_TOOLS:
        raise ValueError(_eppm_refusal(tool))
    why = _policy.refusal(tool, st.tool_profile, st.allow_writes, _shop_on(st))
    if why:
        raise ValueError(why)
    if not _state.DB_PATH and tool not in _policy.NO_DB_TOOLS:
        raise RuntimeError(st.db_problem or _config.NO_DB_MESSAGE)
    return st


def _plan_gate(plan: Sequence, confirm: bool) -> Optional[str]:
    """Two-step flow: a dry run records the digest of its plan (the token is bound to it); an apply
    call must rebuild the same plan, else the change is refused (returned message) before commit."""
    ctx = _CALL.get()
    if ctx is None:
        return None
    d = _confirm.plan_digest(plan)
    if not confirm:
        ctx["plan"] = d
        return None
    if ctx.get("expected_plan") is not None and ctx["expected_plan"] != d:
        ctx["plan_mismatch"] = True
        return ("The database changed since the dry run: the plan is different now. Nothing was written. Run the "
                "dry run again, show the new plan to the user and confirm it.")
    return None


def _confirm_text(tool: str, params: dict) -> str:
    """Text a destructive call must repeat (built from its arguments, so it names what goes)."""
    if tool == "p6_delete_project":
        p = params.get("project")
        return "DELETE PROJECT " + ", ".join([p] if isinstance(p, str) else [str(x) for x in p or []])
    if tool == "p6_delete_activity":
        return f"DELETE ACTIVITY {params.get('project')} {params.get('task_code')}"
    if tool == "p6_restore_backup":
        return f"RESTORE {params.get('name')}"
    if tool == "p6_eppm_push":
        return f"PUSH TO {_state.SETTINGS.eppm}" + (" AND SCHEDULE" if params.get("schedule") else "")
    raise KeyError(tool)


def _preview(tool: str, params: dict) -> dict:
    """Dry run of a write tool that has no confirm parameter of its own."""
    from p6mcp.core.db import _backup_dir, _write_refusal
    if tool == "p6_backup_database":
        why = _write_refusal()
        if why:
            raise RuntimeError(why)
        plan = [f"Copy {os.path.basename(_state.DB_PATH)} ({os.path.getsize(_state.DB_PATH) / 1048576:.1f} MB) to {_backup_dir()}"]
        _plan_gate(plan, False)
        return {"dry_run": True, "would_do": plan}
    raise ValueError(f"{tool} has no dry run")


def _gated(fn: Callable) -> Callable:
    """The callable clients reach: policy check on every call; for write tools the two-step flow
    (dry_run, confirmation_token, confirmation_text) on top of the tool's own confirm logic."""
    name = fn.__name__
    kind = _policy.kind(name)
    if kind in (_policy.READ, _policy.FILE):
        @functools.wraps(fn)
        def call(*a, **k):
            _before_call(name)
            return fn(*a, **k)
        return call

    sig = inspect.signature(fn)
    has_confirm = "confirm" in sig.parameters
    destructive = kind == _policy.DESTRUCTIVE
    settings_only = kind == _policy.CONFIG  # changes MCP for Primavera P6's settings: the token is bound to the config file

    def scope() -> str:
        return (_state.SETTINGS.config_path or _config.config_path()) if settings_only else _state.DB_PATH

    @functools.wraps(fn)
    def call(*a, dry_run: bool = True, confirmation_token: Optional[str] = None,
             confirmation_text: Optional[str] = None, **k):
        _before_call(name)
        ba = sig.bind(*a, **k)
        ba.apply_defaults()
        params = dict(ba.arguments)
        apply = bool(params.pop("confirm", False)) or dry_run is False
        ctx = {"tool": name, "params": params, "token": None, "expected_plan": None, "plan": None}
        if not apply:
            tok = _CALL.set(ctx)
            try:
                res = fn(**params, confirm=False) if has_confirm else _preview(name, params)
            finally:
                _CALL.reset(tok)
            if isinstance(res, dict) and res.get("dry_run") and not res.get("would_refuse"):
                g = TOKENS.issue(name, params, scope(), ctx["plan"] or _confirm.plan_digest(res.get("would_do") or []),
                                 owner=_confirm.OWNER.get())
                res["confirmation_token"] = g.token
                res["token_expires_in_s"] = int(TOKENS.ttl)
                extra = ""
                if destructive:
                    res["confirmation_text_required"] = _confirm_text(name, params)
                    extra = f", confirmation_text=\"{res['confirmation_text_required']}\""
                res["next"] = (f"Show this plan to the user. Only after their explicit yes, call {name} again with the "
                               f"same arguments plus dry_run=false, confirmation_token=\"{g.token}\"{extra}."
                               + ("" if settings_only or _state.SETTINGS.eppm else " P6 must be closed."))
            return res
        if destructive:
            want = _confirm_text(name, params)
            if (confirmation_text or "").strip() != want:
                raise ValueError(f"{name} deletes or replaces data: pass confirmation_text=\"{want}\" exactly, as the "
                                 "dry run asks.")
        g = TOKENS.check(name, params, scope(), confirmation_token, owner=_confirm.OWNER.get())
        ctx.update(token=g.token, expected_plan=g.plan)
        tok = _CALL.set(ctx)
        try:
            res = fn(**params, confirm=True) if has_confirm else fn(**params)
        finally:
            _CALL.reset(tok)
        if ctx.get("plan_mismatch") or not (isinstance(res, dict) and res.get("applied") is False):
            TOKENS.consume(g.token)
        return res

    # The SDK reads __signature__ as it is (no string evaluation): give it evaluated annotations.
    ev = inspect.signature(fn, eval_str=True)
    ps = [p.replace(annotation=LegacyConfirm) if p.name == "confirm" else p for p in ev.parameters.values()]
    P = inspect.Parameter
    ps += [P("dry_run", P.POSITIONAL_OR_KEYWORD, default=True, annotation=DryRun),
           P("confirmation_token", P.POSITIONAL_OR_KEYWORD, default=None, annotation=Optional[ConfirmToken])]
    if destructive:
        ps.append(P("confirmation_text", P.POSITIONAL_OR_KEYWORD, default=None, annotation=Optional[ConfirmText]))
    call.__signature__ = ev.replace(parameters=ps)
    call.__doc__ = (fn.__doc__ or "").rstrip() + WRITE_DOC + (DESTRUCTIVE_DOC if destructive else "")
    return call


def _json_text(obj: Any) -> str:
    """Compact JSON of a tool result, as the client receives it."""
    try:
        import pydantic_core  # what the SDK serializes with (same value formats), installed with mcp
    except ImportError:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)
    return pydantic_core.to_json(obj, fallback=str).decode()


def _wire(res: Any) -> Any:
    """A tool result for the SDK: dicts and lists as compact JSON text (the SDK would indent them by two
    spaces, 13 to 27 % of the bytes and so of the client's context); anything else as it is."""
    return _json_text(res) if isinstance(res, (dict, list)) else res


def _slim_schema(server, name: str) -> None:
    """Trim the input schema clients receive for a registered tool (about 15 KB of the full tool list):
    pydantic's generated 'title' keys, which repeat each parameter's name, and the deprecated `confirm`
    alias (still accepted: the SDK validates arguments with its own model, not with this schema)."""
    try:
        params = server._tool_manager.get_tool(name).parameters
    except AttributeError:  # an SDK without this layout: leave the schema as it is
        return

    def strip(node):
        if isinstance(node, dict):
            if isinstance(node.get("title"), str):
                del node["title"]
            for v in node.values():
                strip(v)
        elif isinstance(node, list):
            for v in node:
                strip(v)
    strip(params)
    props = params.get("properties")
    if isinstance(props, dict) and "confirm" in props and "dry_run" in props:
        del props["confirm"]
        if isinstance(params.get("required"), list) and "confirm" in params["required"]:
            params["required"].remove("confirm")


def _sdk_tool_kw(server) -> dict:
    """structured_output=False where the SDK has it (1.10+): results are text, never also structuredContent."""
    try:
        return {"structured_output": False} if "structured_output" in inspect.signature(server.tool).parameters else {}
    except (TypeError, ValueError):
        return {}


def _tool(annotations: Mapping[str, bool]):
    """Register an MCP tool with its annotations (recorded in TOOL_HINTS even without mcp) and its
    gated callable (REGISTERED). Only the tools the startup settings allow are offered to clients
    (policy.listed). The SDK gets a wrapper that turns any error into ToolError, so the client sees
    the reason (mcp 2.x reports any other exception as a bare 'Error executing tool'). The module
    keeps the plain function: direct calls and tests still get ValueError and friends. annotations: the
    four MCP hints (HINT_KEYS) as a dict literal on each tool, so static scanners read them from the
    source; openWorldHint marks the tools that reach the internet (the skill shop)."""
    hints = dict(annotations)

    def deco(tool_fn):
        name = tool_fn.__name__
        if tuple(hints) != HINT_KEYS or not all(isinstance(v, bool) for v in hints.values()):
            raise ValueError(f"{name}: annotations must give {', '.join(HINT_KEYS)} in this order, as booleans")
        if hints["readOnlyHint"] and hints["destructiveHint"]:
            raise ValueError(f"{name}: a read-only tool cannot be destructive")
        TOOL_HINTS[name] = hints

        @functools.wraps(tool_fn)
        def fn(*a, **k):  # closes the read-only connections the call opened (nested tool calls close their own)
            opened = _ro_cons.get()
            token = None
            if opened is None:
                opened = []
                token = _ro_cons.set(opened)
            mark = len(opened)
            try:
                return tool_fn(*a, **k)
            finally:
                for c in opened[mark:]:
                    try:
                        c.close()
                    except sqlite3.Error:
                        pass
                del opened[mark:]
                if token is not None:
                    _ro_cons.reset(token)
        gated = REGISTERED[name] = _gated(fn)
        if not _policy.listed(name, _state.STARTUP.tool_profile, _state.STARTUP.allow_writes, _shop_on(_state.STARTUP)):
            return fn
        LISTED.add(name)

        @functools.wraps(gated)
        def reg(*a, **k):
            try:
                return _wire(gated(*a, **k))
            except Exception as e:
                if ToolError is None or isinstance(e, ToolError):
                    raise
                msg = str(e) if isinstance(e, (ValueError, RuntimeError)) else f"{type(e).__name__}: {e}"
                raise ToolError(msg) from e
        kw = _sdk_tool_kw(mcp)
        if ToolAnnotations is None:
            mcp.tool(**kw)(reg)
        else:
            mcp.tool(annotations=ToolAnnotations.model_validate(hints), **kw)(reg)
        _slim_schema(mcp, name)
        return fn
    return deco


PROMPTS: dict[str, Callable[..., str]] = {}  # P6 skill -> its MCP prompt function (offered under the settings)


def _eppm_skill_ok(cmd) -> bool:
    """A skill works on a P6 EPPM working copy unless one of its tools is a write tool the copy refuses."""
    return not any(_policy.is_write(t) and t not in EPPM_EDIT_TOOLS for t in cmd.tools())


def _register_prompts() -> None:
    """Offer the P6 skills (p6mcp.skills.offered: the skills installed and turned on, from the shop or the
    user's own; the bundled core set until the first start has installed it) whose tools are all offered,
    as MCP prompts (reference files inline). On a P6 EPPM server, a skill that needs a write tool the working copy
    refuses (update-import-safely: p6_import_xer) is not offered."""
    for cmd in _skills.offered(_state.STARTUP.tool_profile, _state.STARTUP.allow_writes, _state.STARTUP.raw):
        if _state.STARTUP.eppm and not _eppm_skill_ok(cmd):
            continue
        def fn(_cmd=cmd, **values) -> str:
            return _skills.prompt_text(_cmd, values)
        P = inspect.Parameter
        fn.__signature__ = inspect.Signature(
            [P(a.name, P.KEYWORD_ONLY, default=P.empty if a.default is None else a.default, annotation=str)
             for a in cmd.args], return_annotation=str)
        fn.__annotations__ = {**{a.name: str for a in cmd.args}, "return": str}  # read by the SDK's pydantic
        fn.__name__ = cmd.name.replace("-", "_")
        fn.__doc__ = cmd.description
        PROMPTS[cmd.name] = fn
        if hasattr(mcp, "prompt"):
            kw = {"title": cmd.title} if "title" in inspect.signature(mcp.prompt).parameters else {}
            mcp.prompt(name=cmd.name, description=cmd.description, **kw)(fn)


HcCheck = Literal[tuple(list(get_args(DcmaCheck)) + ["key_dates"])]  # drill-down names: the DCMA 14 + information checks


DanglingKind = Literal["start", "finish", "both"]


RedundantCategory = Literal["completed_predecessor", "marked_deleted", "transitive", "duplicate"]


WhatIfOp = Literal["add_link", "delete_link", "update_link", "duration", "constraint", "add_activity"]
FragnetType = Literal["TT_Task", "TT_Mile", "TT_FinMile"]


class WhatIfEdit(TypedDict, total=False):
    """One what-if edit. op add_link / delete_link / update_link: predecessor_code, successor_code,
    rel_type, lag_days, current_type (or task_pred_id for delete / update); op duration: task_code and
    remaining_days, original_days (not started only) or add_remaining_days (days added to the current
    remaining duration, any status); op constraint: task_code, constraint (as p6_update_activity),
    constraint_date; op add_activity (fragnet, in memory only): task_code (a new Activity ID), task_name,
    original_days (in days of calendar, default the project calendar; 0 for a milestone), task_type
    (TT_Task default, TT_Mile, TT_FinMile), wbs (a WBS code, default the project node), constraint and
    constraint_date; link it with add_link edits after it."""
    op: WhatIfOp
    predecessor_code: str
    successor_code: str
    task_pred_id: int
    rel_type: RelType
    current_type: RelType
    lag_days: float
    task_code: str
    task_name: str
    task_type: FragnetType
    calendar: str
    wbs: str
    remaining_days: float
    original_days: float
    add_remaining_days: float
    constraint: CstrName
    constraint_date: str


WhatIfEdits = Annotated[list[WhatIfEdit], Field(description="Edits applied in memory, in order.")]
CompareTo = Literal["current", "engine"]
CutPct = Annotated[float, Field(ge=0, le=100, description="Flag duration cuts above this % of the original or "
                                                            "remaining duration.")]


class RiskPcts(TypedDict):
    """A triangular range in % of the remaining duration: 0 <= min_pct <= mode_pct <= max_pct (100 = unchanged)."""
    min_pct: float
    mode_pct: float
    max_pct: float


class RiskRange(RiskPcts, total=False):
    """A range for some activities, one selector: task_code (an Activity ID), wbs (a WBS code, full or relative
    to the project node: every activity below it) or code_type with code_value (activities with that activity
    code value or one of its child codes)."""
    task_code: str
    wbs: str
    code_type: str
    code_value: str


RiskRanges = Annotated[list[RiskRange], Field(description="Ranges by Activity ID, WBS or activity code value.")]
RiskIterations = Annotated[int, Field(description="Iterations: clamped to 100..5000, then to the network size cap.")]


# ----------------------------------------------------------------- update integrity: an update against the previous one

IntegrityFlag = Literal["open_end_to_finish", "negative_float_cleared_by_constraint", "must_finish_moved",
                        "actuals_changed", "duration_cut", "links_removed_on_critical", "rel_type_or_lag_changed",
                        "schedule_options_changed", "calendars_changed", "deleted_in_progress_or_critical", "data_date"]
MsOrder = Literal["date", "variance"]
MsCodes = Annotated[Union[list[str], str], Field(
    description="Activity IDs to list instead of every milestone: a list, or one text with the IDs separated by commas.")]


# ----------------------------------------------------------------- trend across schedule updates (p6_trend)

TrendMs = Literal["matched", "any", "none"]
TrendProjects = Annotated[list[ProjectId], Field(
    description="Project IDs of the updates of one job, in any order (the trend orders them by data date).")]


# ----------------------------------------------------------------- batch relationship tools

class _LinkEnds(TypedDict):
    predecessor_code: str
    successor_code: str


class LinkRef(_LinkEnds, total=False):
    """A relationship to delete: type needed when the two activities are linked by several types."""
    type: RelType
    predecessor_project: str


class LinkNew(_LinkEnds, total=False):
    """A relationship to add (rel_type default FS, lag_days default 0)."""
    rel_type: RelType
    lag_days: float
    predecessor_project: str


class LinkEdit(_LinkEnds, total=False):
    """A relationship to change: rel_type and/or lag_days; current_type picks it among several types."""
    rel_type: RelType
    lag_days: float
    current_type: RelType
    predecessor_project: str


LinkRefs = Annotated[list[LinkRef], Field(description="Links by Activity IDs.")]
LinkIds = Annotated[list[int], Field(description="task_pred_id values (p6_get_relationships, p6_redundant_links).")]
LinkNews = Annotated[list[LinkNew], Field(min_length=1, description="Links to add.")]
LinkEdits = Annotated[list[LinkEdit], Field(min_length=1, description="Links to change.")]


class _ActEditKeys(TypedDict):
    task_code: str


class ActEdit(_ActEditKeys, total=False):
    """An activity (task_code) and the fields to change, as p6_update_activity takes them."""
    task_name: str
    original_duration_days: float
    remaining_duration_days: float
    constraint: str
    constraint_date: str
    actual_start: str
    actual_finish: str
    physical_pct: float
    allow_after_data_date: bool


ActEdits = Annotated[list[ActEdit], Field(min_length=1, description="Activities to change.")]


# ----------------------------------------------------------------- new activities (p6_add_activities)

ActType = Literal["task", "resource_dependent", "start_milestone", "finish_milestone", "level_of_effort",
                  "wbs_summary"]


class _NewLinkKeys(TypedDict):
    task_code: str


class NewLink(_NewLinkKeys, total=False):
    """A relationship of a new activity: the other activity (of the project, of the same call, or of project=),
    type (FS default) and lag_days (0 default)."""
    type: RelType
    lag_days: float
    project: str


class _NewActKeys(TypedDict):
    task_code: str
    task_name: str


class NewActivity(_NewActKeys, total=False):
    """An activity to create (not started) and its relationships."""
    wbs: str
    type: ActType
    original_duration_days: float
    calendar: str
    constraint: CstrName
    constraint_date: str
    predecessors: list[NewLink]
    successors: list[NewLink]


NewActivities = Annotated[list[NewActivity], Field(min_length=1, description="Activities to create.")]


class AsgEdit(TypedDict, total=False):
    """An assignment (taskrsrc_id, or task_code with resource or role) and the units to set."""
    task_code: str
    resource: str
    role: str
    taskrsrc_id: int
    budgeted_units: float
    actual_units: float
    remaining_units: float
    at_completion_units: float


AsgEdits = Annotated[list[AsgEdit], Field(min_length=1, description="Assignments to change.")]


# ----------------------------------------------------------------- activity code values and WBS moves

class _CodeEditKeys(TypedDict):
    task_code: str
    code_type: str


class CodeEdit(_CodeEditKeys, total=False):
    """An activity code value to assign (code_value) or remove (clear=True) for one code type."""
    code_value: str
    clear: bool


CodeEdits = Annotated[list[CodeEdit], Field(min_length=1, description="Activity code values to assign or clear.")]


class WbsMove(TypedDict):
    """An activity and the WBS it moves to (WBS code as p6_list_wbs shows it, or a WBS name unique in the project)."""
    task_code: str
    wbs: str


WbsMoves = Annotated[list[WbsMove], Field(min_length=1, description="Activities to move to another WBS.")]


# ----------------------------------------------------------------- activity UDF values

class _UdfEditKeys(TypedDict):
    task_code: str
    udf: str


class UdfEdit(_UdfEditKeys, total=False):
    """An activity UDF value to set (value) or remove (clear=True)."""
    value: Union[str, float]
    clear: bool


UdfEdits = Annotated[list[UdfEdit], Field(min_length=1, description="Activity UDF values to set or clear.")]
