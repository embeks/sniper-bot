"""
PumpPortal Trader
"""

import aiohttp
import base64
import json
import logging
from typing import Optional
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
            wallet_pubkey = str(self.wallet.pubkey)
            
            payload = {
                "publicKey": wallet_pubkey,
                "action": "buy",
                "mint": mint,
                "denominatedInSol": "true",
                "amount": sol_amount,
                "slippage": slippage,
                "priorityFee": 0.0001,
                "pool": "pump"
            }
            
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
                        response_text = await response.text()
                        try:
                            data = json.loads(response_text)
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
                        raw_tx_bytes = await response.read()
                        logger.info(f"Received raw binary transaction ({len(raw_tx_bytes)} bytes)")
                    else:
                        raw_tx_bytes = await response.read()
                        logger.warning(f"Unknown content-type: {content_type}, treating as raw bytes")
                    
                    # Validate transaction size
                    if len(raw_tx_bytes) < 100:
                        logger.error(f"Transaction too small: {len(raw_tx_bytes)} bytes")
                        return None
                    
                    # Check if it's a v0 transaction (544 bytes or high bit set)
                    is_v0 = len(raw_tx_bytes) == 544 or (raw_tx_bytes[0] & 0x80) != 0
                    
                    if is_v0:
                        logger.info(f"V0 transaction detected - needs signing")
                        try:
                            from solders.transaction import VersionedTransaction
                            
                            # Parse the unsigned v0 transaction to get the message
                            unsigned_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
                            message = unsigned_tx.message
                            
                            # Create a NEW VersionedTransaction with the message and keypair
                            # This constructor signs the message with your keypair
                            signed_tx = VersionedTransaction(message, [self.wallet.keypair])
                            signed_tx_bytes = bytes(signed_tx)
                            
                            logger.info(f"Created signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign v0 transaction: {e}")
                            return None
                    else:
                        # Legacy transactions (517 bytes for sells) - KEEP EXISTING WORKING CODE
                        logger.info(f"Legacy transaction ({len(raw_tx_bytes)} bytes)")
                        try:
                            from solana.transaction import Transaction
                            tx = Transaction.deserialize(raw_tx_bytes)
                            tx.sign_partial([self.wallet.keypair])
                            signed_tx_bytes = tx.serialize()
                            logger.info("Signed legacy transaction")
                        except Exception as e:
                            logger.error(f"Failed to sign legacy transaction: {e}")
                            # Try sending as-is - PumpPortal might have pre-signed it
                            logger.info("Attempting to send without signing...")
                            signed_tx_bytes = raw_tx_bytes
                    
                    logger.info(f"Sending transaction for {mint[:8]}...")
                    
                    # Send with retry logic
                    try:
                        opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                        response = self.client.send_raw_transaction(signed_tx_bytes, opts)
                        sig = str(response.value)
                        
                        if sig.startswith("1111111"):
                            logger.warning("Transaction failed - received invalid signature")
                            raise Exception("Invalid signature returned")
                        
                        logger.info(f"✅ Transaction sent successfully: {sig}")
                        return sig
                        
                    except Exception as e:
                        logger.warning(f"First send attempt failed: {e}")
                        
                        try:
                            response = self.client.send_raw_transaction(raw_tx_bytes)
                            sig = str(response.value)
                            
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
        token_amount: float,  # UI amount
        bonding_curve_key: str = None,
        slippage: int = 50,
        token_decimals: int = 6
    ) -> Optional[str]:
        """Get a sell transaction from PumpPortal API - expects UI token amounts"""
        try:
            wallet_pubkey = str(self.wallet.pubkey)
            ui_amount = float(token_amount)
            
            if ui_amount <= 0:
                logger.error(f"Invalid UI amount: {ui_amount}")
                return None
            
            # Handle decimals - it might be a tuple (decimals, source) or just an int
            if isinstance(token_decimals, tuple):
                token_decimals = token_decimals[0]  # Extract just the decimals value
            
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
                "denominatedInSol": "false",
                "amount": ui_amount,
                "slippage": slippage,
                "priorityFee": 0.0001,
                "pool": "pump",
                "tokenDecimals": token_decimals
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
                        return None
                    
                    content_type = response.headers.get('content-type', '')
                    
                    # Read response based on content type
                    if 'application/json' in content_type:
                        response_text = await response.text()
                        try:
                            data = json.loads(response_text)
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
                        raw_tx_bytes = await response.read()
                        logger.info(f"Received raw binary transaction ({len(raw_tx_bytes)} bytes)")
                    else:
                        raw_tx_bytes = await response.read()
                        logger.warning(f"Unknown content-type: {content_type}, treating as raw bytes")
                    
                    # Validate transaction size
                    if len(raw_tx_bytes) < 100:
                        logger.error(f"Transaction too small: {len(raw_tx_bytes)} bytes")
                        return None
                    
                    # Check if it's a v0 transaction (544 bytes or high bit set)
                    is_v0 = len(raw_tx_bytes) == 544 or (raw_tx_bytes[0] & 0x80) != 0
                    
                    if is_v0:
                        logger.info(f"V0 transaction detected - needs signing")
                        try:
                            from solders.transaction import VersionedTransaction
                            
                            # Parse the unsigned v0 transaction to get the message
                            unsigned_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
                            message = unsigned_tx.message
                            
                            # Create a NEW VersionedTransaction with the message and keypair
                            # This constructor signs the message with your keypair
                            signed_tx = VersionedTransaction(message, [self.wallet.keypair])
                            signed_tx_bytes = bytes(signed_tx)
                            
                            logger.info(f"Created signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign v0 transaction: {e}")
                            return None
                    else:
                        # Legacy transaction from PumpPortal. Always re-sign correctly:
                        # parse compact-u16 sig_count, extract message, sign, and repack.
                        logger.info("Legacy transaction - manual signing (robust varint parse + re-pack)")
                        try:
                            b = raw_tx_bytes
                            if len(b) < 2:
                                logger.error(f"Legacy tx too small ({len(b)} bytes)")
                                return None
                            
                            # --- parse compact-u16 signature count (little-endian varint) ---
                            idx = 0
                            val = 0
                            shift = 0
                            while True:
                                if idx >= len(b):
                                    logger.error("Bad legacy varint for sig_count")
                                    return None
                                byte = b[idx]
                                val |= (byte & 0x7F) << shift
                                idx += 1
                                if byte < 0x80:
                                    break
                                shift += 7
                                if shift > 14:  # guardrail
                                    logger.error("sig_count varint too long")
                                    return None
                            sig_count = val
                            sig_section_end = idx + 64 * sig_count
                            
                            if sig_section_end > len(b):
                                logger.error("Malformed legacy tx: signatures section extends past end")
                                return None
                            
                            # Message is everything after the signature section
                            msg_bytes = b[sig_section_end:]
                            if not msg_bytes:
                                logger.error("Empty legacy message bytes")
                                return None
                            
                            # Sign the message with our keypair
                            signature = self.wallet.keypair.sign_message(msg_bytes)  # 64 bytes
                            
                            # Repack: [sig_count=1 (0x01)] + [signature] + [message]
                            signed_tx_bytes = bytes([0x01]) + bytes(signature) + msg_bytes
                            
                            logger.info(f"Signed legacy transaction (len={len(signed_tx_bytes)})")
                        except Exception as e:
                            logger.error(f"Failed to sign legacy transaction (manual pack): {e}")
                            return None
                    
                    logger.info(f"Sending sell transaction for {mint[:8]}...")
                    
                    # Send with retry logic
                    try:
                        opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                        response = self.client.send_raw_transaction(signed_tx_bytes, opts)
                        sig = str(response.value)
                        
                        if sig.startswith("1111111"):
                            logger.warning("Transaction failed - received invalid signature")
                            raise Exception("Invalid signature returned")
                        
                        logger.info(f"✅ Sell transaction sent successfully: {sig}")
                        
                        # Quick, non-blocking confirmation check
                        try:
                            st = self.client.get_signature_statuses([sig]).value
                            ok = (st and st[0] and (st[0].confirmation_status in ("confirmed", "finalized")))
                            if not ok:
                                logger.warning(f"Sell not confirmed yet; status={st}")
                        except Exception as _e:
                            logger.debug(f"Status check skipped: {_e}")
                        
                        return sig
                        
                    except Exception as e:
                        logger.warning(f"First send attempt failed: {e}")
                        
                        try:
                            response = self.client.send_raw_transaction(raw_tx_bytes)
                            sig = str(response.value)
                            
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
