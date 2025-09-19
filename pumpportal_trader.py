"""
PumpPortal Trader - Use their API to get properly formatted transactions
FIXED: Proper base64 decoding and correct signing for v0/legacy transactions
"""

import aiohttp
import base64
import logging
from typing import Optional
from solders.keypair import Keypair as SoldersKeypair
from solders.transaction import VersionedTransaction
from solana.transaction import Transaction
from solana.keypair import Keypair as SolanaKeypair

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
            # Ensure publicKey matches our signing wallet
            wallet_pubkey = str(self.wallet.pubkey)
            
            payload = {
                "publicKey": wallet_pubkey,
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
            logger.debug(f"Using wallet: {wallet_pubkey}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status == 200:
                        # Parse JSON response
                        try:
                            data = await response.json()
                        except:
                            # If not JSON, read as text
                            text = await response.text()
                            data = {"transaction": text}
                        
                        if "transaction" not in data:
                            logger.error(f"No transaction in response: {data}")
                            return None
                        
                        # PumpPortal returns base64 encoded transaction
                        tx_base64 = data["transaction"]
                        logger.info(f"Received base64 transaction from PumpPortal (length: {len(tx_base64)})")
                        
                        # Decode base64 to raw bytes
                        try:
                            raw_tx_bytes = base64.b64decode(tx_base64)
                            logger.info(f"Decoded PumpPortal transaction ({len(raw_tx_bytes)} bytes)")
                        except Exception as e:
                            logger.error(f"Failed to base64 decode transaction: {e}")
                            return None
                        
                        # Detect transaction format by checking first byte
                        is_versioned = (raw_tx_bytes[0] & 0x80) != 0
                        
                        if is_versioned:
                            logger.info("Detected v0 versioned transaction")
                            
                            # Parse as VersionedTransaction using solders
                            try:
                                vt = VersionedTransaction.from_bytes(raw_tx_bytes)
                                
                                # Create signed transaction with our keypair
                                # For v0 transactions, we pass the keypair directly
                                signed_tx = VersionedTransaction(vt.message, [self.wallet.keypair])
                                signed_tx_bytes = bytes(signed_tx)
                                
                                logger.info(f"Signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                logger.error(f"Failed to sign v0 transaction: {e}")
                                return None
                        else:
                            logger.info("Detected legacy transaction")
                            
                            # Convert solders Keypair to solana-py Keypair for legacy signing
                            try:
                                # Get the secret key bytes from solders keypair
                                secret_bytes = bytes(self.wallet.keypair.secret())
                                
                                # Create solana-py Keypair
                                solana_keypair = SolanaKeypair(secret_bytes)
                                
                                # Deserialize and sign legacy transaction
                                tx = Transaction.deserialize(raw_tx_bytes)
                                tx.sign(solana_keypair)
                                signed_tx_bytes = tx.serialize()
                                
                                logger.info(f"Signed legacy transaction ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                logger.error(f"Failed to sign legacy transaction: {e}")
                                return None
                        
                        # Send the signed transaction
                        logger.info(f"Sending signed transaction for {mint[:8]}...")
                        try:
                            response = self.client.send_raw_transaction(signed_tx_bytes)
                            sig = str(response.value)
                            
                            if is_versioned:
                                logger.info(f"✅ v0 tx sent: {sig}")
                            else:
                                logger.info(f"✅ legacy tx sent: {sig}")
                            
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
            # Ensure publicKey matches our signing wallet
            wallet_pubkey = str(self.wallet.pubkey)
            
            payload = {
                "publicKey": wallet_pubkey,
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
            logger.debug(f"Using wallet: {wallet_pubkey}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status == 200:
                        # Parse JSON response
                        try:
                            data = await response.json()
                        except:
                            # If not JSON, read as text
                            text = await response.text()
                            data = {"transaction": text}
                        
                        if "transaction" not in data:
                            logger.error(f"No transaction in response: {data}")
                            return None
                        
                        # PumpPortal returns base64 encoded transaction
                        tx_base64 = data["transaction"]
                        logger.info(f"Received base64 transaction from PumpPortal (length: {len(tx_base64)})")
                        
                        # Decode base64 to raw bytes
                        try:
                            raw_tx_bytes = base64.b64decode(tx_base64)
                            logger.info(f"Decoded PumpPortal transaction ({len(raw_tx_bytes)} bytes)")
                        except Exception as e:
                            logger.error(f"Failed to base64 decode transaction: {e}")
                            return None
                        
                        # Detect transaction format by checking first byte
                        is_versioned = (raw_tx_bytes[0] & 0x80) != 0
                        
                        if is_versioned:
                            logger.info("Detected v0 versioned transaction")
                            
                            # Parse as VersionedTransaction using solders
                            try:
                                vt = VersionedTransaction.from_bytes(raw_tx_bytes)
                                
                                # Create signed transaction with our keypair
                                # For v0 transactions, we pass the keypair directly
                                signed_tx = VersionedTransaction(vt.message, [self.wallet.keypair])
                                signed_tx_bytes = bytes(signed_tx)
                                
                                logger.info(f"Signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                logger.error(f"Failed to sign v0 transaction: {e}")
                                return None
                        else:
                            logger.info("Detected legacy transaction")
                            
                            # Convert solders Keypair to solana-py Keypair for legacy signing
                            try:
                                # Get the secret key bytes from solders keypair
                                secret_bytes = bytes(self.wallet.keypair.secret())
                                
                                # Create solana-py Keypair
                                solana_keypair = SolanaKeypair(secret_bytes)
                                
                                # Deserialize and sign legacy transaction
                                tx = Transaction.deserialize(raw_tx_bytes)
                                tx.sign(solana_keypair)
                                signed_tx_bytes = tx.serialize()
                                
                                logger.info(f"Signed legacy transaction ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                logger.error(f"Failed to sign legacy transaction: {e}")
                                return None
                        
                        # Send the signed transaction
                        logger.info(f"Sending signed transaction for {mint[:8]}...")
                        try:
                            response = self.client.send_raw_transaction(signed_tx_bytes)
                            sig = str(response.value)
                            
                            if is_versioned:
                                logger.info(f"✅ v0 tx sent: {sig}")
                            else:
                                logger.info(f"✅ legacy tx sent: {sig}")
                            
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
