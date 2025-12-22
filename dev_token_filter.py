"""
Dev Token Count Filter - Query creator's historical token launches
"""

import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

_creator_token_cache = {}
_cache_lock = asyncio.Lock()

async def get_dev_token_count(creator_wallet: str, timeout: float = 0.5) -> int:
    if not creator_wallet:
        return -1

    async with _cache_lock:
        if creator_wallet in _creator_token_cache:
            return _creator_token_cache[creator_wallet]

    count = -1

    try:
        url = f"https://frontend-api.pump.fun/coins/user-created-coins/{creator_wallet}?limit=50&includeNsfw=true"

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={'Accept': 'application/json'}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        count = len(data)
                    elif isinstance(data, dict) and 'coins' in data:
                        count = len(data['coins'])
                    else:
                        count = 0
                elif resp.status == 404:
                    count = 0

    except asyncio.TimeoutError:
        logger.debug(f"Dev token count timeout for {creator_wallet[:8]}...")
    except Exception as e:
        logger.debug(f"Dev token count error: {e}")

    if count >= 0:
        async with _cache_lock:
            _creator_token_cache[creator_wallet] = count

    return count
