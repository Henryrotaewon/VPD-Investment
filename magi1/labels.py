"""Point-in-time wallet/address labels for on-chain flow analysis.

Labels are evidence, not truth.  A label is usable for a historical event only
when its ``known_at_ms`` is not after the event.  This prevents a later label
discovery from leaking into an earlier backtest.  Conflicting active labels are
returned as CONFLICT and never silently resolved by priority.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class AddressLabel:
    chain: str
    address: str
    entity: str
    role: str
    evidence_grade: str
    source: str
    known_at_ms: int
    valid_from_ms: int = 0
    valid_to_ms: Optional[int] = None
    status: str = "ACTIVE"
    notes: str = ""

    def key(self) -> str:
        return f"{self.chain.lower()}|{self.address.lower()}"


class AddressLabelRegistry:
    """Small auditable registry suitable for a local JSON file.

    ``known_at_ms`` is the information-availability time, while valid_from/to
    describe when the label is believed to apply on-chain.  Both constraints
    must hold for an as-of lookup.
    """

    VALID_GRADES = {"A_OFFICIAL", "B_INDEPENDENT", "C_INFERRED", "D_UNKNOWN"}

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.rows: list[AddressLabel] = []
        self.version = "empty"
        self._mtime_ns: Optional[int] = None
        if self.path and self.path.exists():
            self.load()

    @staticmethod
    def _normalise(row: AddressLabel | dict[str, Any]) -> AddressLabel:
        if isinstance(row, AddressLabel):
            value = row
        else:
            value = AddressLabel(**row)
        if not value.chain or not value.address or not value.entity or not value.role:
            raise ValueError("chain, address, entity and role are required")
        if value.evidence_grade not in AddressLabelRegistry.VALID_GRADES:
            raise ValueError(f"unsupported evidence_grade: {value.evidence_grade}")
        if value.valid_to_ms is not None and value.valid_to_ms < value.valid_from_ms:
            raise ValueError("valid_to_ms must not precede valid_from_ms")
        if value.known_at_ms < 0:
            raise ValueError("known_at_ms must be non-negative")
        return value

    def add(self, row: AddressLabel | dict[str, Any], persist: bool = True) -> AddressLabel:
        value = self._normalise(row)
        self.rows.append(value)
        self.rows.sort(key=lambda x: (x.key(), x.known_at_ms, x.valid_from_ms, x.source))
        self._refresh_version()
        if persist:
            self.save()
        return value

    def load(self) -> None:
        if not self.path or not self.path.exists():
            self.rows = []
            self._refresh_version()
            return
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            rows.append(self._normalise(json.loads(line)))
        self.rows = rows
        self._mtime_ns = self.path.stat().st_mtime_ns
        self._refresh_version()

    def reload_if_changed(self) -> bool:
        if not self.path or not self.path.exists():
            return False
        mtime = self.path.stat().st_mtime_ns
        if self._mtime_ns != mtime:
            self.load()
            return True
        return False

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(json.dumps(asdict(x), ensure_ascii=False, sort_keys=True) + "\n" for x in self.rows)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(self.path)
        self._mtime_ns = self.path.stat().st_mtime_ns

    def _refresh_version(self) -> None:
        payload = [asdict(x) for x in self.rows]
        self.version = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]

    def resolve(self, chain: str, address: str, asof_ms: int, current_ms: Optional[int] = None) -> dict[str, Any]:
        """Resolve one address without future-label leakage.

        ``current_ms`` is intentionally optional and only included in the
        returned audit fields.  The lookup itself always uses ``asof_ms``.
        """
        key = f"{chain.lower()}|{address.lower()}"
        candidates = [
            x for x in self.rows
            if x.key() == key and x.status == "ACTIVE"
            and x.known_at_ms <= asof_ms
            and x.valid_from_ms <= asof_ms
            and (x.valid_to_ms is None or asof_ms < x.valid_to_ms)
        ]
        entities = sorted({x.entity for x in candidates})
        if len(entities) == 1:
            status = "RESOLVED"
            entity = entities[0]
        elif len(entities) > 1:
            status = "CONFLICT"
            entity = None
        else:
            status = "UNKNOWN"
            entity = None
        return {
            "chain": chain,
            "address": address,
            "entity": entity,
            "status": status,
            "roles": sorted({x.role for x in candidates}),
            "evidence_grades": sorted({x.evidence_grade for x in candidates}),
            "sources": sorted({x.source for x in candidates}),
            "registry_version": self.version,
            "asof_ms": asof_ms,
            "current_ms": current_ms,
        }

    def annotate_transaction(self, transaction: dict[str, Any], event_ts_ms: int, current_ms: Optional[int] = None) -> dict[str, Any]:
        """Attach input/output labels while retaining all raw transaction data."""
        out = dict(transaction)
        out["label_lookup_basis"] = "event_asof"
        out["label_registry_version"] = self.version
        out["input_labels"] = [self.resolve("BTC", x["address"], event_ts_ms, current_ms) for x in transaction.get("inputs", []) if x.get("address")]
        out["output_labels"] = [self.resolve("BTC", x["address"], event_ts_ms, current_ms) for x in transaction.get("outputs", []) if x.get("address")]
        return out


def reclassify_onchain(row: dict[str, Any], registry: AddressLabelRegistry, asof_mode: str = "event", now_ms: Optional[int] = None) -> dict[str, Any]:
    """Reclassify a stored event without changing its original raw record.

    ``event`` is the leakage-safe historical view.  ``current`` is an explicit
    investigative view and must never be pooled with a historical backtest.
    """
    if asof_mode not in {"event", "current"}:
        raise ValueError("asof_mode must be 'event' or 'current'")
    asof = int(row.get("event_ts_ms", 0)) if asof_mode == "event" else int(now_ms or 0)
    tx = row.get("metadata", {}).get("transaction", row.get("transaction", {}))
    result = dict(row)
    result["metadata"] = dict(row.get("metadata", {}))
    result["metadata"]["transaction"] = registry.annotate_transaction(tx, asof, now_ms)
    result["classification_basis"] = "known_at_event" if asof_mode == "event" else "known_at_reclassification"
    result["classified_at_ms"] = now_ms
    result["label_registry_version"] = registry.version
    return result
