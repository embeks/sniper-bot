"""
On-chain utilities for fast token verification without indexers
"""
import logging
from solders.pubkey import Pubkey
from typing import Optional, Set

logger = logging.getLogger(__name__)

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

def derive_bonding_curve_pda(mint: str) -> Optional[str]:
    """Derive bonding curve PDA for a mint"""
    try:
        mint_pubkey = Pubkey.from_string(mint)
        program_pubkey = Pubkey.from_string(PUMPFUN_PROGRAM)

        seeds = [
            b"bonding-curve",
            bytes(mint_pubkey)
        ]

        pda, _ = Pubkey.find_program_address(seeds, program_pubkey)
        return str(pda)
    except Exception as e:
        logger.error(f"Error deriving PDA: {e}")
        return None

def extract_buyer_from_transaction(tx_data: dict) -> Optional[str]:
    """Extract buyer address from parsed transaction"""
    try:
        if not tx_data or not tx_data.value:
            return None

        tx = tx_data.value

        # Look for account keys (buyer is typically first signer)
        if hasattr(tx, 'transaction') and hasattr(tx.transaction, 'message'):
            message = tx.transaction.message
            if hasattr(message, 'account_keys') and len(message.account_keys) > 1:
                # First account after program is usually the buyer
                return str(message.account_keys[1])

        return None
    except Exception as e:
        logger.error(f"Error extracting buyer: {e}")
        return None
