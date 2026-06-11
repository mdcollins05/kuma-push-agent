import logging
import pathlib

import yaml
from pydantic import ValidationError

from .models import Monitor
from .schemas import HttpConfig

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
    seeded = 0
    for entry in monitors:
        raw_config = entry.get("config")
        if not isinstance(raw_config, dict):
            # Build from flat YAML keys (pre-v0.3.0 format).
            raw_config = {
                "type": "http",
                "url": entry.get("url", ""),
                "method": (entry.get("method") or "GET").upper(),
                "headers": entry.get("headers") or {},
                "body": entry.get("body"),
                "expected_codes": entry.get("expected_codes", [200]),
                "keyword": entry.get("keyword") or None,
                "max_response_ms": entry.get("max_response_ms"),
                "verify_ssl": entry.get("verify_ssl", True),
            }
        else:
            raw_config.setdefault("type", "http")

        # Route the seed config through the same schema the API uses so
        # malformed YAML is rejected up-front rather than persisted as a
        # broken monitor that only blows up at check time.
        try:
            validated = HttpConfig.model_validate(raw_config)
        except ValidationError as exc:
            logger.warning(
                "Skipping invalid seed entry %r: %s",
                entry.get("name", "<unnamed>"), exc.errors(),
            )
            continue

        monitor = Monitor(
            name=entry.get("name", "Unnamed"),
            interval=int(entry.get("interval", 60)),
            config=validated.model_dump(),
        )
        db.add(monitor)
        seeded += 1

    db.commit()
    logger.info("Seeded %d monitors from %s", seeded, seed_file)
