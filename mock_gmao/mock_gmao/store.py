"""In-memory tenant state with JSON persistence.

One ``TenantStore`` per tenant. The seed lives in ``data/<tenant>.json`` and is
never modified; the live state is persisted to ``<state_dir>/<tenant>.json`` after
every write so that restarting the server keeps what the candidate pushed.
"""

from __future__ import annotations

import copy
import json
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


class ApiError(Exception):
    """Business error surfaced as an HTTP status + list of messages (like MX)."""

    def __init__(self, status_code: int, *messages: str):
        super().__init__(messages[0] if messages else "")
        self.status_code = status_code
        self.messages = list(messages)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(value: str) -> datetime:
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class TenantStore:
    def __init__(self, tenant: str, seed_path: Path, state_dir: Path):
        self.tenant = tenant
        self.seed_path = seed_path
        self.state_path = state_dir / f"{tenant}.json"
        self._lock = threading.RLock()
        self.calls: Counter = Counter()
        self.chaos_rate_override: float | None = None
        self.rate_limit_override: int | None = None
        self.data: dict = {}
        self.load()

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self.state_path.exists():
                self.data = json.loads(self.state_path.read_text())
            else:
                self.data = json.loads(self.seed_path.read_text())
                self._persist()

    def reset(self) -> None:
        with self._lock:
            self.data = json.loads(self.seed_path.read_text())
            self.calls = Counter()
            self.chaos_rate_override = None
            self.rate_limit_override = None
            self._persist()

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        tmp.replace(self.state_path)

    # -- accessors ---------------------------------------------------------
    @property
    def api_key(self) -> str:
        return self.data["apiKey"]

    def _assets(self) -> list[dict]:
        return self.data["assets"]

    def _asset(self, code: str) -> dict | None:
        for a in self._assets():
            if a["code"] == code:
                return a
        return None

    def _family(self, code: str | None) -> dict | None:
        if code is None:
            return None
        for f in self.data["families"]:
            if f["code"] == code:
                return f
        return None

    def _body(self, name: str) -> dict | None:
        for b in self.data["bodies"]:
            if b["bodyName"] == name:
                return b
        return None

    def _children(self, code: str) -> list[dict]:
        return [a for a in self._assets() if a.get("parentCode") == code]

    # -- read endpoints ----------------------------------------------------
    def filter_assets(self, flt: dict) -> list[dict]:
        page = max(int(flt.get("currentPage") or 1), 1)
        size = int(flt.get("pageSize") or 1000)
        size = min(max(size, 1), 1000)  # silently clamped, like the real API
        rows = self._assets()
        if flt.get("archived") is not None:
            rows = [a for a in rows if a["archived"] == bool(flt["archived"])]
        if flt.get("assetCode"):
            rows = [a for a in rows if a["code"] == flt["assetCode"]]
        if flt.get("parentCode"):
            rows = [a for a in rows if a.get("parentCode") == flt["parentCode"]]
        if flt.get("assetFamilyCode"):
            rows = [a for a in rows if a.get("familyCode") == flt["assetFamilyCode"]]
        if flt.get("bodyNames"):
            wanted = set(flt["bodyNames"])
            rows = [a for a in rows if wanted & set(a.get("bodies") or [])]
        if flt.get("startLastModificationDateTime"):
            start = parse_dt(flt["startLastModificationDateTime"])
            rows = [a for a in rows if parse_dt(a["lastModificationDateTime"]) >= start]
        if flt.get("endLastModificationDateTime"):
            end = parse_dt(flt["endLastModificationDateTime"])
            rows = [a for a in rows if parse_dt(a["lastModificationDateTime"]) <= end]
        rows = sorted(rows, key=lambda a: a["code"])
        chunk = rows[(page - 1) * size : page * size]
        return [self._filter_view(a) for a in chunk]

    def _filter_view(self, a: dict) -> dict:
        """AssetConnectorFilterResponseModel: note that ``archived`` is NOT exposed."""
        parent = self._asset(a["parentCode"]) if a.get("parentCode") else None
        fam = self._family(a.get("familyCode"))
        return {
            "code": a["code"],
            "name": a["name"],
            "interfaceKey": a.get("interfaceKey"),
            "parent": {"code": parent["code"], "name": parent["name"]} if parent else None,
            "family": {"code": fam["code"], "name": fam["name"]} if fam else None,
            "bodies": [{"name": b} for b in a.get("bodies") or []],
            "criticality": a.get("criticality"),
            "state": a.get("state"),
            "node": a.get("node", False),
            "sharedData": a.get("sharedData", False),
            "inServiceDate": a.get("inServiceDate"),
            "assetTimeZone": {"id": "Europe/Paris"},
            "accountAssignment": None,
            "brand": None,
            "model": None,
            "manufacturer": None,
            "manufacturerReference": None,
            "serialNumber": None,
            "fixedAssetNumber": None,
            "location": None,
            "subcontractor": None,
            "supplier": None,
        }

    def get_asset(self, code: str | None, interface_key: str | None) -> dict:
        a = None
        if code:
            a = self._asset(code)
        elif interface_key:
            a = next((x for x in self._assets() if x.get("interfaceKey") == interface_key), None)
        else:
            raise ApiError(406, "Either 'code' or 'interfaceKey' must be provided")
        if not a:
            raise ApiError(404, f"Asset not found")
        parent = self._asset(a["parentCode"]) if a.get("parentCode") else None
        fam = self._family(a.get("familyCode"))
        return {
            "asset": {"code": a["code"], "name": a["name"]},
            "assetFamily": {"code": fam["code"], "name": fam["name"]} if fam else None,
            "archived": a["archived"],
            "parent": {"code": parent["code"], "name": parent["name"]} if parent else None,
            "bodies": [{"name": b} for b in a.get("bodies") or []],
            "criticality": a.get("criticality"),
            "state": a.get("state"),
            "node": a.get("node", False),
            "inServiceDate": a.get("inServiceDate"),
            "interfaceKey": a.get("interfaceKey"),
            "assetTimeZoneId": "Europe/Paris",
            "comment": None,
            "meters": [
                {
                    "name": m["name"],
                    "unit": {"code": m["unitCode"], "name": m["unitCode"]},
                    "value": m["value"],
                    "readingDateTime": m["readingDateTime"],
                    "userName": m.get("userName"),
                    "threshold": None,
                    "maximumDailyValue": 24 if m["unitCode"].upper() == "H" else None,
                }
                for m in a.get("meters", [])
            ],
        }

    def export(self, body: dict) -> list[dict]:
        action = (body.get("action") or "").strip().lower()
        page = max(int(body.get("currentPage") or 1), 1)
        size = min(max(int(body.get("pageSize") or 1000), 1), 1000)
        if action == "bodyexport":
            rows = sorted(self.data["bodies"], key=lambda b: b["bodyName"])
            if body.get("bodyNames"):
                rows = [b for b in rows if b["bodyName"] in set(body["bodyNames"])]
            return copy.deepcopy(rows[(page - 1) * size : page * size])
        if action == "assetmeterexport":
            rows = []
            for a in self._assets():
                if body.get("bodyNames") and not (set(body["bodyNames"]) & set(a.get("bodies") or [])):
                    continue
                for m in a.get("meters", []):
                    rows.append(
                        {
                            "assetCode": a["code"],
                            "meterName": m["name"],
                            "value": m["value"],
                            "logicalValue": m["value"],
                            "gapWithPreviousValue": m.get("gapWithPreviousValue"),
                            "readingDateTime": m["readingDateTime"],
                            "unitCode": m["unitCode"],
                            "userName": m.get("userName"),
                            "threshold": None,
                        }
                    )
            rows.sort(key=lambda r: (r["assetCode"], r["meterName"]))
            return rows[(page - 1) * size : page * size]
        raise ApiError(406, f"Unsupported export action '{body.get('action')}'. Supported: BodyExport, AssetMeterExport")

    # -- write endpoints ---------------------------------------------------
    def post_asset(self, body: dict) -> list[str]:
        with self._lock:
            code = (body.get("assetCode") or "").strip()
            name = (body.get("assetName") or "").strip()
            errors = []
            if not name:
                errors.append("assetName is required")
            if not code:
                errors.append("assetCode is required for connector imports (automatic codification is disabled on this tenant)")
            if errors:
                raise ApiError(406, *errors)
            if self._asset(code):
                raise ApiError(406, f"An asset with code '{code}' already exists")
            fam_code = body.get("assetFamilyCode")
            if fam_code and not self._family(fam_code):
                raise ApiError(406, f"Unknown asset family '{fam_code}'")
            parent_code = body.get("assetParentCode")
            parent = None
            if parent_code:
                parent = self._asset(parent_code)
                if not parent:
                    raise ApiError(406, f"Unknown parent asset '{parent_code}'")
                if parent["archived"]:
                    raise ApiError(406, f"Parent asset '{parent_code}' is archived")
            bodies = body.get("bodyNames") or []
            for b in bodies:
                bd = self._body(b)
                if not bd:
                    raise ApiError(406, f"Unknown body '{b}'")
                if bd["archived"]:
                    raise ApiError(406, f"Body '{b}' is archived")
            if not bodies:
                if parent is None:
                    raise ApiError(406, "At least one body is required for a root asset (bodyNames)")
                bodies = list(parent.get("bodies") or [])
            self._assets().append(
                {
                    "code": code,
                    "name": name,
                    "interfaceKey": body.get("interfaceKey"),
                    "parentCode": parent_code or None,
                    "familyCode": fam_code,
                    "familyName": self._family(fam_code)["name"] if fam_code else None,
                    "bodies": bodies,
                    "archived": False,
                    "criticality": None,
                    "state": None,
                    "node": fam_code in ("PLATFORM", "SECTION"),
                    "inServiceDate": None,
                    "sharedData": bool(body.get("sharedData", False)),
                    "lastModificationDateTime": now_iso(),
                    "meters": [],
                }
            )
            self.calls["writes"] += 1
            self._persist()
            return [f"Asset '{code}' created"]

    def patch_asset(self, body: dict) -> list[str]:
        with self._lock:
            code = (body.get("assetCode") or "").strip()
            if not code:
                raise ApiError(406, "assetCode is required")
            a = self._asset(code)
            if not a:
                raise ApiError(404, f"Asset '{code}' not found")
            changes = []
            if "archived" in body and body["archived"] is not None:
                target = bool(body["archived"])
                if target and not a["archived"]:
                    active_children = [c for c in self._children(code) if not c["archived"]]
                    if active_children:
                        raise ApiError(
                            406,
                            f"Cannot archive asset '{code}': it has {len(active_children)} active child asset(s) "
                            f"({', '.join(sorted(c['code'] for c in active_children)[:5])})",
                        )
                if a["archived"] != target:
                    a["archived"] = target
                    changes.append("archived")
            if body.get("assetName"):
                a["name"] = body["assetName"].strip()
                changes.append("assetName")
            if body.get("parentCode"):
                p = self._asset(body["parentCode"])
                if not p:
                    raise ApiError(406, f"Unknown parent asset '{body['parentCode']}'")
                a["parentCode"] = p["code"]
                changes.append("parentCode")
            if body.get("bodyNames"):
                for b in body["bodyNames"]:
                    if not self._body(b):
                        raise ApiError(406, f"Unknown body '{b}'")
                a["bodies"] = list(body["bodyNames"])
                changes.append("bodyNames")
            if body.get("assetFamilyCode"):
                fam = self._family(body["assetFamilyCode"])
                if not fam:
                    raise ApiError(406, f"Unknown asset family '{body['assetFamilyCode']}'")
                a["familyCode"] = fam["code"]
                a["familyName"] = fam["name"]
                changes.append("assetFamilyCode")
            if body.get("interfaceKey") is not None:
                a["interfaceKey"] = body["interfaceKey"]
                changes.append("interfaceKey")
            a["lastModificationDateTime"] = now_iso()
            self.calls["writes"] += 1
            self._persist()
            return [f"Asset '{code}' updated ({', '.join(changes) if changes else 'no change'})"]

    def meter_update(self, body: dict) -> list[str]:
        with self._lock:
            code = body.get("assetCode")
            a = self._asset(code) if code else None
            if not a and body.get("assetInterfaceKey"):
                a = next((x for x in self._assets() if x.get("interfaceKey") == body["assetInterfaceKey"]), None)
            if not a:
                raise ApiError(406, f"Asset '{code or body.get('assetInterfaceKey')}' not found")
            if a["archived"]:
                raise ApiError(406, f"Asset '{a['code']}' is archived: meters cannot be updated")
            if body.get("dateTime") in (None, ""):
                raise ApiError(406, "dateTime is required")
            try:
                reading_at = parse_dt(str(body["dateTime"]))
            except ValueError:
                raise ApiError(406, f"dateTime '{body['dateTime']}' is not an ISO 8601 date-time")
            if reading_at > datetime.now(timezone.utc):
                raise ApiError(406, "dateTime cannot be in the future")
            value = body.get("value")
            if value is None:
                raise ApiError(406, "value is required")
            if isinstance(value, bool) or not isinstance(value, int):
                raise ApiError(406, f"value must be an integer (int64), got {value!r}")
            if value < 0:
                raise ApiError(406, "value cannot be negative")
            name = (body.get("name") or "Running hours").strip()
            unit = (body.get("unitCode") or "H").strip()
            meter = next((m for m in a.setdefault("meters", []) if m["name"].lower() == name.lower()), None)
            if meter:
                if meter["unitCode"].upper() != unit.upper():
                    raise ApiError(406, f"Unit mismatch for meter '{name}' on '{a['code']}': expected '{meter['unitCode']}', got '{unit}'")
                last_at = parse_dt(meter["readingDateTime"])
                if reading_at <= last_at:
                    raise ApiError(
                        406,
                        f"A reading dated {meter['readingDateTime']} already exists for meter '{name}' on '{a['code']}'; "
                        f"new readings must be more recent",
                    )
                if value < meter["value"]:
                    raise ApiError(
                        406,
                        f"Meter value {value} is lower than the previous reading {meter['value']} for '{name}' on '{a['code']}' "
                        f"(cumulative meters cannot decrease; create a new meter after a counter replacement)",
                    )
                meter["gapWithPreviousValue"] = value - meter["value"]
                meter["value"] = value
                meter["readingDateTime"] = reading_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                meter["userName"] = body.get("userName") or "CONNECTOR"
            else:
                a["meters"].append(
                    {
                        "name": name,
                        "unitCode": unit,
                        "value": value,
                        "gapWithPreviousValue": None,
                        "readingDateTime": reading_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "userName": body.get("userName") or "CONNECTOR",
                    }
                )
            self.calls["writes"] += 1
            self._persist()
            return [f"Meter '{name}' updated on '{a['code']}': {value} {unit}"]

    # -- admin ---------------------------------------------------------------
    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)

    def apply_scenario(self, name: str) -> str:
        t = self.data.get("scenarioTargets", {})
        with self._lock:
            if name == "technician_adds_system":
                code, parent, eq = t["new_system_code"], t["new_system_parent"], t["new_equipment_code"]
                if self._asset(code):
                    return "already applied"
                section = self._asset(parent)
                if not section:
                    return f"parent section {parent} not found"
                state_ok = {"name": "1 - Disponible - Normal", "servicing": True}
                self._assets().append(
                    dict(code=code, name="New chemical injection package", interfaceKey=None, parentCode=parent,
                         familyCode="SYS_WI", familyName="System - Water injection", bodies=list(section["bodies"]),
                         archived=False, criticality={"code": "PC", "name": "Production Critical"}, state=state_ok,
                         node=False, inServiceDate="2026-09-01T00:00:00Z", sharedData=False,
                         lastModificationDateTime=now_iso(), meters=[])
                )
                self._assets().append(
                    dict(code=eq, name="Methanol injection pump", interfaceKey=None, parentCode=code,
                         familyCode="PU_RE", familyName="Pumps - Reciprocating", bodies=list(section["bodies"]),
                         archived=False, criticality=None, state=state_ok, node=False,
                         inServiceDate="2026-09-01T00:00:00Z", sharedData=False,
                         lastModificationDateTime=now_iso(), meters=[])
                )
                self._persist()
                return f"added {code} (+ {eq}) under {parent}"
            if name == "technician_archives_system":
                code = t["system_to_archive"]
                target = self._asset(code)
                if not target:
                    return f"{code} not found"
                for c in self._children(code):
                    c["archived"] = True
                    c["lastModificationDateTime"] = now_iso()
                target["archived"] = True
                target["lastModificationDateTime"] = now_iso()
                self._persist()
                return f"archived {code} and its children"
            if name == "key_user_renames_section":
                code = t["section_to_rename"]
                s = self._asset(code)
                if s:
                    s["name"] = s["name"] + " & power"
                    s["lastModificationDateTime"] = now_iso()
                    self._persist()
                return f"renamed {code}"
            if name == "api_degraded":
                self.chaos_rate_override = 0.3
                return "chaos rate set to 0.3 (30% of connector calls fail with 500/503) until reset"
            if name == "rate_limit_strict":
                self.rate_limit_override = 10
                return "rate limit set to 10 requests/minute until reset"
            raise ApiError(404, f"Unknown scenario '{name}'")
