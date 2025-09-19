"""
PumpPortal Trader - Use their API to get properly formatted transactions
"""

import aiohttp
import base64
import logging
from typing import Optional
from solana.transaction import Transaction
from solders.pubkey import Pubkey

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
        slippage: int = 10
    ) -> Optional[str]:
        """Get a buy transaction from PumpPortal API"""
        try:
            payload = {
                "publicKey": str(self.wallet.pubkey),
                "action": "buy",
                "mint": mint,
                "denominatedInSol": "true",
                "amount": sol_amount,
                "slippage": slippage,
                "priorityFee": 0.0001,  # Priority fee in SOL
                "pool": "pump"  # Use pump pool
            }
            
            # Add bonding curve if provided
            if bonding_curve_key:
                payload["bondingCurveKey"] = bonding_curve_key
            
            logger.info(f"Requesting buy transaction for {mint[:8]}... amount: {sol_amount} SOL")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # The API returns a base64 encoded transaction
                        if "transaction" in data:
                            tx_base64 = data["transaction"]
                            logger.info("Received transaction from PumpPortal")
                            
                            # Decode the transaction
                            tx_bytes = base64.b64decode(tx_base64)
                            
                            # Deserialize to Transaction object
                            tx = Transaction.deserialize(tx_bytes)
                            
                            # Sign with our keypair
                            tx.sign_partial(self.wallet.keypair)
                            
                            # Send the signed transaction
                            logger.info(f"Sending signed transaction for {mint[:8]}...")
                            response = self.client.send_raw_transaction(tx.serialize())
                            
                            sig = str(response.value)
                            logger.info(f"✅ Transaction sent: {sig}")
                            return sig
                        else:
                            logger.error(f"No transaction in response: {data}")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"PumpPortal API error ({response.status}): {error_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Failed to create buy transaction: {e}")
            return None
    
    async def create_sell_transaction(
        self,
        mint: str,
        token_amount: float,
        bonding_curve_key: str = None,
        slippage: int = 10
    ) -> Optional[str]:
        """Get a sell transaction from PumpPortal API"""
        try:
            payload = {
                "publicKey": str(self.wallet.pubkey),
                "action": "sell",
                "mint": mint,
                "denominatedInSol": "false",
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
                        data = await response.json()
                        
                        if "transaction" in data:
                            tx_base64 = data["transaction"]
                            tx_bytes = base64.b64decode(tx_base64)
                            tx = Transaction.deserialize(tx_bytes)
                            tx.sign_partial(self.wallet.keypair)
                            
                            response = self.client.send_raw_transaction(tx.serialize())
                            sig = str(response.value)
                            logger.info(f"✅ Sell transaction sent: {sig}")
                            return sig
                        else:
                            logger.error(f"No transaction in response: {data}")
                            return None
                    else:
                        error_text = await response.text()
                        logger.error(f"PumpPortal API error ({response.status}): {error_text}")
                        return None
                        
        except Exception as e:
            logger.error(f"Failed to create sell transaction: {e}")
            return None
