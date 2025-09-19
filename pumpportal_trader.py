"""
PumpPortal Trader - Use their API to get properly formatted transactions
FIXED: Handle application/octet-stream responses (raw binary transactions)
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
                        content_type = response.headers.get('content-type', '')
                        
                        # Read raw bytes
                        response_bytes = await response.read()
                        logger.info(f"Received response ({len(response_bytes)} bytes, type: {content_type})")
                        
                        tx_bytes = None
                        
                        # Handle different response types
                        if 'application/json' in content_type:
                            # JSON response with base64 transaction
                            import json
                            data = json.loads(response_bytes.decode('utf-8'))
                            if "transaction" in data:
                                # Decode base64
                                tx_bytes = base64.b64decode(data["transaction"])
                            else:
                                logger.error(f"No transaction in JSON response: {data}")
                                return None
                        elif 'application/octet-stream' in content_type or 'binary' in content_type:
                            # Raw binary transaction
                            tx_bytes = response_bytes
                        else:
                            # Try to detect format
                            try:
                                # Try JSON first
                                import json
                                data = json.loads(response_bytes.decode('utf-8'))
                                if "transaction" in data:
                                    tx_bytes = base64.b64decode(data["transaction"])
                            except:
                                # Assume raw bytes
                                tx_bytes = response_bytes
                        
                        if not tx_bytes:
                            logger.error("No transaction data extracted from response")
                            return None
                        
                        logger.info(f"Processing transaction ({len(tx_bytes)} bytes)")
                        
                        # Try different transaction formats
                        signed_tx_bytes = None
                        
                        # Check if this is a versioned transaction (starts with 0x80 or has length 544)
                        is_versioned = tx_bytes[0] == 0x80 or len(tx_bytes) == 544
                        
                        if is_versioned:
                            try:
                                from solders.transaction import VersionedTransaction
                                
                                # Parse the versioned transaction
                                versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                                
                                # Get the message to sign
                                message_bytes = bytes(versioned_tx.message)
                                
                                # Sign the message
                                signature = self.wallet.keypair.sign_message(message_bytes)
                                
                                # The transaction should already have placeholder signatures
                                # We need to properly construct a new VersionedTransaction
                                # with our signature in the correct position
                                
                                # Get the number of signatures required
                                num_signatures = len(versioned_tx.signatures)
                                
                                # Create new signatures list with our signature first
                                new_signatures = [signature]
                                # Add any other signatures (likely empty placeholders)
                                for i in range(1, num_signatures):
                                    new_signatures.append(versioned_tx.signatures[i])
                                
                                # Create properly signed transaction
                                signed_tx = VersionedTransaction(versioned_tx.message, new_signatures)
                                signed_tx_bytes = bytes(signed_tx)
                                
                                logger.info(f"Successfully signed VersionedTransaction ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                logger.error(f"Failed to sign as VersionedTransaction: {e}")
                                # Try manual byte manipulation as last resort
                                try:
                                    # Sign the raw message
                                    message_start = 1 + (64 * 2)  # version + 2 signatures typically
                                    message_bytes = tx_bytes[message_start:]
                                    signature = self.wallet.keypair.sign_message(message_bytes)
                                    
                                    # Replace first signature slot (after version byte)
                                    signed_tx_bytes = tx_bytes[:1] + bytes(signature) + tx_bytes[65:]
                                    logger.info("Signed using byte manipulation")
                                except Exception as e2:
                                    logger.error(f"Byte manipulation also failed: {e2}")
                        
                        # If not versioned or versioned failed, try legacy
                        if signed_tx_bytes is None:
                            try:
                                tx = Transaction.deserialize(tx_bytes)
                                tx.sign(self.wallet.keypair)
                                signed_tx_bytes = tx.serialize()
                                logger.info("Successfully processed as Legacy Transaction")
                            except Exception as e:
                                logger.error(f"Legacy transaction also failed: {e}")
                                logger.debug(f"First 20 bytes (hex): {tx_bytes[:20].hex()}")
                                logger.debug(f"Last 20 bytes (hex): {tx_bytes[-20:].hex()}")
                                return None
                        
                        if not signed_tx_bytes:
                            logger.error("Failed to sign transaction")
                            return None
                        
                        # Send the signed transaction
                        logger.info(f"Sending signed transaction for {mint[:8]}...")
                        try:
                            response = self.client.send_raw_transaction(signed_tx_bytes)
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
                        content_type = response.headers.get('content-type', '')
                        
                        # Read raw bytes
                        response_bytes = await response.read()
                        logger.info(f"Received response ({len(response_bytes)} bytes, type: {content_type})")
                        
                        tx_bytes = None
                        
                        # Handle different response types
                        if 'application/json' in content_type:
                            # JSON response with base64 transaction
                            import json
                            data = json.loads(response_bytes.decode('utf-8'))
                            if "transaction" in data:
                                # Decode base64
                                tx_bytes = base64.b64decode(data["transaction"])
                            else:
                                logger.error(f"No transaction in JSON response: {data}")
                                return None
                        elif 'application/octet-stream' in content_type or 'binary' in content_type:
                            # Raw binary transaction
                            tx_bytes = response_bytes
                        else:
                            # Try to detect format
                            try:
                                # Try JSON first
                                import json
                                data = json.loads(response_bytes.decode('utf-8'))
                                if "transaction" in data:
                                    tx_bytes = base64.b64decode(data["transaction"])
                            except:
                                # Assume raw bytes
                                tx_bytes = response_bytes
                        
                        if not tx_bytes:
                            logger.error("No transaction data extracted from response")
                            return None
                        
                        logger.info(f"Processing transaction ({len(tx_bytes)} bytes)")
                        
                        # Try different transaction formats
                        signed_tx_bytes = None
                        
                        # Check if this is a versioned transaction (starts with 0x80 or has length 544)
                        is_versioned = tx_bytes[0] == 0x80 or len(tx_bytes) == 544
                        
                        if is_versioned:
                            try:
                                from solders.transaction import VersionedTransaction
                                
                                # Parse the versioned transaction
                                versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                                
                                # Get the message to sign
                                message_bytes = bytes(versioned_tx.message)
                                
                                # Sign the message
                                signature = self.wallet.keypair.sign_message(message_bytes)
                                
                                # The transaction should already have placeholder signatures
                                # We need to properly construct a new VersionedTransaction
                                # with our signature in the correct position
                                
                                # Get the number of signatures required
                                num_signatures = len(versioned_tx.signatures)
                                
                                # Create new signatures list with our signature first
                                new_signatures = [signature]
                                # Add any other signatures (likely empty placeholders)
                                for i in range(1, num_signatures):
                                    new_signatures.append(versioned_tx.signatures[i])
                                
                                # Create properly signed transaction
                                signed_tx = VersionedTransaction(versioned_tx.message, new_signatures)
                                signed_tx_bytes = bytes(signed_tx)
                                
                                logger.info(f"Successfully signed VersionedTransaction ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                logger.error(f"Failed to sign as VersionedTransaction: {e}")
                                # Try manual byte manipulation as last resort
                                try:
                                    # Sign the raw message
                                    message_start = 1 + (64 * 2)  # version + 2 signatures typically
                                    message_bytes = tx_bytes[message_start:]
                                    signature = self.wallet.keypair.sign_message(message_bytes)
                                    
                                    # Replace first signature slot (after version byte)
                                    signed_tx_bytes = tx_bytes[:1] + bytes(signature) + tx_bytes[65:]
                                    logger.info("Signed using byte manipulation")
                                except Exception as e2:
                                    logger.error(f"Byte manipulation also failed: {e2}")
                        
                        # If not versioned or versioned failed, try legacy
                        if signed_tx_bytes is None:
                            try:
                                tx = Transaction.deserialize(tx_bytes)
                                tx.sign(self.wallet.keypair)
                                signed_tx_bytes = tx.serialize()
                                logger.info("Successfully processed as Legacy Transaction")
                            except Exception as e:
                                logger.error(f"Legacy transaction also failed: {e}")
                                logger.debug(f"First 20 bytes (hex): {tx_bytes[:20].hex()}")
                                logger.debug(f"Last 20 bytes (hex): {tx_bytes[-20:].hex()}")
                                return None
                        
                        if not signed_tx_bytes:
                            logger.error("Failed to sign transaction")
                            return None
                        
                        # Send the signed transaction
                        try:
                            response = self.client.send_raw_transaction(signed_tx_bytes)
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
