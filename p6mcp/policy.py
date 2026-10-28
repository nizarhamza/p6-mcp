"""Tool classification and the rules that gate each call (enforced by the server, not the UI).

kind:
  read         reads the database only (read-only connection)
  file         reads the database and writes a file the caller names (export_xer, export_dashboard): Full profile
  write        changes the database: Full profile + allow_writes + dry run + confirmation token
  destructive  write that deletes or replaces data: also needs the exact confirmation text
  config       changes P6 MCP's own settings, never the database (p6_shop_install): Full profile + dry run +
               confirmation token, not gated by allow_writes

The skill shop tools (SHOP_TOOLS) read github.com: they are listed only when shop_enabled is on.

The Basic profile lists only BASIC_TOOLS (all read tools). The tool list is fixed when the server
starts (clients cache it); the rules are checked again on every call, so turning writes off takes
effect at once, while turning a tool on needs a restart of the server (reported by p6_diagnose).
"""

from __future__ import annotations

from typing import Optional

READ, FILE, WRITE, DESTRUCTIVE, CONFIG = "read", "file", "write", "destructive", "config"
KINDS = (READ, FILE, WRITE, DESTRUCTIVE, CONFIG)

TOOL_KIND: dict[str, str] = {
    # read
    "p6_list_projects": READ, "p6_list_eps": READ, "p6_list_wbs": READ, "p6_list_calendars": READ,
    "p6_project_summary": READ, "p6_query_activities": READ, "p6_get_activity": READ,
    "p6_get_relationships": READ, "p6_critical_path": READ, "p6_driving_path": READ,
    "p6_float_analysis": READ, "p6_float_paths": READ, "p6_float_origin": READ, "p6_lookahead": READ,
    "p6_milestones": READ,
    "p6_progress_check": READ, "p6_progress_curve": READ, "p6_resource_summary": READ,
    "p6_group_summary": READ, "p6_health_check": READ, "p6_health_check_compare": READ,
    "p6_compare_projects": READ, "p6_cpm_validate": READ, "p6_dangling": READ, "p6_redundant_links": READ,
    "p6_schedule_options": READ, "p6_schedule_whatif": READ, "p6_inspect_xer": READ,
    "p6_list_backups": READ, "p6_edit_log": READ, "p6_diagnose": READ, "p6_sql_select": READ,
    "p6_list_udfs": READ, "p6_shop_search": READ, "p6_shop_get": READ,
    # file output
    "p6_export_xer": FILE, "p6_export_dashboard": FILE,
    # write
    "p6_update_activity": WRITE, "p6_update_assignment": WRITE, "p6_update_assignments": WRITE,
    "p6_update_udf": WRITE, "p6_update_udfs": WRITE,
    "p6_add_relationship": WRITE, "p6_add_relationships": WRITE,
    "p6_update_relationship": WRITE, "p6_update_relationships": WRITE, "p6_delete_relationship": WRITE,
    "p6_delete_relationships": WRITE, "p6_create_eps": WRITE, "p6_import_xer": WRITE,
    "p6_set_schedule_options": WRITE, "p6_backup_database": WRITE,
    # destructive
    "p6_delete_activity": DESTRUCTIVE, "p6_delete_project": DESTRUCTIVE, "p6_restore_backup": DESTRUCTIVE,
    # P6 MCP settings
    "p6_shop_install": CONFIG,
}

# Basic profile: a small read-only set that small local models handle well
BASIC_TOOLS = frozenset({
    "p6_diagnose", "p6_list_projects", "p6_list_wbs", "p6_project_summary", "p6_query_activities",
    "p6_get_activity", "p6_get_relationships", "p6_critical_path", "p6_float_analysis", "p6_lookahead",
    "p6_health_check",
})
# Tools that do not need a database (the rest refuse with the "No database configured" message)
NO_DB_TOOLS = frozenset({"p6_diagnose", "p6_inspect_xer", "p6_shop_search", "p6_shop_get", "p6_shop_install"})
# The skill shop (p6mcp.shop): GitHub reads, offered only while the setting shop_enabled is on
SHOP_TOOLS = frozenset({"p6_shop_search", "p6_shop_get", "p6_shop_install"})
SHOP_OFF = ("The skill shop is turned off. Turn on 'Allow the skill shop' in P6 MCP Settings (Tools and safety), "
            "then restart your MCP client.")

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


WRITES_OFF = ("Write operations are disabled. Turn on 'Allow write operations' in P6 MCP Settings "
              "(Tools and safety) to use this tool.")


def write_refusal(profile: str, allow_writes: bool) -> Optional[str]:
    """Why no database write may run under these settings (None = writes allowed)."""
    if profile != "full":
        return ("Writes need the Full tool profile. Switch the tool profile to Full in P6 MCP Settings, "
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
                "P6 MCP Settings, then restart your MCP client.")
    if k in (WRITE, DESTRUCTIVE):
        return write_refusal(profile, allow_writes)
    return None
