"""Tool classification and the rules that gate each call (enforced by the server, not the UI).

kind:
  read         reads the database only (read-only connection)
  file         reads the database and writes a file the caller names (export_xer, export_dashboard): Full profile
  write        changes the database: Full profile + allow_writes + dry run + confirmation token
  destructive  write that deletes or replaces data: also needs the exact confirmation text
  config       changes the app's own settings, never the database (p6_shop_install): Full profile + dry run +
               confirmation token, not gated by allow_writes

The skill shop tools (SHOP_TOOLS) read github.com: they are listed only when shop_enabled is on.

The Basic profile lists only BASIC_TOOLS (all read tools). The tool list is fixed when the server
starts (clients cache it); the rules are checked again on every call, so turning writes off takes
effect at once, while turning a tool on needs a restart of the server (reported by p6_diagnose).
"""

from __future__ import annotations

from typing import Iterable, Optional

READ, FILE, WRITE, DESTRUCTIVE, CONFIG = "read", "file", "write", "destructive", "config"
KINDS = (READ, FILE, WRITE, DESTRUCTIVE, CONFIG)

TOOL_KIND: dict[str, str] = {
    # read
    "p6_list_projects": READ, "p6_list_eps": READ, "p6_list_wbs": READ, "p6_list_calendars": READ,
    "p6_project_summary": READ, "p6_query_activities": READ, "p6_get_activity": READ,
    "p6_get_relationships": READ, "p6_critical_path": READ, "p6_driving_path": READ,
    "p6_float_analysis": READ, "p6_float_paths": READ, "p6_float_origin": READ, "p6_lookahead": READ,
    "p6_milestones": READ, "p6_schedule_risk": READ, "p6_update_integrity": READ, "p6_trend": READ,
    "p6_progress_check": READ, "p6_progress_curve": READ, "p6_resource_summary": READ, "p6_resource_loading": READ,
    "p6_group_summary": READ, "p6_health_check": READ, "p6_health_check_compare": READ,
    "p6_compare_projects": READ, "p6_cpm_validate": READ, "p6_dangling": READ, "p6_redundant_links": READ,
    "p6_schedule_options": READ, "p6_schedule_whatif": READ, "p6_inspect_xer": READ,
    "p6_list_backups": READ, "p6_edit_log": READ, "p6_diagnose": READ, "p6_sql_select": READ,
    "p6_list_udfs": READ, "p6_shop_search": READ, "p6_shop_get": READ,
    "p6_eppm_projects": READ, "p6_eppm_status": READ, "p6_calendar_check": READ,
    # file output (p6_eppm_pull: the local working copy, the server is only read)
    "p6_export_xer": FILE, "p6_export_dashboard": FILE, "p6_export_lookahead": FILE, "p6_eppm_pull": FILE,
    # write
    "p6_update_activity": WRITE, "p6_update_activities": WRITE, "p6_update_assignment": WRITE,
    "p6_update_assignments": WRITE,
    "p6_update_udf": WRITE, "p6_update_udfs": WRITE,
    "p6_add_relationship": WRITE, "p6_add_relationships": WRITE,
    "p6_update_relationship": WRITE, "p6_update_relationships": WRITE, "p6_delete_relationship": WRITE,
    "p6_delete_relationships": WRITE, "p6_create_eps": WRITE, "p6_import_xer": WRITE,
    "p6_set_schedule_options": WRITE, "p6_backup_database": WRITE,
    "p6_eppm_schedule": WRITE,  # schedules on the P6 EPPM server (two-step flow), then pulls
    "p6_eppm_discard": WRITE,  # drops pending changes of the local working copy (the server is not touched)
    # destructive
    "p6_delete_activity": DESTRUCTIVE, "p6_delete_project": DESTRUCTIVE, "p6_restore_backup": DESTRUCTIVE,
    "p6_eppm_push": DESTRUCTIVE,  # writes to the P6 EPPM server
    # MCP for Primavera P6 settings
    "p6_shop_install": CONFIG,
}

# Basic profile: a small read-only set that small local models handle well
BASIC_TOOLS = frozenset({
    "p6_diagnose", "p6_list_projects", "p6_list_wbs", "p6_project_summary", "p6_query_activities",
    "p6_get_activity", "p6_get_relationships", "p6_critical_path", "p6_float_analysis", "p6_lookahead",
    "p6_health_check",
})
# Tools that do not need a database (the rest refuse with the "No database configured" message)
NO_DB_TOOLS = frozenset({"p6_diagnose", "p6_inspect_xer", "p6_shop_search", "p6_shop_get", "p6_shop_install",
                         "p6_eppm_projects", "p6_eppm_pull", "p6_eppm_status"})
# The skill shop (p6mcp.shop): GitHub reads, offered only while the setting shop_enabled is on
SHOP_TOOLS = frozenset({"p6_shop_search", "p6_shop_get", "p6_shop_install"})
SHOP_OFF = ("The skill shop is turned off. Turn on 'Allow the skill shop' in MCP for Primavera P6 Settings "
            "(Tools and safety), then restart your MCP client.")

assert BASIC_TOOLS <= {t for t, k in TOOL_KIND.items() if k == READ}
assert set(TOOL_KIND.values()) <= set(KINDS)


def kind(tool: str) -> str:
    try:
        return TOOL_KIND[tool]
    except KeyError:
        raise KeyError(f"{tool} has no classification in p6mcp.policy.TOOL_KIND") from None


def is_write(tool: str) -> bool:
    return kind(tool) in (WRITE, DESTRUCTIVE)


def listed(tool: str, profile: str, allow_writes: bool, shop: bool = True) -> bool:
    """Is the tool offered to clients when the server starts with these settings?"""
    k = kind(tool)
    if tool in SHOP_TOOLS and not shop:
        return False
    if profile != "full":
        return tool in BASIC_TOOLS
    if k in (WRITE, DESTRUCTIVE):
        return allow_writes
    return True


WRITES_OFF = ("Write operations are disabled. Turn on 'Allow write operations' in MCP for Primavera P6 Settings "
              "(Tools and safety) to use this tool.")


def write_refusal(profile: str, allow_writes: bool) -> Optional[str]:
    """Why no database write may run under these settings (None = writes allowed)."""
    if profile != "full":
        return ("Writes need the Full tool profile. Switch the tool profile to Full in MCP for Primavera P6 Settings, "
                "then restart your MCP client.")
    if not allow_writes:
        return WRITES_OFF
    return None


def refusal(tool: str, profile: str, allow_writes: bool, shop: bool = True) -> Optional[str]:
    """Why this call is refused under the current settings (None = allowed)."""
    k = kind(tool)
    if tool in SHOP_TOOLS and not shop:
        return SHOP_OFF
    if profile != "full" and tool not in BASIC_TOOLS:
        return (f"{tool} is not available in the Basic tool profile. Switch the tool profile to Full in "
                "MCP for Primavera P6 Settings, then restart your MCP client.")
    if k in (WRITE, DESTRUCTIVE):
        return write_refusal(profile, allow_writes)
    return None


# ----------------------------------------------------------------- remote clients (claude.ai, ChatGPT)
# A remote client reaches the server through the remote access server (p6mcp.remote), approved on this PC with
# OAuth. It is exposed to prompt injection from whatever it reads, so it gets less than a local client:
# - tools that take or write a path on this PC are never offered (the remote user has no access to that folder);
# - destructive and config tools and the skill shop are never offered;
# - writes, and p6_eppm_pull (it rewrites the local working copy), need the remote switch remote_allow_writes
#   on top of the local rules, and a grant approved with edits (scope p6:write).
REMOTE_READ_SCOPE, REMOTE_WRITE_SCOPE = "p6:read", "p6:write"
LOCAL_PATH_TOOLS = frozenset({"p6_import_xer", "p6_inspect_xer", "p6_export_xer", "p6_export_dashboard",
                              "p6_export_lookahead"})
REMOTE_NEVER = LOCAL_PATH_TOOLS | SHOP_TOOLS | frozenset(t for t, k in TOOL_KIND.items() if k in (DESTRUCTIVE, CONFIG))
REMOTE_AS_WRITE = frozenset({"p6_eppm_pull"})
REMOTE_NO_PATH_ARGS = {"p6_schedule_options": ("xer_path",)}  # listed remotely, but these arguments are refused
REMOTE_OFF = ("{tool} is not available to remote clients (claude.ai, ChatGPT): it works with files or settings on "
              "the PC, or deletes data. Use it from a local client on the PC.")
REMOTE_PATH_ARG = ("{arg} is not available to remote clients: it names a file on the PC. Use this tool from a local "
                   "client on the PC for that.")
REMOTE_NO_GRANT = "This connection has no access to P6 data. Connect again and approve it on the PC."
REMOTE_WRITES_OFF = ("Edits from remote clients are off. Turn on 'Allow edits from remote clients' in MCP for "
                     "Primavera P6 Settings (Remote access) on the PC.")
REMOTE_SCOPE_OFF = ("This connection was approved read only. To edit, remove the connector in your AI app, connect "
                    "again and approve it on the PC with 'Allow edits' ticked.")


def remote_kind(tool: str) -> str:
    """'never', 'write' or 'read' for a remote client."""
    if tool in REMOTE_NEVER:
        return "never"
    if is_write(tool) or tool in REMOTE_AS_WRITE:
        return "write"
    return "read"


def remote_listed(tool: str, profile: str, allow_writes: bool, remote_writes: bool, shop: bool = True) -> bool:
    """Is the tool offered to remote clients when the remote access server starts with these settings?"""
    rk = remote_kind(tool)
    if rk == "never" or (rk == "write" and not remote_writes):
        return False
    return listed(tool, profile, allow_writes, shop)


def remote_refusal(tool: str, profile: str, allow_writes: bool, remote_writes: bool,
                   scopes: Optional[Iterable[str]], shop: bool = True, args: Optional[dict] = None) -> Optional[str]:
    """Why a remote client's call is refused (None = allowed); the local rules apply on top. scopes: the scopes
    of the OAuth grant of the call (None or empty: no grant, refused). args: the call's arguments."""
    rk = remote_kind(tool)
    if rk == "never":
        return REMOTE_OFF.format(tool=tool)
    scopes = set(scopes or ())
    if REMOTE_READ_SCOPE not in scopes:
        return REMOTE_NO_GRANT
    for arg in REMOTE_NO_PATH_ARGS.get(tool, ()):
        if (args or {}).get(arg) not in (None, ""):
            return REMOTE_PATH_ARG.format(arg=arg)
    if rk == "write":
        if not remote_writes:
            return REMOTE_WRITES_OFF
        if REMOTE_WRITE_SCOPE not in scopes:
            return REMOTE_SCOPE_OFF
    return refusal(tool, profile, allow_writes, shop)
