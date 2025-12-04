"""
PumpPortal Trader
UPDATED: Tighter slippage now that we validate liquidity first
"""

import aiohttp
import asyncio
import base64
import json
import logging
import random
from typing import Optional
from solana.rpc.types import TxOpts

logger = logging.getLogger(__name__)

class PumpPortalTrader:
    """Use PumpPortal's API for transaction creation with dynamic fees"""
    
    def __init__(self, wallet_manager, client):
        self.wallet = wallet_manager
        self.client = client
        self.api_url = "https://pumpportal.fun/api/trade-local"

    async def _send_via_jito(self, signed_tx_bytes: bytes) -> Optional[str]:
        """Send transaction via Jito block engine for priority inclusion"""
        from config import JITO_ENDPOINTS
        import base64

        tx_base64 = base64.b64encode(signed_tx_bytes).decode('utf-8')
        endpoint = random.choice(JITO_ENDPOINTS)

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [tx_base64, {"encoding": "base64"}]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    result = await response.json()

                    if "result" in result:
                        sig = result["result"]
                        logger.info(f"🚀 Jito accepted: {sig[:16]}...")
                        return sig
                    elif "error" in result:
                        logger.warning(f"⚠️ Jito rejected: {result['error'].get('message', result['error'])}")
                        return None
                    else:
                        logger.warning(f"⚠️ Unexpected Jito response: {result}")
                        return None

        except asyncio.TimeoutError:
            logger.warning(f"⚠️ Jito timeout")
            return None
        except Exception as e:
            logger.warning(f"⚠️ Jito error: {e}")
            return None

    def _build_jito_tip_instruction(self, tip_lamports: int) -> bytes:
        """Build raw bytes for a Jito tip instruction to append to transaction"""
        from config import JITO_TIP_ACCOUNTS
        import struct

        # Pick random tip account
        tip_account = random.choice(JITO_TIP_ACCOUNTS)

        logger.debug(f"Jito tip: {tip_lamports} lamports to {tip_account[:8]}...")

        # Return tip account and amount - will be used by PumpPortal API
        return tip_account, tip_lamports

    async def get_priority_fee(self, urgency: str = "buy") -> float:
        """
        OPTIMIZED: Competitive priority fees for maximum profitability

        Fee philosophy:
        - Buy: Low fee for fast entry while minimizing costs (0.001 SOL = 2% on 0.05 SOL)
        - Sell: Slightly higher for reliable exits (0.0015 SOL = 3% on 0.05 SOL)
        - Emergency: Higher priority for critical exits only (0.002 SOL = 4% on 0.05 SOL)

        urgency levels:
        - "buy": 0.0010 SOL (normal buys, 1-3s confirm)
        - "sell": 0.0015 SOL (normal sells, 1-2s confirm)
        - "emergency": 0.0020 SOL (stop loss/rug detection only, <1s confirm)

        Breakeven analysis at 0.05 SOL:
        - Total fees: 0.001 (buy) + 0.0015 (sell) = 0.0025 SOL
        - Breakeven: +5% (75% reduction from previous 0.010 SOL!)
        """
        urgency_fees = {
            "buy": 0.0005,      # 1% on 0.05 SOL - fast enough for entry
            "sell": 0.0015,     # ✅ RAISED: 3% - need to compete with Jito during congestion
            "emergency": 0.0025 # ✅ RAISED: 5% - critical exits must land
        }
        # Total fees: 0.0013 SOL = 2.6% per trade (down from 5%)

        fee = urgency_fees.get(urgency, 0.0010)  # Default to buy fee
        logger.debug(f"Priority fee ({urgency}): {fee:.6f} SOL")
        return fee
    
    async def create_buy_transaction(
        self,
        mint: str,
        sol_amount: float,
        bonding_curve_key: str = None,
        slippage: int = 30,  # UPDATED: Tighter default (0.3%) since we validate liquidity
        urgency: str = "buy"  # Default to buy priority (0.001 SOL)
    ) -> Optional[str]:
        """Get a buy transaction from PumpPortal API with dynamic fees"""
        try:
            wallet_pubkey = str(self.wallet.pubkey)
            
            # Get dynamic priority fee
            priority_fee = await self.get_priority_fee(urgency)
            
            payload = {
                "publicKey": wallet_pubkey,
                "action": "buy",
                "mint": mint,
                "denominatedInSol": "true",
                "amount": sol_amount,
                "slippage": slippage,  # Basis points (30 = 0.3%)
                "priorityFee": priority_fee,
                "pool": "pump"
            }
            
            if bonding_curve_key:
                payload["bondingCurveKey"] = bonding_curve_key
            
            logger.info(f"Requesting buy transaction for {mint[:8]}... amount: {sol_amount} SOL")
            logger.info(f"Priority fee: {priority_fee:.6f} SOL ({urgency}), Slippage: {slippage} BPS")
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
                            
                            unsigned_tx = VersionedTransaction.from_bytes(raw_tx_bytes)
                            message = unsigned_tx.message
                            signed_tx = VersionedTransaction(message, [self.wallet.keypair])
                            signed_tx_bytes = bytes(signed_tx)
                            
                            logger.info(f"Created signed v0 transaction ({len(signed_tx_bytes)} bytes)")
                            
                        except Exception as e:
                            logger.error(f"Failed to sign v0 transaction: {e}")
                            return None
                    else:
                        # Legacy transaction - manual signing with overflow protection
                        logger.info("Legacy transaction - manual signing (robust varint parse + re-pack)")
                        try:
                            b = raw_tx_bytes
                            if len(b) < 2:
                                logger.error(f"Legacy tx too small ({len(b)} bytes)")
                                return None

                            # Parse compact-u16 signature count with overflow protection
                            idx = 0
                            val = 0
                            shift = 0
                            while True:
                                if idx >= len(b):
                                    logger.error("Bad legacy varint for sig_count")
                                    return None

                                # Check shift BEFORE parsing to prevent overflow
                                if shift > 14:
                                    logger.error(f"sig_count varint too long (shift={shift}, would overflow u16)")
                                    return None

                                byte = b[idx]
                                val |= (byte & 0x7F) << shift
                                idx += 1

                                # Check value BEFORE continuing to prevent overflow
                                if val > 65535:
                                    logger.error(f"sig_count varint value {val} exceeds u16 max (65535)")
                                    return None

                                if byte < 0x80:
                                    break
                                shift += 7

                            sig_count = val

                            # Additional validation
                            if sig_count > 100:  # Sanity check
                                logger.error(f"sig_count {sig_count} is suspiciously high, likely malformed")
                                return None

                            sig_section_end = idx + 64 * sig_count

                            if sig_section_end > len(b):
                                logger.error(f"Malformed legacy tx: sig_section_end {sig_section_end} > len {len(b)}")
                                return None

                            # Message is everything after signatures
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
                            logger.error(f"Failed to sign legacy transaction: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                            return None
                    
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
        bonding_curve_key: str = None,
        slippage: int = 50,
        token_decimals: int = 6,
        urgency: str = "sell"  # Default to sell priority (0.0015 SOL)
    ) -> Optional[str]:
        """Get a sell transaction from PumpPortal API with dynamic fees - expects UI token amounts"""
        try:
            wallet_pubkey = str(self.wallet.pubkey)
            ui_amount = float(token_amount)
            
            if ui_amount <= 0:
                logger.error(f"Invalid UI amount: {ui_amount}")
                return None
            
            # Handle decimals - it might be a tuple (decimals, source) or just an int
            if isinstance(token_decimals, tuple):
                token_decimals = token_decimals[0]
            
            # Get dynamic priority fee based on urgency
            priority_fee = await self.get_priority_fee(urgency)
            
            logger.info(f"=== SELL TRANSACTION ===")
            logger.info(f"Token: {mint[:8]}...")
            logger.info(f"UI Amount: {ui_amount:.6f} tokens")
            logger.info(f"Decimals: {token_decimals}")
            logger.info(f"Priority fee: {priority_fee:.6f} SOL ({urgency})")
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
                        # Legacy transaction - manual signing with overflow protection
                        logger.info("Legacy transaction - manual signing (robust varint parse + re-pack)")
                        try:
                            b = raw_tx_bytes
                            if len(b) < 2:
                                logger.error(f"Legacy tx too small ({len(b)} bytes)")
                                return None

                            # Parse compact-u16 signature count with overflow protection
                            idx = 0
                            val = 0
                            shift = 0
                            while True:
                                if idx >= len(b):
                                    logger.error("Bad legacy varint for sig_count")
                                    return None

                                # Check shift BEFORE parsing to prevent overflow
                                if shift > 14:
                                    logger.error(f"sig_count varint too long (shift={shift}, would overflow u16)")
                                    return None

                                byte = b[idx]
                                val |= (byte & 0x7F) << shift
                                idx += 1

                                # Check value BEFORE continuing to prevent overflow
                                if val > 65535:
                                    logger.error(f"sig_count varint value {val} exceeds u16 max (65535)")
                                    return None

                                if byte < 0x80:
                                    break
                                shift += 7

                            sig_count = val

                            # Additional validation
                            if sig_count > 100:  # Sanity check
                                logger.error(f"sig_count {sig_count} is suspiciously high, likely malformed")
                                return None

                            sig_section_end = idx + 64 * sig_count

                            if sig_section_end > len(b):
                                logger.error(f"Malformed legacy tx: sig_section_end {sig_section_end} > len {len(b)}")
                                return None

                            # Message is everything after signatures
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
                            logger.error(f"Failed to sign legacy transaction: {e}")
                            import traceback
                            logger.error(traceback.format_exc())
                            return None
                    
                    logger.info(f"Sending sell transaction for {mint[:8]}...")

                    # Send via Jito first (faster), fallback to regular RPC (must exit)
                    from config import JITO_ENABLED

                    sig = None
                    if JITO_ENABLED:
                        sig = await self._send_via_jito(signed_tx_bytes)
                        if sig:
                            logger.info(f"✅ Sell TX via Jito: {sig}")
                            return sig
                        else:
                            logger.warning(f"⚠️ Jito failed for sell - falling back to RPC")

                    # Fallback to regular RPC (MUST exit position)
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
                        logger.warning(f"RPC send failed: {e}")

                        # Try raw bytes as last resort
                        try:
                            response = self.client.send_raw_transaction(raw_tx_bytes)
                            sig = str(response.value)

                            if sig.startswith("1111111"):
                                logger.error("Transaction failed - received invalid signature on retry")
                                return None

                            logger.info(f"✅ Sell transaction sent on retry: {sig}")
                            return sig

                        except Exception as e2:
                            logger.error(f"All send attempts failed: {e2}")
                            return None
                        
        except Exception as e:
            logger.error(f"Failed to create sell transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
