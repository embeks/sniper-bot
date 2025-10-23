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

def extract_buyer_from_transaction(tx_response) -> Optional[str]:
    """
    Extract buyer address from Solana transaction response
    Buyer = fee payer (first signer in transaction)
    """
    try:
        # Handle None or missing response
        if not tx_response:
            return None

        # Get the actual transaction from the RPC response
        if hasattr(tx_response, 'value') and tx_response.value:
            tx = tx_response.value
        else:
            return None

        # Access the transaction and message
        if hasattr(tx, 'transaction'):
            transaction = tx.transaction

            # Get the message (contains account keys)
            if hasattr(transaction, 'message'):
                message = transaction.message

                # Get account keys - first key is the fee payer (buyer)
                if hasattr(message, 'account_keys'):
                    account_keys = message.account_keys

                    if len(account_keys) > 0:
                        # First account is the fee payer = buyer
                        buyer = str(account_keys[0])
                        logger.debug(f"Extracted buyer: {buyer[:8]}...")
                        return buyer

        logger.debug("Could not find account keys in transaction")
        return None

    except Exception as e:
        logger.debug(f"Error extracting buyer: {e}")
        return None
