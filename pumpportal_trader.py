"""
PumpPortal Trader - Use their API to get properly formatted transactions
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
                "denominatedInSol": True,  # Boolean, not string
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
                        # Handle different response types
                        content_type = response.headers.get('content-type', '')
                        
                        if 'application/json' in content_type:
                            data = await response.json()
                        else:
                            # If not JSON, try to parse as text first
                            text_data = await response.text()
                            try:
                                import json
                                data = json.loads(text_data)
                            except:
                                # If that fails, assume it's the transaction directly
                                data = {"transaction": text_data}
                        
                        # The API returns a base58 encoded VersionedTransaction
                        if "transaction" in data:
                            tx_base58 = data["transaction"]
                            logger.info("Received transaction from PumpPortal")
                            
                            # Decode from base58
                            tx_bytes = base58.b58decode(tx_base58)
                            
                            # Construct VersionedTransaction
                            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                            
                            # Sign with our keypair
                            versioned_tx.sign([self.wallet.keypair])
                            
                            # Send the signed transaction
                            logger.info(f"Sending signed transaction for {mint[:8]}...")
                            response = self.client.send_raw_transaction(bytes(versioned_tx))
                            
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
        slippage: int = 50
    ) -> Optional[str]:
        """Get a sell transaction from PumpPortal API"""
        try:
            payload = {
                "publicKey": str(self.wallet.pubkey),
                "action": "sell",
                "mint": mint,
                "denominatedInSol": False,  # Boolean, not string
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
                        # Handle different response types
                        content_type = response.headers.get('content-type', '')
                        
                        if 'application/json' in content_type:
                            data = await response.json()
                        else:
                            # If not JSON, try to parse as text first
                            text_data = await response.text()
                            try:
                                import json
                                data = json.loads(text_data)
                            except:
                                # If that fails, assume it's the transaction directly
                                data = {"transaction": text_data}
                        
                        if "transaction" in data:
                            tx_base58 = data["transaction"]
                            
                            # Decode from base58
                            tx_bytes = base58.b58decode(tx_base58)
                            
                            # Construct VersionedTransaction
                            versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                            
                            # Sign with our keypair
                            versioned_tx.sign([self.wallet.keypair])
                            
                            # Send the signed transaction
                            response = self.client.send_raw_transaction(bytes(versioned_tx))
                            
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
