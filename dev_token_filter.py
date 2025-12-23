"""
Dev Token Count Filter - Block serial ruggers via Helius API
Counts PumpFun CREATE transactions to identify serial token launchers.
"""

import aiohttp
import asyncio
import logging

from config import HELIUS_API_KEY

logger = logging.getLogger(__name__)

_creator_token_cache = {}
_cache_lock = asyncio.Lock()


async def get_dev_token_count(creator_wallet: str, timeout: float = 2.5) -> int:
    """
    Count how many PumpFun tokens this wallet has created.
    Returns: token count, or -1 on error (FAIL-CLOSED - caller should block)
    Timeout raised to 2.5s to avoid false negatives on slow API responses.
    Includes retry logic: on first timeout, wait 200ms and retry once.
    """
    if not creator_wallet or not HELIUS_API_KEY:
        return -1

    async with _cache_lock:
        if creator_wallet in _creator_token_cache:
            return _creator_token_cache[creator_wallet]

    count = -1
    url = f"https://api.helius.xyz/v0/addresses/{creator_wallet}/transactions?api-key={HELIUS_API_KEY}&type=CREATE&source=PUMP_FUN&limit=2"

    for attempt in range(2):  # Try twice before giving up
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            count = len(data)
                            break  # Success, exit retry loop

        except Exception as e:
            logger.debug(f"Dev token count error (attempt {attempt + 1}): {e}")
            if attempt == 0:
                # First timeout - wait 200ms and retry once
                await asyncio.sleep(0.2)
                continue
            # Second attempt failed, count stays -1

    if count >= 0:
        async with _cache_lock:
            _creator_token_cache[creator_wallet] = count

    return count


async def is_first_time_creator(creator_wallet: str) -> bool:
    """Returns False if wallet has 1+ historical token creates (serial rugger)."""
    count = await get_dev_token_count(creator_wallet)
    return count == -1 or count == 0
