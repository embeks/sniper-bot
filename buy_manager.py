# buy_manager.py - COMPLETE FIXED VERSION
"""
Centralized buy function manager to prevent circular imports
This file was missing and causing your crashes - now properly implemented
"""

from typing import Optional, Callable
import logging
import asyncio

# Global buy function reference - properly named without asterisks
_buy_function: Optional[Callable] = None
_original_buy = None

def set_buy_function(func: Callable):
    """Set the active buy function for the bot to use"""
    global _buy_function
    _buy_function = func
    logging.info(f"[BuyManager] Buy function set to: {func.__name__}")

def get_buy_function() -> Callable:
    """Get the current buy function, falling back to utils.buy_token if none set"""
    global _original_buy
    
    if _buy_function is None:
        # Lazy load the original buy function from utils to avoid circular import
        if _original_buy is None:
            from utils import buy_token
            _original_buy = buy_token
        return _original_buy
    
    return _buy_function

async def execute_buy(mint: str, force_amount: Optional[float] = None, **kwargs) -> bool:
    """
    Execute buy with current strategy
    This is the main entry point for all buy operations
    
    Args:
        mint: Token mint address to buy
        force_amount: Optional forced amount in SOL
        **kwargs: Additional arguments to pass to buy function
    
    Returns:
        bool: True if buy succeeded, False otherwise
    """
    try:
        buy_func = get_buy_function()
        
        # Log the buy attempt
        logging.info(f"[BuyManager] Executing buy for {mint[:8]}... using {buy_func.__name__}")
        
        # Handle different function signatures
        if force_amount is not None:
            # Try with force_amount parameter
            try:
                result = await buy_func(mint, force_amount=force_amount, **kwargs)
            except TypeError:
                # Function doesn't accept force_amount, try without
                logging.debug("[BuyManager] Function doesn't accept force_amount, trying without")
                result = await buy_func(mint, **kwargs)
        else:
            # Normal buy without forced amount
            result = await buy_func(mint, **kwargs)
        
        if result:
            logging.info(f"[BuyManager] Buy successful for {mint[:8]}...")
        else:
            logging.warning(f"[BuyManager] Buy failed for {mint[:8]}...")
        
        return result
        
    except Exception as e:
        logging.error(f"[BuyManager] Error executing buy for {mint[:8]}...: {e}")
        return False

# Alias for backward compatibility
buy_token = execute_buy

# Initialize on module load
def initialize():
    """Initialize buy manager on module load"""
    logging.info("[BuyManager] Module initialized - ready to handle buy operations")

# Call initialization
initialize()
