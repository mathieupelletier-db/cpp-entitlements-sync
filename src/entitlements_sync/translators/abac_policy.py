"""Translate LF-Tag lifecycle events into UC policy upsert/delete ops."""
from __future__ import annotations

from ..models import LFEvent, LFEventKind, SyncOp, SyncOpKind

POLICY_NAME_PREFIX = "lf_sync__"


class ABACPolicyTranslator:
    """Maintain one UC ABAC policy per LF-Tag key. The policy body is rendered by the UC adapter
    (Plan 2) from a small template; here we only emit the upsert/delete intent + policy name."""

    def __init__(self, *, namespace_map: dict[str, str]) -> None:
        self._namespace_map = namespace_map

    def translate(self, ev: LFEvent) -> list[SyncOp]:
        if ev.kind is LFEventKind.CREATE_LF_TAG or ev.kind is LFEventKind.UPDATE_LF_TAG:
            return [self._policy_op(SyncOpKind.UPSERT_POLICY, ev)]
        if ev.kind is LFEventKind.DELETE_LF_TAG:
            return [self._policy_op(SyncOpKind.DELETE_POLICY, ev)]
        return []

    def _policy_op(self, kind: SyncOpKind, ev: LFEvent) -> SyncOp:
        assert ev.lf_tags is not None and len(ev.lf_tags) >= 1
        lf_key = ev.lf_tags[0].key
        uc_key = self._namespace_map.get(lf_key, lf_key)
        return SyncOp(
            kind=kind,
            resource=None,
            principal=None,
            permissions=(),
            tag_key=None,
            tag_value=None,
            policy_name=f"{POLICY_NAME_PREFIX}{uc_key}",
        )
