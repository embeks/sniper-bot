"""
PumpPortal Trader - Use their API to get properly formatted transactions
FIXED: Use base64 decoding instead of base58 for PumpPortal responses
"""

import aiohttp
import base64
import logging
from typing import Optional
from solana.transaction import Transaction
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
                        # Parse JSON response
                        data = await response.json()
                        
                        if "transaction" not in data:
                            logger.error(f"No transaction in response: {data}")
                            return None
                        
                        # PumpPortal returns base64 encoded transaction
                        tx_base64 = data["transaction"]
                        logger.info(f"Received base64 transaction from PumpPortal (length: {len(tx_base64)})")
                        
                        # Decode base64 to bytes
                        try:
                            tx_bytes = base64.b64decode(tx_base64)
                        except Exception as e:
                            logger.error(f"Failed to decode base64 transaction: {e}")
                            return None
                        
                        # Deserialize transaction
                        try:
                            tx = Transaction.deserialize(tx_bytes)
                        except Exception as e:
                            logger.error(f"Failed to deserialize transaction: {e}")
                            return None
                        
                        # Sign with our keypair
                        try:
                            tx.sign(self.wallet.keypair)
                        except Exception as e:
                            logger.error(f"Failed to sign transaction: {e}")
                            return None
                        
                        # Send the signed transaction
                        logger.info(f"Sending signed transaction for {mint[:8]}...")
                        try:
                            response = self.client.send_raw_transaction(tx.serialize())
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
                        # Parse JSON response
                        data = await response.json()
                        
                        if "transaction" not in data:
                            logger.error(f"No transaction in response: {data}")
                            return None
                        
                        # PumpPortal returns base64 encoded transaction
                        tx_base64 = data["transaction"]
                        logger.info(f"Received base64 transaction from PumpPortal (length: {len(tx_base64)})")
                        
                        # Decode base64 to bytes
                        try:
                            tx_bytes = base64.b64decode(tx_base64)
                        except Exception as e:
                            logger.error(f"Failed to decode base64 transaction: {e}")
                            return None
                        
                        # Deserialize transaction
                        try:
                            tx = Transaction.deserialize(tx_bytes)
                        except Exception as e:
                            logger.error(f"Failed to deserialize transaction: {e}")
                            return None
                        
                        # Sign with our keypair
                        try:
                            tx.sign(self.wallet.keypair)
                        except Exception as e:
                            logger.error(f"Failed to sign transaction: {e}")
                            return None
                        
                        # Send the signed transaction
                        try:
                            response = self.client.send_raw_transaction(tx.serialize())
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
        
