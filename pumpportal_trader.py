"""
PumpPortal Trader - Use their API to get properly formatted transactions
FIXED: Properly handles UI amounts for sells with tokenDecimals parameter
FIXED: Checks for invalid signatures (all 1's) and handles them properly
FIXED: Better handling of versioned vs legacy transactions
"""

import aiohttp
import base64
import json
import logging
from typing import Optional

# Import solders for versioned transactions
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair as SoldersKeypair

# Import solana-py for legacy transactions
from solana.transaction import Transaction
from solana.keypair import Keypair as SolanaKeypair
from solana.rpc.types import TxOpts

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
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"PumpPortal API error ({response.status}): {error_text}")
                        return None
                    
                    content_type = response.headers.get('content-type', '')
                    
                    # Read response based on content type
                    if 'application/json' in content_type:
                        # JSON response - decode base64 transaction
                        response_text = await response.text()
                        try:
                            data = json.loads(response_text)
                            # Check for transaction or signedTransaction field
                            tx_base64 = data.get("transaction") or data.get("signedTransaction")
                            if not tx_base64:
                                logger.error("No transaction field in JSON response")
                                return None
                            raw_tx_bytes = base64.b64decode(tx_base64)
                            logger.info(f"Decoded base64 transaction from JSON ({len(raw_tx_bytes)} bytes)")
                        except Exception as e:
                            logger.error(f"Failed to decode JSON response: {e}")
                            return None
                    elif 'application/octet-stream' in content_type:
                        # Binary response - use directly
                        raw_tx_bytes = await response.read()
                        logger.info(f"Received raw binary transaction ({len(raw_tx_bytes)} bytes)")
                    else:
                        # Unknown content type - try as raw bytes
                        raw_tx_bytes = await response.read()
                        logger.warning(f"Unknown content-type: {content_type}, treating as raw bytes")
                    
                    # Validate transaction size
                    if len(raw_tx_bytes) < 100:
                        logger.error(f"Transaction too small: {len(raw_tx_bytes)} bytes")
                        return None
                    
                    # Detect transaction type by examining first byte and size
                    # Versioned transactions have high bit set (0x80) or are exactly 544 bytes
                    is_versioned = (raw_tx_bytes[0] & 0x80) != 0 or len(raw_tx_bytes) == 544
                    
                    if is_versioned:
                        logger.info("Detected versioned (v0) transaction")
                        
                        try:
                            # Parse and sign versioned transaction using solders
                            vt = VersionedTransaction.from_bytes(raw_tx_bytes)
                            
                            # Sign with wallet keypair
                            vt.sign([self.wallet.keypair])
                            
                            # Get signed bytes
                            signed_tx_bytes = bytes(vt)
                            logger.info(f"Signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign v0 transaction: {e}")
                            return None
                    else:
                        logger.info("Detected legacy transaction")
                        
                        try:
                            # Parse and sign legacy transaction using solana-py
                            tx = Transaction.deserialize(raw_tx_bytes)
                            
                            # Create solana-py Keypair from wallet's solders keypair
                            secret_key = bytes(self.wallet.keypair.secret())
                            solana_keypair = SolanaKeypair.from_secret_key(secret_key)
                            
                            # Sign the transaction
                            tx.sign(solana_keypair)
                            
                            # Get signed bytes
                            signed_tx_bytes = tx.serialize()
                            logger.info(f"Signed legacy transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign legacy transaction: {e}")
                            return None
                    
                    # Send the signed transaction
                    logger.info(f"Sending signed transaction for {mint[:8]}...")
                    
                    # First attempt with options
                    try:
                        opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                        response = self.client.send_raw_transaction(signed_tx_bytes, opts)
                        sig = str(response.value)
                        
                        # Check for invalid signature (all 1's)
                        if sig.startswith("1111111"):
                            logger.warning("Transaction failed - received invalid signature")
                            raise Exception("Invalid signature returned")
                        
                        logger.info(f"✅ Transaction sent successfully: {sig}")
                        return sig
                        
                    except Exception as e:
                        logger.warning(f"First send attempt failed: {e}")
                        
                        # Retry without options
                        try:
                            response = self.client.send_raw_transaction(signed_tx_bytes)
                            sig = str(response.value)
                            
                            # Check for invalid signature
                            if sig.startswith("1111111"):
                                logger.error("Transaction failed - received invalid signature on retry")
                                return None
                            
                            logger.info(f"✅ Transaction sent on retry: {sig}")
                            return sig
                            
                        except Exception as e2:
                            logger.error(f"Both send attempts failed: {e2}")
                            return None
                        
        except Exception as e:
            logger.error(f"Failed to create buy transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    async def create_sell_transaction(
        self,
        mint: str,
        token_amount: float,  # Expects UI amount from main.py
        bonding_curve_key: str = None,
        slippage: int = 50,
        token_decimals: int = 6  # Default to 6 for PumpFun tokens
    ) -> Optional[str]:
        """Get a sell transaction from PumpPortal API - expects UI token amounts"""
        try:
            # Ensure publicKey matches our signing wallet
            wallet_pubkey = str(self.wallet.pubkey)
            
            # PumpPortal expects UI amounts when denominatedInSol is "false"
            ui_amount = float(token_amount)
            
            # Sanity check on UI amount
            if ui_amount <= 0:
                logger.error(f"Invalid UI amount: {ui_amount}")
                return None
            
            # Log for debugging
            logger.info(f"=== SELL TRANSACTION ===")
            logger.info(f"Token: {mint[:8]}...")
            logger.info(f"UI Amount: {ui_amount:.6f} tokens")
            logger.info(f"Decimals: {token_decimals}")
            logger.info(f"Verification - Raw atoms would be: {int(ui_amount * 10**token_decimals)}")
            logger.info(f"=======================")
            
            payload = {
                "publicKey": wallet_pubkey,
                "action": "sell",
                "mint": mint,
                "denominatedInSol": "false",  # CRITICAL: Must be string "false" for token amounts
                "amount": ui_amount,  # UI amount as float
                "slippage": slippage,
                "priorityFee": 0.0001,
                "pool": "pump",
                "tokenDecimals": token_decimals  # CRITICAL: Required for UI amount sells
            }
            
            if bonding_curve_key:
                payload["bondingCurveKey"] = bonding_curve_key
            
            logger.debug(f"Sell payload: {json.dumps(payload, indent=2)}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"PumpPortal API error ({response.status}): {error_text}")
                        if response.status == 400:
                            logger.error(f"Bad Request Details:")
                            logger.error(f"  - UI amount sent: {ui_amount:.6f}")
                            logger.error(f"  - Token decimals: {token_decimals}")
                            logger.error(f"  - denominatedInSol: false (string)")
                            logger.error(f"Check if token has migrated or if amount is valid")
                        return None
                    
                    content_type = response.headers.get('content-type', '')
                    
                    # Read response based on content type
                    if 'application/json' in content_type:
                        # JSON response - decode base64 transaction
                        response_text = await response.text()
                        try:
                            data = json.loads(response_text)
                            # Check for transaction or signedTransaction field
                            tx_base64 = data.get("transaction") or data.get("signedTransaction")
                            if not tx_base64:
                                logger.error("No transaction field in JSON response")
                                return None
                            raw_tx_bytes = base64.b64decode(tx_base64)
                            logger.info(f"Decoded base64 transaction from JSON ({len(raw_tx_bytes)} bytes)")
                        except Exception as e:
                            logger.error(f"Failed to decode JSON response: {e}")
                            return None
                    elif 'application/octet-stream' in content_type:
                        # Binary response - use directly
                        raw_tx_bytes = await response.read()
                        logger.info(f"Received raw binary transaction ({len(raw_tx_bytes)} bytes)")
                    else:
                        # Unknown content type - try as raw bytes
                        raw_tx_bytes = await response.read()
                        logger.warning(f"Unknown content-type: {content_type}, treating as raw bytes")
                    
                    # Debug: Log first few bytes
                    if len(raw_tx_bytes) > 0:
                        first_bytes = raw_tx_bytes[:10].hex() if len(raw_tx_bytes) >= 10 else raw_tx_bytes.hex()
                        logger.debug(f"First bytes of transaction: {first_bytes}")
                    
                    # Validate transaction size
                    if len(raw_tx_bytes) < 100:
                        logger.error(f"Transaction too small: {len(raw_tx_bytes)} bytes")
                        if len(raw_tx_bytes) > 0:
                            logger.error(f"Transaction content: {raw_tx_bytes.hex()[:200]}")
                        return None
                    
                    # Detect transaction type
                    is_versioned = (raw_tx_bytes[0] & 0x80) != 0 or len(raw_tx_bytes) == 544
                    
                    if is_versioned:
                        logger.info("Detected versioned (v0) transaction")
                        
                        try:
                            # Parse and sign versioned transaction using solders
                            vt = VersionedTransaction.from_bytes(raw_tx_bytes)
                            
                            # Sign with wallet keypair
                            vt.sign([self.wallet.keypair])
                            
                            # Get signed bytes
                            signed_tx_bytes = bytes(vt)
                            logger.info(f"Signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign v0 transaction: {e}")
                            return None
                    else:
                        logger.info("Detected legacy transaction")
                        
                        try:
                            # Parse and sign legacy transaction using solana-py
                            tx = Transaction.deserialize(raw_tx_bytes)
                            
                            # Create solana-py Keypair from wallet's solders keypair
                            secret_key = bytes(self.wallet.keypair.secret())
                            solana_keypair = SolanaKeypair.from_secret_key(secret_key)
                            
                            # Sign the transaction
                            tx.sign(solana_keypair)
                            
                            # Get signed bytes
                            signed_tx_bytes = tx.serialize()
                            logger.info(f"Signed legacy transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign legacy transaction: {e}")
                            return None
                    
                    # Send the signed transaction
                    logger.info(f"Sending signed sell transaction for {mint[:8]}...")
                    
                    # First attempt with options
                    try:
                        opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                        response = self.client.send_raw_transaction(signed_tx_bytes, opts)
                        sig = str(response.value)
                        
                        # Check for invalid signature (all 1's)
                        if sig.startswith("1111111"):
                            logger.warning("Transaction failed - received invalid signature")
                            raise Exception("Invalid signature returned")
                        
                        logger.info(f"✅ Sell transaction sent successfully: {sig}")
                        return sig
                        
                    except Exception as e:
                        logger.warning(f"First send attempt failed: {e}")
                        
                        # Retry without options
                        try:
                            response = self.client.send_raw_transaction(signed_tx_bytes)
                            sig = str(response.value)
                            
                            # Check for invalid signature
                            if sig.startswith("1111111"):
                                logger.error("Transaction failed - received invalid signature on retry")
                                return None
                            
                            logger.info(f"✅ Sell transaction sent on retry: {sig}")
                            return sig
                            
                        except Exception as e2:
                            logger.error(f"Both send attempts failed: {e2}")
                            return None
                        
        except Exception as e:
            logger.error(f"Failed to create sell transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
