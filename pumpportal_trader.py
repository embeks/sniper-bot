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
                        
                        # Debug: Log the first and last bytes to understand the structure
                        logger.debug(f"First 10 bytes (hex): {tx_bytes[:10].hex()}")
                        logger.debug(f"Last 10 bytes (hex): {tx_bytes[-10:].hex()}")
                        
                        # Check what we're dealing with
                        if tx_bytes[0] == 0x01:
                            logger.info("Detected legacy transaction format (starts with 0x01)")
                        elif tx_bytes[0] == 0x80:
                            logger.info("Detected versioned transaction v0 (starts with 0x80)")
                        else:
                            logger.info(f"Unknown transaction format (starts with 0x{tx_bytes[0]:02x})")
                        
                        # Try different transaction formats
                        signed_tx_bytes = None
                        
                        # For 544-byte responses from PumpPortal, these are likely versioned transactions
                        if len(tx_bytes) == 544:
                            try:
                                # These might already be fully formed transactions that just need our signature
                                # inserted at the right position
                                
                                # The transaction structure for v0 is:
                                # [0x80 version][compact array of signatures][message]
                                # Let's check if this is already a valid structure
                                
                                # Sign the message portion
                                # For v0 transactions, after version byte and signature array
                                version = tx_bytes[0]
                                if version == 0x80:
                                    # Next byte is the number of signatures
                                    num_sigs = tx_bytes[1]
                                    sig_start = 2
                                    msg_start = sig_start + (64 * num_sigs)
                                    
                                    # Extract and sign the message
                                    message_bytes = tx_bytes[msg_start:]
                                    signature = self.wallet.keypair.sign_message(message_bytes)
                                    
                                    # Insert our signature in the first slot
                                    signed_tx_bytes = (
                                        tx_bytes[0:2] +  # version + num_sigs
                                        bytes(signature) +  # our signature (64 bytes)
                                        tx_bytes[sig_start + 64:]  # skip first empty sig, keep rest
                                    )
                                    logger.info(f"Manually signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                                else:
                                    logger.warning(f"544-byte transaction doesn't start with 0x80: {version:02x}")
                            except Exception as e:
                                logger.error(f"Manual signing failed: {e}")
                        
                        # If manual approach didn't work, try parsing with libraries
                        if signed_tx_bytes is None:
                            try:
                                from solders.transaction import VersionedTransaction
                                from solders.message import MessageV0
                                
                                # Parse the versioned transaction
                                versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                                
                                # Simply use populate method to add our signature
                                # This is the proper way to sign a partially signed transaction
                                versioned_tx.populate(self.wallet.keypair)
                                signed_tx_bytes = bytes(versioned_tx)
                                
                                logger.info(f"Successfully signed VersionedTransaction with populate ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                # If populate doesn't work, try manual signing
                                try:
                                    from solders.transaction import VersionedTransaction
                                    
                                    # Parse the versioned transaction again
                                    versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                                    
                                    # Get the actual message bytes for signing
                                    message_bytes = bytes(versioned_tx.message)
                                    
                                    # Sign the message
                                    signature = self.wallet.keypair.sign_message(message_bytes)
                                    
                                    # Manually reconstruct: version byte + signature + rest of transaction
                                    # The transaction structure is: [version_byte][signatures][message]
                                    if tx_bytes[0] == 0x80:
                                        # Has version byte
                                        # Find how many signatures there are (compact array)
                                        num_sigs = tx_bytes[1]
                                        sig_start = 2
                                        sig_end = sig_start + (64 * num_sigs)
                                        
                                        # Replace first signature
                                        signed_tx_bytes = (
                                            tx_bytes[0:1] +  # version byte
                                            tx_bytes[1:2] +  # num signatures
                                            bytes(signature) +  # our signature
                                            tx_bytes[sig_start + 64:] # rest of transaction
                                        )
                                    else:
                                        # Legacy format - shouldn't happen for PumpFun
                                        signed_tx_bytes = None
                                    
                                    if signed_tx_bytes:
                                        logger.info("Signed using manual reconstruction")
                                except Exception as e2:
                                    logger.error(f"Manual signing also failed: {e2}")
                        
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
                                from solders.message import MessageV0
                                
                                # Parse the versioned transaction
                                versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                                
                                # Simply use populate method to add our signature
                                # This is the proper way to sign a partially signed transaction
                                versioned_tx.populate(self.wallet.keypair)
                                signed_tx_bytes = bytes(versioned_tx)
                                
                                logger.info(f"Successfully signed VersionedTransaction with populate ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                # If populate doesn't work, try manual signing
                                try:
                                    from solders.transaction import VersionedTransaction
                                    
                                    # Parse the versioned transaction again
                                    versioned_tx = VersionedTransaction.from_bytes(tx_bytes)
                                    
                                    # Get the actual message bytes for signing
                                    message_bytes = bytes(versioned_tx.message)
                                    
                                    # Sign the message
                                    signature = self.wallet.keypair.sign_message(message_bytes)
                                    
                                    # Manually reconstruct: version byte + signature + rest of transaction
                                    # The transaction structure is: [version_byte][signatures][message]
                                    if tx_bytes[0] == 0x80:
                                        # Has version byte
                                        # Find how many signatures there are (compact array)
                                        num_sigs = tx_bytes[1]
                                        sig_start = 2
                                        sig_end = sig_start + (64 * num_sigs)
                                        
                                        # Replace first signature
                                        signed_tx_bytes = (
                                            tx_bytes[0:1] +  # version byte
                                            tx_bytes[1:2] +  # num signatures
                                            bytes(signature) +  # our signature
                                            tx_bytes[sig_start + 64:] # rest of transaction
                                        )
                                    else:
                                        # Legacy format - shouldn't happen for PumpFun
                                        signed_tx_bytes = None
                                    
                                    if signed_tx_bytes:
                                        logger.info("Signed using manual reconstruction")
                                except Exception as e2:
                                    logger.error(f"Manual signing also failed: {e2}")
                        
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
