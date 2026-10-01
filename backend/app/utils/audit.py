import logging
from typing import Any, Optional

from app.database.supabase import supabase

logger = logging.getLogger(__name__)


def write_audit(
    actor: dict,
    action: str,
    entity: str,
    entity_id: str,
    before: Optional[dict] = None,
    after: Optional[dict] = None,
    reason: Optional[str] = None,
) -> Any:
    """Insert one audit_log row for an admin/reviewer action.

    Only for actions the DB triggers do NOT already audit (review decisions and
    role changes are audited by triggers in 002_rbac_queue_notifications.sql).
    """
    row = {
        "actor_id": actor["id"],
        "actor_role": actor["role"],
        "action": action,
        "entity": entity,
        "entity_id": str(entity_id),
        "before": before,
        "after": after,
        "reason": reason,
    }
    try:
        res = supabase.table("audit_log").insert(row).execute()
        return res.data[0] if res.data else row
    except Exception as e:
        logger.error("Failed to write audit log for action %s on %s/%s: %s", action, entity, entity_id, e)
        return row
