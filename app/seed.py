import logging
import pathlib

import yaml

from .models import Monitor

logger = logging.getLogger(__name__)


def seed_from_yaml(db, seed_file: str) -> None:
    """Seed monitors from YAML if the monitors table is empty. Idempotent — only runs once."""
    if db.query(Monitor).count() > 0:
        return

    path = pathlib.Path(seed_file)
    if not path.exists():
        return

    try:
        data = yaml.safe_load(path.read_text())
    except Exception as exc:
        logger.warning("Could not parse seed file %s: %s", seed_file, exc)
        return

    monitors = data.get("monitors") or []
    for entry in monitors:
        config = entry.get("config")
        if not isinstance(config, dict):
            # Build from flat YAML keys (pre-v0.3.0 format).
            config = {
                "type": "http",
                "url": entry.get("url", ""),
                "method": (entry.get("method") or "GET").upper(),
                "headers": entry.get("headers") or {},
                "body": entry.get("body"),
                "expected_codes": entry.get("expected_codes", [200]),
                "keyword": entry.get("keyword") or None,
                "max_response_ms": entry.get("max_response_ms"),
                "verify_ssl": bool(entry.get("verify_ssl", True)),
            }
        else:
            config.setdefault("type", "http")
        monitor = Monitor(
            name=entry.get("name", "Unnamed"),
            interval=int(entry.get("interval", 60)),
            config=config,
        )
        db.add(monitor)

    db.commit()
    logger.info("Seeded %d monitors from %s", len(monitors), seed_file)
