# buy_manager.py - NEW FILE TO CREATE
"""Centralized buy function manager to prevent circular imports"""
from typing import Optional, Callable
import logging

_buy_function: Optional[Callable] = None
_original_buy = None

def set_buy_function(func: Callable):
    """Set the active buy function"""
    global _buy_function
    _buy_function = func
    logging.info(f"Buy function set to: {func.__name__}")

def get_buy_function() -> Callable:
    """Get the current buy function"""
    global _original_buy
    if _buy_function is None:
        if _original_buy is None:
            from utils import buy_token
            _original_buy = buy_token
        return _original_buy
    return _buy_function

async def execute_buy(mint: str, *args, **kwargs):
    """Execute buy with current strategy"""
    buy_func = get_buy_function()
    return await buy_func(mint, *args, **kwargs)
