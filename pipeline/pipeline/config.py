"""Runtime configuration, read from the environment.

Only non-secret defaults live here. The CMMS API key is read from the
environment (CMMS_API_KEY) and never hard-coded or logged; in production it
would be injected from Azure Key Vault into the container/task environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    cmms_base_url: str = field(default_factory=lambda: os.getenv("CMMS_BASE_URL", "http://localhost:8080"))
    cmms_tenant: str = field(default_factory=lambda: os.getenv("CMMS_TENANT", "PERENCO"))
    cmms_api_key: str = field(default_factory=lambda: os.getenv("CMMS_API_KEY", "demo-key-global"))

    mdm_db_path: Path = field(
        default_factory=lambda: Path(os.getenv("SYSTEMREF_DB_PATH", str(REPO_ROOT / "mdm_data" / "systemref.sqlite3")))
    )
    iot_exports_dir: Path = field(default_factory=lambda: Path(os.getenv("IOT_EXPORTS_DIR", str(REPO_ROOT / "iot_historian" / "exports"))))
    audit_db_path: Path = field(default_factory=lambda: Path(os.getenv("AUDIT_DB_PATH", str(REPO_ROOT / "pipeline" / "state" / "audit.sqlite3"))))

    # Business thresholds / non-functional requirements (docs/02_business_rules.md, ARCHITECTURE_.md)
    archive_ratio_threshold: float = field(default_factory=lambda: float(os.getenv("ARCHIVE_RATIO_THRESHOLD", "0.10")))
    cmms_rate_limit_per_minute: int = field(default_factory=lambda: int(os.getenv("CMMS_RATE_LIMIT_PER_MINUTE", "50")))
    http_max_retries: int = field(default_factory=lambda: int(os.getenv("CMMS_HTTP_MAX_RETRIES", "6")))
    http_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("CMMS_HTTP_TIMEOUT", "10")))

    # MDM active-scope rule. See ARCHITECTURE_.md part A section 3 and DECISIONS.md.
    # "conventional": date_start <= as_of and (date_end is null or date_end > as_of)
    # "literal": date_start < as_of and (date_end is null or date_end < as_of)  -- as stated verbatim on the
    #            clarification call; kept behind a flag pending final fixture-level confirmation.
    mdm_active_rule: str = field(default_factory=lambda: os.getenv("MDM_ACTIVE_RULE", "conventional"))

    dry_run: bool = field(default_factory=lambda: _bool("SYNC_DRY_RUN", False))


def get_settings() -> Settings:
    return Settings()
