import asyncio
import contextlib
import os
from typing import Dict, Iterable, Optional, Tuple

from .utils import get_logger

Target = Tuple[str, int]  # (ip, port)


class TcpProber:
    """Checks that node IPs accept TCP connections from the monitor.

    The panel only knows whether a node is connected to *it*. A node can stay
    connected while users cannot reach it: a firewall change on the inbound
    port, a blocked IP, a dead listener. Opening a TCP connection to the IP that
    is published in DNS, on the port users connect to, catches those cases.

    A probe only proves reachability from wherever this monitor runs, so it
    complements the panel's view rather than replacing it.
    """

    def __init__(self, concurrency: int = 20):
        self.logger = get_logger(__name__)
        self._semaphore = asyncio.Semaphore(concurrency)

    async def probe(self, ip: str, port: int, timeout: float, attempts: int) -> Optional[str]:
        """Return None if a connection succeeded, otherwise the last error."""
        last_error = "unknown error"
        for attempt in range(attempts):
            async with self._semaphore:
                try:
                    _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
                except asyncio.TimeoutError:
                    last_error = "timed out"
                except OSError as e:
                    # asyncio wraps the errno in "Connect call failed (...)"; prefer the plain reason.
                    last_error = (os.strerror(e.errno) if e.errno else None) or e.strerror or str(e) or type(e).__name__
                else:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(writer.wait_closed(), 1)
                    return None
            if attempt + 1 < attempts:
                await asyncio.sleep(0.5)
        return last_error

    async def probe_many(self, targets: Iterable[Target], timeout: float, attempts: int) -> Dict[Target, Optional[str]]:
        unique = sorted(set(targets))
        results = await asyncio.gather(*(self.probe(ip, port, timeout, attempts) for ip, port in unique))
        failed = sum(1 for r in results if r is not None)
        self.logger.info(f"Probed {len(unique)} target(s): {len(unique) - failed} reachable, {failed} unreachable")
        return dict(zip(unique, results))
