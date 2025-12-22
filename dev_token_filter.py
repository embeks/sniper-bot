"""
Dev Token Count Filter - Block serial ruggers via Helius API
Counts PumpFun CREATE transactions to identify serial token launchers.
"""

import aiohttp
import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_creator_token_cache = {}
_cache_lock = asyncio.Lock()
HELIUS_API_KEY = os.getenv("HELIUS_API")


async def get_dev_token_count(creator_wallet: str, timeout: float = 1.0) -> int:
    """
    Count how many PumpFun tokens this wallet has created.
    Returns: token count, or -1 on error
    """
    if not creator_wallet or not HELIUS_API_KEY:
        return -1

    async with _cache_lock:
        if creator_wallet in _creator_token_cache:
            return _creator_token_cache[creator_wallet]

    count = -1
    try:
        url = f"https://api.helius.xyz/v0/addresses/{creator_wallet}/transactions?api-key={HELIUS_API_KEY}&limit=100"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        count = sum(1 for tx in data if tx.get("type") == "CREATE" and tx.get("source") == "PUMP_FUN")

    except Exception as e:
        logger.debug(f"Dev token count error: {e}")

    if count >= 0:
        async with _cache_lock:
            _creator_token_cache[creator_wallet] = count

    return count


async def is_first_time_creator(creator_wallet: str) -> bool:
    """Returns False if wallet has 2+ historical token creates (serial rugger)."""
    count = await get_dev_token_count(creator_wallet)
    return count == -1 or count <= 1
