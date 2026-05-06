"""Translate LF-Tag add/remove events into UC SyncOps."""
from __future__ import annotations

from ..models import LFEvent, LFEventKind, SyncOp, SyncOpKind

MANAGED_BY_KEY = "managed_by"
MANAGED_BY_VALUE = "lf_sync"


class TagTranslator:
    """Map LF-Tag keys via a namespace map. Unknown keys pass through unchanged."""

    def __init__(self, *, namespace_map: dict[str, str]) -> None:
        self._namespace_map = namespace_map

    def translate(self, ev: LFEvent) -> list[SyncOp]:
        if ev.kind is LFEventKind.ADD_LFTAGS_TO_RESOURCE:
            return self._set_ops(ev)
        if ev.kind is LFEventKind.REMOVE_LFTAGS_FROM_RESOURCE:
            return self._unset_ops(ev)
        return []

    def _uc_key(self, lf_key: str) -> str:
        return self._namespace_map.get(lf_key, lf_key)

    def _set_ops(self, ev: LFEvent) -> list[SyncOp]:
        assert ev.resource is not None and ev.lf_tags is not None
        ops: list[SyncOp] = [
            SyncOp(
                kind=SyncOpKind.SET_TAG,
                resource=ev.resource,
                principal=None,
                permissions=(),
                tag_key=self._uc_key(t.key),
                tag_value=t.value,
                policy_name=None,
            )
            for t in ev.lf_tags
        ]
        # always assert the managed-by marker on first touch; idempotent in the UC client
        ops.append(
            SyncOp(
                kind=SyncOpKind.SET_TAG,
                resource=ev.resource,
                principal=None,
                permissions=(),
                tag_key=MANAGED_BY_KEY,
                tag_value=MANAGED_BY_VALUE,
                policy_name=None,
            )
        )
        return ops

    def _unset_ops(self, ev: LFEvent) -> list[SyncOp]:
        assert ev.resource is not None and ev.lf_tags is not None
        return [
            SyncOp(
                kind=SyncOpKind.UNSET_TAG,
                resource=ev.resource,
                principal=None,
                permissions=(),
                tag_key=self._uc_key(t.key),
                tag_value=None,
                policy_name=None,
            )
            for t in ev.lf_tags
        ]
