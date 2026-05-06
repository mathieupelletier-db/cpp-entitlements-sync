"""Parse a CloudTrail Lake Formation event record into an LFEvent.

Handles the common shapes for: GrantPermissions, RevokePermissions, AddLFTagsToResource,
RemoveLFTagsFromResource, CreateLFTag, UpdateLFTag, DeleteLFTag. Raises on unknown event names.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .models import (
    LFEvent,
    LFEventKind,
    LFTagAssignment,
    Principal,
    PrincipalKind,
    ResourceRef,
)

GRANT_KINDS = {"GrantPermissions", "RevokePermissions"}
TAG_RESOURCE_KINDS = {"AddLFTagsToResource", "RemoveLFTagsFromResource"}
TAG_LIFECYCLE_KINDS = {"CreateLFTag", "UpdateLFTag", "DeleteLFTag"}


def parse_event(rec: dict) -> LFEvent:
    name = rec["eventName"]
    if name in GRANT_KINDS:
        return _parse_grant(rec, name)
    if name in TAG_RESOURCE_KINDS:
        return _parse_tag_resource(rec, name)
    if name in TAG_LIFECYCLE_KINDS:
        return _parse_tag_lifecycle(rec, name)
    raise ValueError(f"Unknown LF event name: {name}")


def _parse_grant(rec: dict, name: str) -> LFEvent:
    p = rec["requestParameters"]
    return LFEvent(
        kind=LFEventKind(name),
        event_id=rec["eventID"],
        event_time=_ts(rec["eventTime"]),
        resource=_parse_resource(p.get("resource", {})),
        principal=_parse_principal(p.get("principal", {})),
        permissions=tuple(p.get("permissions", [])),
        lf_tags=None,
    )


def _parse_tag_resource(rec: dict, name: str) -> LFEvent:
    p = rec["requestParameters"]
    raw_tags = p.get("lFTags", [])
    flat: list[LFTagAssignment] = []
    for t in raw_tags:
        for v in t.get("tagValues", []):
            flat.append(LFTagAssignment(key=t["tagKey"], value=v))
    return LFEvent(
        kind=LFEventKind(name),
        event_id=rec["eventID"],
        event_time=_ts(rec["eventTime"]),
        resource=_parse_resource(p.get("resource", {})),
        principal=None,
        permissions=(),
        lf_tags=tuple(flat),
    )


def _parse_tag_lifecycle(rec: dict, name: str) -> LFEvent:
    p = rec["requestParameters"]
    key = p["tagKey"]
    return LFEvent(
        kind=LFEventKind(name),
        event_id=rec["eventID"],
        event_time=_ts(rec["eventTime"]),
        resource=None,
        principal=None,
        permissions=(),
        lf_tags=(LFTagAssignment(key=key, value=""),),
    )


def _parse_resource(raw: dict) -> Optional[ResourceRef]:
    if "table" in raw:
        t = raw["table"]
        return ResourceRef(
            catalog=t.get("catalogId", "default"),
            database=t["databaseName"],
            table=t["name"],
            column=None,
        )
    if "tableWithColumns" in raw:
        t = raw["tableWithColumns"]
        cols = t.get("columnNames", [None])
        return ResourceRef(
            catalog=t.get("catalogId", "default"),
            database=t["databaseName"],
            table=t["name"],
            column=cols[0] if cols else None,
        )
    if "database" in raw:
        d = raw["database"]
        return ResourceRef(
            catalog=d.get("catalogId", "default"),
            database=d["name"],
            table=None,
            column=None,
        )
    return None


def _parse_principal(raw: dict) -> Optional[Principal]:
    ident = raw.get("dataLakePrincipalIdentifier")
    if ident is None:
        return None
    if ident.startswith("arn:aws:iam::"):
        return Principal(kind=PrincipalKind.IAM_ROLE, identifier=ident)
    if "@" in ident:
        return Principal(kind=PrincipalKind.IDP_USER, identifier=ident)
    return Principal(kind=PrincipalKind.IDP_GROUP, identifier=ident)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
