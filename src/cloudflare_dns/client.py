import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

from cloudflare import AsyncCloudflare

from ..utils.logger import get_logger

T = TypeVar("T")

# 429 is a rate limit — the request is well-formed and worth repeating.
# Every other 4xx means the request itself is wrong (bad token, missing scope,
# unknown zone) and will fail identically on every retry.
_RETRYABLE_CLIENT_ERRORS = frozenset({429})


def _status_code(error: Exception) -> Optional[int]:
    """Best-effort HTTP status extraction across cloudflare-python and httpx errors."""
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _retry_after(error: Exception) -> Optional[float]:
    """Honour a server-provided Retry-After header when rate limited."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    try:
        return max(0.0, float(headers.get("Retry-After", "")))
    except (TypeError, ValueError):
        return None


def _record_to_dict(record: Any) -> Dict:
    return {
        "id": record.id,
        "name": record.name,
        "content": record.content,
        "type": record.type,
        "ttl": record.ttl,
        "proxied": record.proxied,
    }


class CloudflareClient:
    def __init__(self, api_token: str, rate_limit_delay: float = 0.25, retry_delay: float = 1.0, max_retries: int = 5):
        self.api_token = api_token
        self.logger = get_logger(__name__)
        self.cf = AsyncCloudflare(api_token=api_token)
        self.rate_limit_delay = rate_limit_delay
        self.retry_delay = retry_delay
        self.max_retries = max_retries
        self._last_request_time: float = 0
        self._rate_limit_lock = asyncio.Lock()

    async def _rate_limit(self) -> None:
        async with self._rate_limit_lock:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < self.rate_limit_delay:
                await asyncio.sleep(self.rate_limit_delay - elapsed)
            self._last_request_time = time.monotonic()

    async def _backoff(self, attempt: int, error: Exception) -> None:
        """Exponential backoff with jitter, capped at 30s."""
        override = _retry_after(error)
        if override is not None:
            await asyncio.sleep(min(override, 30.0))
            return
        delay = min(self.retry_delay * (2 ** (attempt - 1)), 30.0)
        await asyncio.sleep(delay * (0.5 + random.random() * 0.5))

    async def _call(self, description: str, operation: Callable[[], Awaitable[T]]) -> T:
        """Run a Cloudflare API call with rate limiting and retries.

        Client errors other than 429 are raised immediately: retrying an invalid
        token or a missing zone just multiplies the failure by max_retries on
        every monitoring cycle.
        """
        attempt = 0
        while True:
            try:
                await self._rate_limit()
                return await operation()
            except Exception as e:
                status = _status_code(e)
                if status is not None and 400 <= status < 500 and status not in _RETRYABLE_CLIENT_ERRORS:
                    self.logger.error(f"{description} failed with client error {status}, not retrying: {e}")
                    raise

                attempt += 1
                if attempt >= self.max_retries:
                    self.logger.error(f"{description} failed after {attempt} attempts: {e}")
                    raise
                self.logger.warning(f"{description} failed (attempt {attempt}/{self.max_retries}), retrying: {e}")
                await self._backoff(attempt, e)

    async def get_dns_records(self, zone_id: str, name: str = None, record_type: str = "A") -> List[Dict]:
        params = {"type": record_type}
        if name:
            params["name"] = name

        async def operation() -> List[Dict]:
            records_list = []
            async for record in self.cf.dns.records.list(zone_id=zone_id, **params):
                records_list.append(_record_to_dict(record))
            return records_list

        records = await self._call(f"Fetching DNS records for zone {zone_id}", operation)
        self.logger.debug(f"Found {len(records)} DNS records for zone {zone_id}")
        return records

    async def create_dns_record(
            self, zone_id: str, name: str, content: str, record_type: str = "A", ttl: int = 120, proxied: bool = False
    ) -> Dict:
        async def operation() -> Dict:
            record = await self.cf.dns.records.create(  # type: ignore[call-overload]
                zone_id=zone_id, type=record_type, name=name, content=content, ttl=int(ttl), proxied=proxied
            )
            return _record_to_dict(record)

        result = await self._call(f"Creating DNS record {name} -> {content}", operation)
        self.logger.info(f"Created DNS record: {name} -> {content}")
        return result

    async def update_dns_record(
            self,
            zone_id: str,
            record_id: str,
            name: str,
            content: str,
            record_type: str = "A",
            ttl: int = 120,
            proxied: bool = False,
    ) -> Dict:
        async def operation() -> Dict:
            record = await self.cf.dns.records.update(  # type: ignore[call-overload]
                dns_record_id=record_id,
                zone_id=zone_id,
                type=record_type,
                name=name,
                content=content,
                ttl=int(ttl),
                proxied=proxied,
            )
            return _record_to_dict(record)

        result = await self._call(f"Updating DNS record {name} -> {content}", operation)
        self.logger.info(f"Updated DNS record: {name} -> {content}")
        return result

    async def delete_dns_record(self, zone_id: str, record_id: str) -> None:
        async def operation() -> None:
            await self.cf.dns.records.delete(dns_record_id=record_id, zone_id=zone_id)

        await self._call(f"Deleting DNS record {record_id}", operation)
        self.logger.info(f"Deleted DNS record: {record_id}")

    async def get_record_by_name_and_content(
            self, zone_id: str, name: str, content: str, record_type: str = "A"
    ) -> Optional[Dict]:
        records = await self.get_dns_records(zone_id, name=name, record_type=record_type)
        for record in records:
            if record.get("content") == content:
                return record
        return None

    async def get_zone_id_by_domain(self, domain: str) -> Optional[str]:
        async def operation() -> Optional[str]:
            async for zone in self.cf.zones.list(name=domain):
                return zone.id
            return None

        zone_id = await self._call(f"Fetching zone for domain {domain}", operation)
        if zone_id:
            self.logger.info(f"Found zone_id for {domain}: {zone_id}")
        else:
            self.logger.error(f"No zone found for domain: {domain}")
        return zone_id
