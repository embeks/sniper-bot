"""
PumpPortal Trader - Use their API to get properly formatted transactions
FIXED: Expects raw token amounts from main.py for sells
FIXED: Checks for invalid signatures (all 1's) and handles them properly
"""

import aiohttp
import base64
import logging
from typing import Optional
from solders.keypair import Keypair as SoldersKeypair
from solders.transaction import VersionedTransaction
from solana.transaction import Transaction
# For solana-py, Keypair is in solana.keypair
try:
    from solana.keypair import Keypair as SolanaKeypair
except ImportError:
    # Fallback to older solana-py structure
    try:
        from solana.account import Account as SolanaKeypair
    except ImportError:
        # Last resort - use solders for everything
        SolanaKeypair = None

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
                        content_type = response.headers.get('content-type', '')
                        
                        # Read raw bytes first
                        response_bytes = await response.read()
                        logger.info(f"Received response ({len(response_bytes)} bytes, type: {content_type})")
                        
                        # PumpPortal returns raw binary transactions with application/octet-stream
                        # These are NOT base64 encoded - they're the actual transaction bytes
                        if 'application/octet-stream' in content_type:
                            raw_tx_bytes = response_bytes  # Use directly!
                            logger.info("Using raw binary transaction from PumpPortal")
                        else:
                            # Only if JSON, try to parse and decode base64
                            try:
                                import json
                                data = json.loads(response_bytes.decode('utf-8'))
                                tx_base64 = data.get("transaction")
                                if tx_base64:
                                    raw_tx_bytes = base64.b64decode(tx_base64)
                                    logger.info("Decoded base64 transaction from JSON response")
                                else:
                                    logger.error("No transaction in JSON response")
                                    return None
                            except:
                                # If not JSON, assume raw bytes
                                raw_tx_bytes = response_bytes
                                logger.info("Using response as raw transaction bytes")
                        
                        logger.info(f"Processing transaction ({len(raw_tx_bytes)} bytes)")
                        
                        # Detect transaction format by checking first byte
                        # Versioned transactions start with 0x80 (for v0) or 0x00 (legacy marker followed by version)
                        # 544-byte transactions from PumpFun are versioned v0
                        is_versioned = len(raw_tx_bytes) == 544 or raw_tx_bytes[0] == 0x80
                        
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
                            
                            # For legacy transactions, we can use solders directly
                            try:
                                # Deserialize legacy transaction
                                tx = Transaction.deserialize(raw_tx_bytes)
                                
                                # Sign with solders keypair directly if solana-py Keypair not available
                                if SolanaKeypair is None:
                                    # Use solders for signing - sign the transaction's message
                                    tx.sign_partial([self.wallet.keypair])
                                else:
                                    # Convert solders Keypair to solana-py format
                                    secret_bytes = bytes(self.wallet.keypair.secret())
                                    solana_keypair = SolanaKeypair(secret_bytes)
                                    tx.sign(solana_keypair)
                                
                                signed_tx_bytes = tx.serialize()
                                logger.info(f"Signed legacy transaction ({len(signed_tx_bytes)} bytes)")
                            except Exception as e:
                                logger.error(f"Failed to sign legacy transaction: {e}")
                                return None
                        
                        # Send the signed transaction
                        logger.info(f"Sending signed transaction for {mint[:8]}...")
                        try:
                            # Try sending with skip_preflight to avoid blockhash issues
                            from solana.rpc.types import TxOpts
                            opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                            
                            response = self.client.send_raw_transaction(signed_tx_bytes, opts)
                            sig = str(response.value)
                            
                            # Check for failed signature (all 1's)
                            if sig.startswith("1111111"):
                                logger.warning(f"Transaction failed - received invalid signature")
                                return None
                            
                            if is_versioned:
                                logger.info(f"✅ v0 tx sent: {sig}")
                            else:
                                logger.info(f"✅ legacy tx sent: {sig}")
                            
                            return sig
                        except Exception as e:
                            logger.error(f"Failed to send transaction: {e}")
                            
                            # Try again without options
                            try:
                                response = self.client.send_raw_transaction(signed_tx_bytes)
                                sig = str(response.value)
                                
                                # Check for failed signature (all 1's)
                                if sig.startswith("1111111"):
                                    logger.warning(f"Transaction failed - received invalid signature")
                                    return None
                                
                                if is_versioned:
                                    logger.info(f"✅ v0 tx sent (retry): {sig}")
                                else:
                                    logger.info(f"✅ legacy tx sent (retry): {sig}")
                                
                                return sig
                            except Exception as e2:
                                logger.error(f"Retry also failed: {e2}")
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
        token_amount: float,  # Now expects UI amount from main.py
        bonding_curve_key: str = None,
        slippage: int = 50,
        token_decimals: int = 6  # Default to 6 for PumpFun tokens
    ) -> Optional[str]:
        """Get a sell transaction from PumpPortal API - expects UI token amounts"""
        try:
            # Ensure publicKey matches our signing wallet
            wallet_pubkey = str(self.wallet.pubkey)
            
            # PumpPortal expects UI amounts when denominatedInSol is "false"
            # The API handles decimal conversion internally
            ui_amount = float(token_amount)  # Ensure it's a float
            logger.info(f"Selling {ui_amount:.6f} UI tokens")
            
            payload = {
                "publicKey": wallet_pubkey,
                "action": "sell",
                "mint": mint,
                "denominatedInSol": "false",  # API expects string "false" not boolean
                "amount": ui_amount,  # PumpPortal expects UI amount as float
                "slippage": slippage,
                "priorityFee": 0.0001,
                "pool": "pump",
                "tokenDecimals": token_decimals  # Required for UI amount sells
            }
            
            if bonding_curve_key:
                payload["bondingCurveKey"] = bonding_curve_key
            
            logger.info(f"Requesting sell transaction for {mint[:8]}... amount: {ui_amount:.6f} UI tokens (decimals: {token_decimals})")
            logger.debug(f"Using wallet: {wallet_pubkey}")
            logger.debug(f"Payload amount field: {payload['amount']} (UI amount)")
            logger.debug(f"Token decimals: {token_decimals}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(self.api_url, json=payload) as response:
                    if response.status == 200:
                        content_type = response.headers.get('content-type', '')
                        
                        # Read raw bytes first
                        response_bytes = await response.read()
                        logger.info(f"Received response ({len(response_bytes)} bytes, type: {content_type})")
                        
                        # PumpPortal returns raw binary transactions with application/octet-stream
                        # These are NOT base64 encoded - they're the actual transaction bytes
                        if 'application/octet-stream' in content_type:
                            raw_tx_bytes = response_bytes  # Use directly!
                            logger.info("Using raw binary transaction from PumpPortal")
                        else:
                            # Only if JSON, try to parse and decode base64
                            try:
                                import json
                                data = json.loads(response_bytes.decode('utf-8'))
                                tx_base64 = data.get("transaction")
                                if tx_base64:
                                    raw_tx_bytes = base64.b64decode(tx_base64)
                                    logger.info("Decoded base64 transaction from JSON response")
                                else:
                                    logger.error("No transaction in JSON response")
                                    return None
                            except:
                                # If not JSON, assume raw bytes
                                raw_tx_bytes = response_bytes
                                logger.info("Using response as raw transaction bytes")
                        
                        logger.info(f"Processing transaction ({len(raw_tx_bytes)} bytes)")
                        
                        # Detect transaction format by checking first byte
                        # Versioned transactions start with 0x80 (for v0) or 0x00 (legacy marker followed by version)
                        # 544-byte transactions from PumpFun are versioned v0
                        is_versioned = len(raw_tx_bytes) == 544 or raw_tx_bytes[0] == 0x80
                        
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
                            
                            # For legacy transactions, try multiple approaches
                            signed_tx_bytes = None
                            try:
                                # For sell transactions, PumpPortal might return pre-signed tx
                                # Try to send directly first
                                logger.info(f"Attempting to send pre-signed transaction for {mint[:8]}...")
                                
                                from solana.rpc.types import TxOpts
                                opts = TxOpts(skip_preflight=True, preflight_commitment="processed", max_retries=3)
                                
                                try:
                                    # Try sending raw bytes directly (might be pre-signed)
                                    response = self.client.send_raw_transaction(raw_tx_bytes, opts)
                                    sig = str(response.value)
                                    
                                    # Check for failed signature (all 1's)
                                    if sig.startswith("1111111"):
                                        logger.warning(f"Transaction failed - received invalid signature")
                                        raise Exception("Invalid signature returned")
                                    
                                    logger.info(f"✅ Pre-signed sell tx sent: {sig}")
                                    return sig
                                except Exception as direct_send_error:
                                    logger.info(f"Not pre-signed or failed, will sign ourselves: {direct_send_error}")
                                
                                # If direct send failed, try to deserialize and sign
                                try:
                                    tx = Transaction.deserialize(raw_tx_bytes)
                                    
                                    # Sign with solders keypair directly if solana-py Keypair not available
                                    if SolanaKeypair is None:
                                        # Use solders for signing - sign the transaction's message
                                        tx.sign_partial([self.wallet.keypair])
                                    else:
                                        # Convert solders Keypair to solana-py format
                                        secret_bytes = bytes(self.wallet.keypair.secret())
                                        solana_keypair = SolanaKeypair(secret_bytes)
                                        tx.sign(solana_keypair)
                                    
                                    signed_tx_bytes = tx.serialize()
                                    logger.info(f"Signed legacy transaction ({len(signed_tx_bytes)} bytes)")
                                    
                                except Exception as deser_error:
                                    logger.warning(f"Standard deserialization failed: {deser_error}")
                                    # Last resort - try sending original bytes
                                    logger.info("Last resort: sending original transaction bytes")
                                    try:
                                        response = self.client.send_raw_transaction(raw_tx_bytes, opts)
                                        sig = str(response.value)
                                        
                                        # Check for failed signature (all 1's)
                                        if sig.startswith("1111111"):
                                            logger.warning(f"Transaction failed - received invalid signature")
                                            return None
                                        
                                        logger.info(f"✅ Original bytes sent successfully: {sig}")
                                        return sig
                                    except Exception as final_error:
                                        logger.error(f"All methods failed: {final_error}")
                                        return None
                                        
                            except Exception as e:
                                logger.error(f"Failed to process legacy transaction: {e}")
                                return None
                        
                        # Send the signed transaction if we have it
                        if signed_tx_bytes:
                            logger.info(f"Sending signed transaction for {mint[:8]}...")
                            try:
                                # Try sending with skip_preflight to avoid blockhash issues
                                from solana.rpc.types import TxOpts
                                opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                                
                                response = self.client.send_raw_transaction(signed_tx_bytes, opts)
                                sig = str(response.value)
                                
                                # Check for failed signature (all 1's)
                                if sig.startswith("1111111"):
                                    logger.warning(f"Transaction failed - received invalid signature")
                                    return None
                                
                                if is_versioned:
                                    logger.info(f"✅ v0 tx sent: {sig}")
                                else:
                                    logger.info(f"✅ legacy tx sent: {sig}")
                                
                                return sig
                            except Exception as e:
                                logger.error(f"Failed to send transaction: {e}")
                                
                                # Try again without options
                                try:
                                    response = self.client.send_raw_transaction(signed_tx_bytes)
                                    sig = str(response.value)
                                    
                                    # Check for failed signature (all 1's)
                                    if sig.startswith("1111111"):
                                        logger.warning(f"Transaction failed - received invalid signature")
                                        return None
                                    
                                    if is_versioned:
                                        logger.info(f"✅ v0 tx sent (retry): {sig}")
                                    else:
                                        logger.info(f"✅ legacy tx sent (retry): {sig}")
                                    
                                    return sig
                                except Exception as e2:
                                    logger.error(f"Retry also failed: {e2}")
                                    return None
                        else:
                            # We don't have signed_tx_bytes, transaction was already sent
                            return None
                            
                    else:
                        error_text = await response.text()
                        logger.error(f"PumpPortal API error ({response.status}): {error_text}")
                        # Log additional debugging info for 400 errors
                        if response.status == 400:
                            logger.error(f"Bad Request - check if amount {ui_amount:.6f} is valid")
                            logger.error(f"UI amount sent: {ui_amount:.6f}")
                            logger.error(f"This should be the UI token amount (human-readable)")
                        return None
                        
        except Exception as e:
            logger.error(f"Failed to create sell transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
