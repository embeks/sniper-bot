# shared_state.py - Shared state between modules to avoid circular imports
"""
This module contains shared state that needs to be accessed by multiple modules
without creating circular import dependencies.
"""

# Track PumpFun tokens globally across all modules
# Key: mint address, Value: dict with metadata
pumpfun_tokens = {}

# Track tokens we've already bought to prevent duplicates
already_bought = set()

# Track recent buy attempts to prevent rapid retries
recent_buy_attempts = {}

# Track trending tokens
trending_tokens = set()

# Migration watch list for PumpFun graduations
migration_watch_list = set()

# Pool verification cache
pool_verification_cache = {}

# Detected pools mapping
detected_pools = {}

# Momentum tracking
momentum_analyzed = {}
momentum_bought = set()
