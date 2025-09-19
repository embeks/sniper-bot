"""
PumpPortal Trader - Use their API to get properly formatted transactions
FIXED: UTF-8 decode error when handling transaction responses
"""

import aiohttp
import base58
import logging
from typing import Optional
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair

logger = logging.getLogger(__name__)

class PumpPortalTrader:
    """Use PumpPortal's API for transaction creation"""
    
    def __init__(self, wallet_manager, client):
        self.wallet = wallet_manager
        self.client = client
        self.api_url = "https://pumpportal.fun/api/trade-local"
    
    async def create_buy_transaction(
        self, 
        mint: str, 
        sol_amount: float,
        bonding_curve_key: str = None,
        slippage: int = 50
    ) -> Optional[str]:
        """Get a buy transaction from PumpPortal API"""
        try:
            payload = {
                "publicKey": str(self.wallet.pubkey),
                "action": "buy",
                "mint": mint,
                "denominatedInSol": "true",  # API expects string "true" not boolean
                "amount": sol_amount,
                "slippage": slippage,
                "priorityFee": 0.0001,
                "pool": "pump"
            }
            
            # Add bonding curve if provided
            if bonding_curve_key:
                payload["bondingCurveKey"] = bonding_curve_key
            
            logger.info(f"Requesting buy transaction for {mint[:8]}... amount: {sol_amount} SOL")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status == 200:
                        # Read response as bytes first to avoid UTF-8 decode issues
                        response_bytes = await response.read()
                        
                        # Try to parse as JSON
                        try:
                            import json
                            response_text = response_bytes.decode('utf-8', errors='ignore')
                            data = json.loads(response_text)
                            tx_base58 = data.get("transaction")
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            # If not JSON, assume the entire response is the base58 transaction
                            # Remove any non-base58 characters
                            tx_base58 = response_bytes.decode('ascii', errors='ignore').strip()
                        
                        if not tx_base58:
                            logger.error("No transaction data in response")
                            return None
                        
                        logger.info(f"Received transaction from PumpPortal (length: {len(tx_base58)})")
                        
                        # Decode base58 transaction - this is already bytes, no UTF-8 involved
                        try:
                            tx_bytes = base58.b58decode(tx_base58)
                        except Exception as e:
                            logger.error(f"Failed to decode base58 transaction: {e}")
                            return None
                        
                        # Construct VersionedTransaction from bytes
                        try:
                            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                        except Exception as e:
                            logger.error(f"Failed to parse transaction bytes: {e}")
                            return None
                        
                        # Sign with our keypair
                        try:
                            versioned_tx.sign([self.wallet.keypair])
                        except Exception as e:
                            logger.error(f"Failed to sign transaction: {e}")
                            return None
                        
                        # Send the signed transaction
                        logger.info(f"Sending signed transaction for {mint[:8]}...")
                        try:
                            # Send as bytes, not string
                            response = self.client.send_raw_transaction(bytes(versioned_tx))
                            sig = str(response.value)
                            logger.info(f"✅ Transaction sent: {sig}")
                            return sig
                        except Exception as e:
                            logger.error(f"Failed to send transaction: {e}")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"PumpPortal API error ({response.status}): {error_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Failed to create buy transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    async def create_sell_transaction(
        self,
        mint: str,
        token_amount: float,
        bonding_curve_key: str = None,
        slippage: int = 50
    ) -> Optional[str]:
        """Get a sell transaction from PumpPortal API"""
        try:
            payload = {
                "publicKey": str(self.wallet.pubkey),
                "action": "sell",
                "mint": mint,
                "denominatedInSol": "false",  # API expects string "false" not boolean
                "amount": token_amount,
                "slippage": slippage,
                "priorityFee": 0.0001,
                "pool": "pump"
            }
            
            if bonding_curve_key:
                payload["bondingCurveKey"] = bonding_curve_key
            
            logger.info(f"Requesting sell transaction for {mint[:8]}... amount: {token_amount} tokens")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status == 200:
                        # Read response as bytes first to avoid UTF-8 decode issues
                        response_bytes = await response.read()
                        
                        # Try to parse as JSON
                        try:
                            import json
                            response_text = response_bytes.decode('utf-8', errors='ignore')
                            data = json.loads(response_text)
                            tx_base58 = data.get("transaction")
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            # If not JSON, assume the entire response is the base58 transaction
                            tx_base58 = response_bytes.decode('ascii', errors='ignore').strip()
                        
                        if not tx_base58:
                            logger.error("No transaction data in response")
                            return None
                        
                        # Decode base58 transaction
                        try:
                            tx_bytes = base58.b58decode(tx_base58)
                        except Exception as e:
                            logger.error(f"Failed to decode base58 transaction: {e}")
                            return None
                        
                        # Construct VersionedTransaction from bytes
                        try:
                            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                        except Exception as e:
                            logger.error(f"Failed to parse transaction bytes: {e}")
                            return None
                        
                        # Sign with our keypair
                        try:
                            versioned_tx.sign([self.wallet.keypair])
                        except Exception as e:
                            logger.error(f"Failed to sign transaction: {e}")
                            return None
                        
                        # Send the signed transaction
                        try:
                            response = self.client.send_raw_transaction(bytes(versioned_tx))
                            sig = str(response.value)
                            logger.info(f"✅ Sell transaction sent: {sig}")
                            return sig
                        except Exception as e:
                            logger.error(f"Failed to send transaction: {e}")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"PumpPortal API error ({response.status}): {error_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Failed to create sell transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
