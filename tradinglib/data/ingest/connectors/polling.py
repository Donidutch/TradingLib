"""Polling connector: a REST endpoint re-read on a cadence, with change detection."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from tradinglib.data.ingest.connectors.base import FetchResult
from tradinglib.data.ingest.connectors.rest import RestConnector
from tradinglib.data.ingest.formats import parse_timestamp, timestamp_field
from tradinglib.data.ingest.source import ScheduleKind, SourceConfig, SourceStatus

logger = logging.getLogger(__name__)

#: How many record fingerprints to remember when there is no watermark field.
SEEN_LIMIT: int = 50_000

#: Fallback poll interval when the source's schedule is not periodic.
DEFAULT_POLL_SECONDS: float = 60.0


class PollingConnector(RestConnector):
    """Repeatedly fetches an endpoint and emits only records that changed.

    Change detection uses the record timestamp when one is present (a
    watermark: only records strictly newer than the highest timestamp already
    seen are emitted), and a content fingerprint otherwise. The first poll of a
    freshly created connector emits everything it receives - there is nothing to
    compare against yet, and dropping the first batch would silently lose data.

    Advanced configuration keys, in addition to the REST ones::

        watermark_field   record field holding the timestamp (default: detected)
        dedupe            "watermark" (default) / "fingerprint" / "off"
        poll_seconds      override for stream()'s cadence
    """

    protocol = "polling"

    def __init__(self, config: SourceConfig, **kwargs: Any) -> None:
        """Bind the connector and initialise the change-detection state."""
        super().__init__(config, **kwargs)
        self.watermark: datetime | None = None
        self._seen: set[str] = set()
        self._polls = 0

    async def connect(self) -> None:
        """Create the HTTP client and initialise the change-detection cursor."""
        await super().connect()

    async def close(self) -> None:
        """Close the HTTP client and drop the change-detection state."""
        await super().close()
        self._seen.clear()

    # --- change detection -------------------------------------------------
    @property
    def dedupe_mode(self) -> str:
        """The configured change-detection mode."""
        mode = str(self.config.advanced.get("dedupe") or "watermark").lower()
        return mode if mode in ("watermark", "fingerprint", "off") else "watermark"

    @staticmethod
    def fingerprint(record: dict[str, Any]) -> str:
        """Return a stable content fingerprint for a record."""
        blob = json.dumps(record, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()

    def _watermark_field(self, records: list[dict[str, Any]]) -> str | None:
        """Return the field used as the watermark, if any."""
        configured = self.config.advanced.get("watermark_field")
        if configured:
            return str(configured)
        return timestamp_field(records[0]) if records else None

    def select_new(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return the subset of ``records`` not already ingested, updating state."""
        if not records or self.dedupe_mode == "off":
            return records

        field = None if self.dedupe_mode == "fingerprint" else self._watermark_field(records)
        zone = self.config.timezone
        if field:
            highest = self.watermark
            fresh: list[dict[str, Any]] = []
            for record in records:
                stamp = parse_timestamp(record.get(field), tz=zone)
                if stamp is None:
                    fresh.append(record)
                    continue
                if self.watermark is None or stamp > self.watermark:
                    fresh.append(record)
                if highest is None or stamp > highest:
                    highest = stamp
            self.watermark = highest
            return fresh

        fresh = []
        for record in records:
            digest = self.fingerprint(record)
            if digest in self._seen:
                continue
            self._seen.add(digest)
            fresh.append(record)
        if len(self._seen) > SEEN_LIMIT:
            logger.info("ingest %s: fingerprint cache reset at %d entries", self.config.id, SEEN_LIMIT)
            self._seen = set(list(self._seen)[-SEEN_LIMIT // 2 :])
        return fresh

    # --- fetch ------------------------------------------------------------
    async def fetch(self, **params: Any) -> FetchResult:
        """Poll once and return records new since the last cursor."""
        raw = await super().fetch(**params)
        self._polls += 1
        fresh = self.select_new(raw.records)
        return FetchResult(
            records=fresh,
            fetched_at=raw.fetched_at,
            latency_ms=raw.latency_ms,
            complete=raw.complete,
            cursor=self.watermark.isoformat() if self.watermark else raw.cursor,
        )

    async def fetch_all(self, **params: Any) -> FetchResult:
        """Poll once and return every record, bypassing change detection."""
        return await super().fetch(**params)

    # --- stream -----------------------------------------------------------
    @property
    def poll_seconds(self) -> float:
        """The cadence :meth:`stream` polls at."""
        override = self.config.advanced.get("poll_seconds")
        if override:
            return float(override)
        schedule = self.config.parsed_schedule
        if schedule.kind is ScheduleKind.INTERVAL and schedule.interval_seconds:
            return float(schedule.interval_seconds)
        return DEFAULT_POLL_SECONDS

    async def stream(self) -> AsyncIterator[dict[str, Any]]:
        """Poll on the configured interval, yielding new records.

        Runs until the consumer stops iterating or the task is cancelled. A
        failed poll is logged and retried on the next tick rather than ending
        the stream.
        """
        while True:
            try:
                batch = await self.fetch()
            except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the stream
                logger.warning("ingest %s: poll failed: %s", self.config.id, exc)
                self._mark_failure(str(exc))
            else:
                for record in batch.records:
                    yield record
            await asyncio.sleep(self.poll_seconds)

    async def status(self) -> SourceStatus:
        """Report status from the most recent poll."""
        return await super().status()

    def state(self) -> dict[str, Any]:
        """Return the connector's change-detection state, for the UI/debugging."""
        return {
            "polls": self._polls,
            "dedupe": self.dedupe_mode,
            "watermark": self.watermark.isoformat() if self.watermark else None,
            "fingerprints": len(self._seen),
            "now": datetime.now(timezone.utc).isoformat(),
        }


__all__ = ["DEFAULT_POLL_SECONDS", "SEEN_LIMIT", "PollingConnector"]
