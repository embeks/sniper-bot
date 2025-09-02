# buy_manager.py - COMPLETE FIXED VERSION
"""
Buy function management module to prevent circular dependencies
This module handles the dynamic assignment of buy functions
"""

import logging
from typing import Optional, Callable, Any
import os

# Global variable to hold the active buy function
_active_buy_function = None

def set_buy_function(func: Callable) -> None:
    """
    Set the active buy function that will be used for all buy operations
    
    Args:
        func: The buy function to use (elite_buy_token or monster_buy_token)
    """
    global _active_buy_function
    _active_buy_function = func
    logging.info(f"[BUY_MANAGER] Buy function set to: {func.__name__ if func else 'None'}")

def get_buy_function() -> Optional[Callable]:
    """
    Get the currently active buy function
    
    Returns:
        The active buy function or None if not set
    """
    return _active_buy_function

async def execute_buy(mint: str, force_amount: float = None) -> bool:
    """
    Execute a buy using the currently active buy function
    
    Args:
        mint: Token mint address to buy
        force_amount: Optional forced amount in SOL
        
    Returns:
        True if buy succeeded, False otherwise
    """
    if _active_buy_function is None:
        logging.error("[BUY_MANAGER] No buy function set! Call set_buy_function first")
        # Fallback to importing the default buy function
        try:
            from utils import buy_token as original_buy_token
            logging.warning("[BUY_MANAGER] Using fallback buy_token from utils")
            return await original_buy_token(mint)
        except Exception as e:
            logging.error(f"[BUY_MANAGER] Failed to import fallback buy function: {e}")
            return False
    
    try:
        logging.info(f"[BUY_MANAGER] Executing buy for {mint[:8]}... using {_active_buy_function.__name__}")
        result = await _active_buy_function(mint, force_amount)
        return result
    except Exception as e:
        logging.error(f"[BUY_MANAGER] Buy execution failed: {e}")
        return False

# Compatibility alias for modules that import 'buy_token'
async def buy_token(mint: str, force_amount: float = None) -> bool:
    """
    Compatibility wrapper for modules expecting 'buy_token' function
    
    Args:
        mint: Token mint address to buy
        force_amount: Optional forced amount in SOL
        
    Returns:
        True if buy succeeded, False otherwise
    """
    return await execute_buy(mint, force_amount)

# Function that sniper_logic and other modules can import
async def managed_buy_token(mint: str, amount_sol: float = None) -> bool:
    """
    Managed buy function that uses the configured buy strategy
    
    Args:
        mint: Token mint address
        amount_sol: Optional amount in SOL to buy
        
    Returns:
        True if buy succeeded, False otherwise
    """
    # If no buy function is set, try to import and use the default
    if _active_buy_function is None:
        logging.warning("[BUY_MANAGER] No buy function configured, attempting to use default")
        try:
            # Try to import from utils first
            from utils import buy_token as original_buy_token
            
            # If amount is specified, temporarily override BUY_AMOUNT_SOL
            if amount_sol is not None:
                original_amount = os.getenv("BUY_AMOUNT_SOL")
                os.environ["BUY_AMOUNT_SOL"] = str(amount_sol)
                
                try:
                    result = await original_buy_token(mint)
                finally:
                    # Restore original amount
                    if original_amount:
                        os.environ["BUY_AMOUNT_SOL"] = original_amount
                    else:
                        del os.environ["BUY_AMOUNT_SOL"]
                
                return result
            else:
                return await original_buy_token(mint)
                
        except Exception as e:
            logging.error(f"[BUY_MANAGER] Failed to use default buy function: {e}")
            return False
    
    # Use the configured buy function
    return await execute_buy(mint, amount_sol)

# Additional helper functions for sniper_logic compatibility
def is_buy_function_set() -> bool:
    """
    Check if a buy function has been configured
    
    Returns:
        True if buy function is set, False otherwise
    """
    return _active_buy_function is not None

def get_buy_function_name() -> str:
    """
    Get the name of the currently active buy function
    
    Returns:
        Name of the buy function or 'None' if not set
    """
    if _active_buy_function:
        return _active_buy_function.__name__
    return "None"

# Initialize logging
logging.basicConfig(level=logging.INFO)
logging.info("[BUY_MANAGER] Module loaded, waiting for buy function configuration...")
