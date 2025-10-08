"""
PumpPortal Trader
WITH CRITICAL FIX: Dynamic priority fees + Accurate SOL tracking from blockchain
"""

import aiohttp
import asyncio
import base64
import json
import logging
import time
from typing import Optional
from solana.rpc.types import TxOpts

logger = logging.getLogger(__name__)

class PumpPortalTrader:
    """Use PumpPortal's API for transaction creation with dynamic fees"""
    
    def __init__(self, wallet_manager, client):
        self.wallet = wallet_manager
        self.client = client
        self.api_url = "https://pumpportal.fun/api/trade-local"
    
    async def get_priority_fee(self, urgency: str = "normal") -> float:
        """
        Get priority fee based on urgency
        
        urgency levels:
        - "low": 0.0001 SOL (5x target, price still climbing)
        - "normal": 0.0005 SOL (2x/3x targets, regular buys)
        - "high": 0.001 SOL (stop-loss, early dump, no volume)
        - "critical": 0.002 SOL (retries, must execute immediately)
        """
        urgency_fees = {
            "low": 0.0001,
            "normal": 0.0005,
            "high": 0.001,
            "critical": 0.002
        }
        
        fee = urgency_fees.get(urgency, 0.0005)
        logger.debug(f"Priority fee ({urgency}): {fee:.6f} SOL")
        return fee
    
    async def get_sol_received_from_sell(self, signature: str, timeout: int = 30) -> float:
        """
        CRITICAL FIX: Fetch actual SOL received from a sell transaction
        Returns the actual SOL amount received from blockchain, not estimated
        
        This solves the P&L accuracy problem where estimates don't account for:
        - Actual slippage during execution
        - Bonding curve price movement
        - PumpPortal's actual fees
        """
        try:
            start = time.time()
            
            logger.debug(f"Fetching actual SOL received from tx {signature[:8]}...")
            
            # Wait for transaction to confirm and fetch details
            while time.time() - start < timeout:
                try:
                    tx = self.client.get_transaction(
                        signature,
                        encoding="jsonParsed",
                        max_supported_transaction_version=0
                    )
                    
                    if not tx or not tx.value:
                        await asyncio.sleep(1)
                        continue
                    
                    # Transaction found! Parse it to get actual SOL received
                    meta = tx.value.transaction.meta
                    
                    if not meta:
                        logger.warning("Transaction has no meta data")
                        await asyncio.sleep(1)
                        continue
                    
                    # Get pre and post balances
                    pre_balances = meta.pre_balances
                    post_balances = meta.post_balances
                    
                    if not pre_balances or not post_balances:
                        logger.warning("Transaction missing balance data")
                        await asyncio.sleep(1)
                        continue
                    
                    # Your wallet is the signer (account index 0)
                    if len(pre_balances) > 0 and len(post_balances) > 0:
                        pre_sol_lamports = pre_balances[0]
                        post_sol_lamports = post_balances[0]
                        
                        # Calculate actual SOL gained (post - pre)
                        sol_gained_lamports = post_sol_lamports - pre_sol_lamports
                        sol_gained = sol_gained_lamports / 1e9
                        
                        # SOL gained should be positive for a sell
                        if sol_gained > 0:
                            logger.info(f"✅ Actual SOL received from blockchain: {sol_gained:.6f} SOL")
                            return sol_gained
                        else:
                            # This might happen if we're checking too early
                            logger.debug(f"SOL balance change: {sol_gained:.6f} (negative or zero)")
                            await asyncio.sleep(1)
                            continue
                    
                except Exception as e:
                    logger.debug(f"Error fetching transaction details: {e}")
                    await asyncio.sleep(1)
            
            logger.warning(f"⏱️ Timeout ({timeout}s) fetching actual SOL from tx")
            return 0.0
            
        except Exception as e:
            logger.error(f"Failed to get actual SOL received: {e}")
            return 0.0
    
    async def create_buy_transaction(
        self, 
        mint: str, 
        sol_amount: float,
        bonding_curve_key: str = None,
        slippage: int = 50
    ) -> Optional[str]:
        """Get a buy transaction from PumpPortal API - FIXED: removed urgency param"""
        try:
            wallet_pubkey = str(self.wallet.pubkey)
            
            # Use normal priority for buys
            priority_fee = await self.get_priority_fee("normal")
            
            payload = {
                "publicKey": wallet_pubkey,
                "action": "buy",
                "mint": mint,
                "denominatedInSol": "true",
                "amount": sol_amount,
                "slippage": slippage,
                "priorityFee": priority_fee,
                "pool": "pump"
            }
            
            if bonding_curve_key:
                payload["bondingCurveKey"] = bonding_curve_key
            
            logger.info(f"Requesting buy transaction for {mint[:8]}... amount: {sol_amount} SOL")
            logger.info(f"Priority fee: {priority_fee:.6f} SOL (normal)")
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
                            signed_tx = VersionedTransaction(message, [self.wallet.keypair])
                            signed_tx_bytes = bytes(signed_tx)
                            
                            logger.info(f"Created signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign v0 transaction: {e}")
                            return None
                    else:
                        # Legacy transactions (517 bytes for sells)
                        logger.info(f"Legacy transaction ({len(raw_tx_bytes)} bytes)")
                        try:
                            from solana.transaction import Transaction
                            tx = Transaction.deserialize(raw_tx_bytes)
                            tx.sign_partial([self.wallet.keypair])
                            signed_tx_bytes = tx.serialize()
                            logger.info("Signed legacy transaction")
                        except Exception as e:
                            logger.error(f"Failed to sign legacy transaction: {e}")
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
        token_amount: float,
        slippage: int = 50,
        token_decimals: int = 6
    ) -> Optional[str]:
        """Get a sell transaction from PumpPortal API - FIXED: removed urgency param"""
        try:
            wallet_pubkey = str(self.wallet.pubkey)
            ui_amount = float(token_amount)
            
            if ui_amount <= 0:
                logger.error(f"Invalid UI amount: {ui_amount}")
                return None
            
            # Handle decimals - it might be a tuple (decimals, source) or just an int
            if isinstance(token_decimals, tuple):
                token_decimals = token_decimals[0]
            
            # Use normal priority for sells
            priority_fee = await self.get_priority_fee("normal")
            
            logger.info(f"=== SELL TRANSACTION ===")
            logger.info(f"Token: {mint[:8]}...")
            logger.info(f"UI Amount: {ui_amount:.6f} tokens")
            logger.info(f"Decimals: {token_decimals}")
            logger.info(f"Priority fee: {priority_fee:.6f} SOL (normal)")
            logger.info(f"Verification - Raw atoms would be: {int(ui_amount * 10**token_decimals)}")
            logger.info(f"=======================")
            
            payload = {
                "publicKey": wallet_pubkey,
                "action": "sell",
                "mint": mint,
                "denominatedInSol": "false",
                "amount": ui_amount,
                "slippage": slippage,
                "priorityFee": priority_fee,
                "pool": "pump",
                "tokenDecimals": token_decimals
            }
            
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
                    
                    # Check if it's a v0 transaction
                    is_v0 = len(raw_tx_bytes) == 544 or (raw_tx_bytes[0] & 0x80) != 0
                    
                    if is_v0:
                        logger.info(f"V0 transaction detected - needs signing")
                        try:
                            from solders.transaction import VersionedTransaction
                            
                            unsigned_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
                            message = unsigned_tx.message
                            signed_tx = VersionedTransaction(message, [self.wallet.keypair])
                            signed_tx_bytes = bytes(signed_tx)
                            
                            logger.info(f"Created signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign v0 transaction: {e}")
                            return None
                    else:
                        # Legacy transaction - manual signing with robust varint parse
                        logger.info("Legacy transaction - manual signing (robust varint parse + re-pack)")
                        try:
                            b = raw_tx_bytes
                            if len(b) < 2:
                                logger.error(f"Legacy tx too small ({len(b)} bytes)")
                                return None
                            
                            # Parse compact-u16 signature count
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
                                if shift > 14:
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
                            
                            # Sign the message
                            signature = self.wallet.keypair.sign_message(msg_bytes)
                            
                            # Repack: [sig_count=1] + [signature] + [message]
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
