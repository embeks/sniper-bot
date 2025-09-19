"""
Monitor - WebSocket listener for PumpFun launches and Raydium migrations
"""

import json
import base64
import asyncio
import logging
import websockets
from typing import Optional, Dict, Set, Callable
from datetime import datetime, timedelta
from solders.pubkey import Pubkey

from config import (
    WS_ENDPOINT, MONITOR_PROGRAMS, LOG_CONTAINS_FILTERS,
    PUMPFUN_PROGRAM_ID, RAYDIUM_PROGRAM_ID,
    RPC_ENDPOINT, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID
)

logger = logging.getLogger(__name__)

class TokenMonitor:
    """Monitor blockchain for new token launches"""
    
    def __init__(self, on_token_found: Callable):
        """Initialize monitor with callback"""
        self.on_token_found = on_token_found
        self.seen_signatures = set()
        self.seen_tokens = set()
        self.running = False
        
        # Track launch stats
        self.launches_seen = 0
        self.launches_processed = 0
        
    async def start(self):
        """Start monitoring WebSocket"""
        self.running = True
        logger.info("🔍 Starting WebSocket monitor...")
        
        # Start both WebSocket and polling
        websocket_task = asyncio.create_task(self._websocket_monitor())
        polling_task = asyncio.create_task(self._polling_monitor())
        
        await asyncio.gather(websocket_task, polling_task)
    
    async def _websocket_monitor(self):
        """WebSocket monitoring"""
        while self.running:
            try:
                await self._connect_and_monitor()
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                if self.running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)
    
    async def _polling_monitor(self):
        """Backup polling method - checks recent transactions"""
        from solana.rpc.api import Client
        client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
        
        while self.running:
            try:
                # Get recent signatures for PumpFun
                signatures = client.get_signatures_for_address(
                    PUMPFUN_PROGRAM_ID,
                    limit=20
                )
                
                if signatures.value:
                    for sig_info in signatures.value:
                        sig = sig_info.signature
                        
                        if sig in self.seen_signatures:
                            continue
                        
                        self.seen_signatures.add(sig)
                        
                        # Get transaction details
                        tx = client.get_transaction(
                            sig,
                            max_supported_transaction_version=0
                        )
                        
                        if tx.value:
                            # Check if it's a new token
                            logs = tx.value.transaction.meta.log_messages if tx.value.transaction.meta else []
                            
                            # Look for creation patterns
                            for log in logs:
                                if any(pattern in log.lower() for pattern in [
                                    'create', 'init', 'new', 'mint', 'bonding'
                                ]):
                                    logger.info(f"[POLLING] Potential launch detected: {sig[:20]}...")
                                    
                                    # Extract mint from transaction
                                    mint = self._extract_mint_from_transaction(tx.value)
                                    if mint and mint not in self.seen_tokens:
                                        self.seen_tokens.add(mint)
                                        self.launches_seen += 1
                                        
                                        logger.info(f"🚀 [POLLING] NEW TOKEN FOUND: {mint}")
                                        
                                        if self.on_token_found:
                                            self.launches_processed += 1
                                            await self.on_token_found({
                                                'mint': mint,
                                                'signature': sig,
                                                'type': 'pumpfun_launch',
                                                'source': 'polling',
                                                'timestamp': datetime.now().isoformat()
                                            })
                                    break
                
                # Poll every 2 seconds
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.debug(f"Polling error: {e}")
                await asyncio.sleep(5)
    
    def _extract_mint_from_transaction(self, tx) -> Optional[str]:
        """Extract mint address from transaction"""
        try:
            # Look through account keys
            account_keys = tx.transaction.transaction.message.account_keys
            
            # PumpFun tokens are usually in the first few accounts after program
            for i, key in enumerate(account_keys):
                key_str = str(key)
                # Skip known program IDs and system accounts
                if key_str not in [
                    str(PUMPFUN_PROGRAM_ID),
                    str(SYSTEM_PROGRAM_ID), 
                    str(TOKEN_PROGRAM_ID),
                    "11111111111111111111111111111111",
                    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
                ] and len(key_str) > 30:
                    # This might be the mint
                    return key_str
            
            return None
        except:
            return None
    
    async def _connect_and_monitor(self):
        """Connect to WebSocket and monitor logs"""
        async with websockets.connect(WS_ENDPOINT, max_size=None) as websocket:
            logger.info(f"✅ Connected to WebSocket: {WS_ENDPOINT[:50]}...")
            
            # Subscribe to programs
            await self._subscribe_to_programs(websocket)
            
            # Process messages
            while self.running:
                try:
                    message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=30.0
                    )
                    await self._process_message(message)
                    
                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    await websocket.ping()
                    
                except Exception as e:
                    logger.error(f"Message processing error: {e}")
                    break
    
    async def _subscribe_to_programs(self, websocket):
        """Subscribe to program logs"""
        # Subscribe to PumpFun program logs
        subscribe_msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "logsSubscribe",
            "params": [
                {
                    "mentions": [str(PUMPFUN_PROGRAM_ID)]
                },
                {
                    "commitment": "confirmed"
                }
            ]
        }
        
        await websocket.send(json.dumps(subscribe_msg))
        logger.info(f"📡 Subscribed to PumpFun program logs")
        
        # Also subscribe to Raydium for migration detection
        raydium_msg = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "logsSubscribe",
            "params": [
                {
                    "mentions": [str(RAYDIUM_PROGRAM_ID)]
                },
                {
                    "commitment": "confirmed"
                }
            ]
        }
        
        await websocket.send(json.dumps(raydium_msg))
        logger.info(f"📡 Subscribed to Raydium program logs")
    
    async def _process_message(self, message: str):
        """Process WebSocket message"""
        try:
            data = json.loads(message)
            
            # Check if it's a notification
            if data.get('method') == 'logsNotification':
                result = data.get('params', {}).get('result', {})
                
                # Get signature
                signature = result.get('value', {}).get('signature')
                if not signature or signature in self.seen_signatures:
                    return
                
                self.seen_signatures.add(signature)
                
                # Get logs
                logs = result.get('value', {}).get('logs', [])
                
                # Only process if it's PumpFun related
                if not any('6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwkvq' in log for log in logs):
                    return
                
                # Look for specific creation indicators
                is_creation = False
                for log in logs:
                    # Strong indicators of new token creation
                    if any(indicator in log for indicator in [
                        'InitializeMint2',  # Token mint initialization
                        'InitializeAccount3',  # Token account creation
                        'MintTo',  # Initial mint
                        'create_mint',
                        'create_token',
                        'Instruction: Create',
                        'Instruction: Initialize'
                    ]):
                        is_creation = True
                        break
                
                if is_creation:
                    # Extract mint address
                    mint = self._extract_mint_from_logs(logs)
                    if mint and mint not in self.seen_tokens:
                        self.seen_tokens.add(mint)
                        self.launches_seen += 1
                        
                        logger.info("=" * 60)
                        logger.info(f"🚀 NEW PUMPFUN LAUNCH DETECTED!")
                        logger.info(f"📜 Mint: {mint}")
                        logger.info(f"📝 Signature: {signature[:20]}...")
                        logger.info(f"📊 Total launches seen: {self.launches_seen}")
                        logger.info("=" * 60)
                        
                        # Call the callback
                        if self.on_token_found:
                            self.launches_processed += 1
                            await self.on_token_found({
                                'mint': mint,
                                'signature': signature,
                                'type': 'pumpfun_launch',
                                'timestamp': datetime.now().isoformat()
                            })
                    
        except Exception as e:
            pass  # Silently ignore parse errors in high volume
    
    def _is_pumpfun_launch(self, logs: list) -> bool:
        """Check if logs indicate a PumpFun token launch"""
        for log in logs:
            log_lower = log.lower()
            # Look for multiple possible indicators
            if any(indicator in log_lower for indicator in [
                "initializebondingcurve",
                "initialize_bonding_curve", 
                "createbondingcurve",
                "create_bonding_curve",
                "bondingcurve",
                "init",
                "create",
                "new token",
                "mint:"
            ]):
                # Confirm it's from PumpFun program
                if "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwkvq" in str(logs):
                    return True
        return False
    
    def _is_raydium_creation(self, logs: list) -> bool:
        """Check if logs indicate a Raydium pool creation"""
        for log in logs:
            if "initialize2" in log.lower() or "InitializePool" in log:
                return True
        return False
    
    async def _handle_pumpfun_launch(self, logs: list, signature: str):
        """Handle a PumpFun token launch"""
        try:
            # Extract mint address from logs
            mint = self._extract_mint_from_logs(logs)
            if not mint or mint in self.seen_tokens:
                return
            
            self.seen_tokens.add(mint)
            self.launches_seen += 1
            
            logger.info("=" * 60)
            logger.info(f"🚀 NEW PUMPFUN LAUNCH DETECTED!")
            logger.info(f"📜 Mint: {mint}")
            logger.info(f"📝 Signature: {signature[:20]}...")
            logger.info(f"📊 Total launches seen: {self.launches_seen}")
            logger.info("=" * 60)
            
            # Call the callback
            if self.on_token_found:
                self.launches_processed += 1
                await self.on_token_found({
                    'mint': mint,
                    'signature': signature,
                    'type': 'pumpfun_launch',
                    'timestamp': datetime.now().isoformat()
                })
                
        except Exception as e:
            logger.error(f"Failed to handle PumpFun launch: {e}")
    
    async def _handle_raydium_creation(self, logs: list, signature: str):
        """Handle a Raydium pool creation (migration)"""
        try:
            # Extract mint from logs
            mint = self._extract_mint_from_logs(logs)
            if not mint:
                return
            
            logger.info(f"📊 Raydium pool created for {mint[:8]}... (likely PumpFun migration)")
            
            # Note: In Phase 1, we might skip these as they're already migrated
            
        except Exception as e:
            logger.error(f"Failed to handle Raydium creation: {e}")
    
    def _extract_mint_from_logs(self, logs: list) -> Optional[str]:
        """Extract mint address from transaction logs"""
        try:
            # Look for mint address patterns in logs
            for log in logs:
                # Check for "mint: <address>" pattern
                if "mint:" in log.lower():
                    parts = log.split("mint:")
                    if len(parts) > 1:
                        potential_mint = parts[1].strip().split()[0]
                        if len(potential_mint) >= 32:  # Valid Solana address length
                            return potential_mint
                
                # Check for token program patterns
                if "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA" in log:
                    # Extract the account that's not the token program
                    parts = log.split()
                    for part in parts:
                        if len(part) >= 32 and part != "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA":
                            try:
                                # Verify it's a valid pubkey
                                Pubkey.from_string(part)
                                return part
                            except:
                                continue
            
            # Alternative: parse from instruction data if available
            for log in logs:
                if "Program data:" in log:
                    data_str = log.split("Program data:")[1].strip()
                    # Decode and look for mint
                    # This would require more complex parsing based on instruction layout
                    
            return None
            
        except Exception as e:
            logger.debug(f"Failed to extract mint: {e}")
            return None
    
    def stop(self):
        """Stop monitoring"""
        self.running = False
        logger.info(f"Monitor stopped. Processed {self.launches_processed}/{self.launches_seen} launches")

class QuickMonitor:
    """Quick synchronous monitor for testing"""
    
    def __init__(self, dex_manager):
        self.dex = dex_manager
        self.processed = set()
        
    def check_recent_launches(self) -> list:
        """Check for recent launches via RPC (fallback method)"""
        try:
            from solana.rpc.api import Client
            client = Client(RPC_ENDPOINT.replace('wss://', 'https://').replace('ws://', 'http://'))
            
            # Get recent signatures for PumpFun program
            signatures = client.get_signatures_for_address(
                PUMPFUN_PROGRAM_ID,
                limit=10
            )
            
            launches = []
            
            if signatures.value:
                for sig_info in signatures.value:
                    sig = sig_info.signature
                    
                    if sig in self.processed:
                        continue
                    
                    # Get transaction
                    tx = client.get_transaction(
                        sig,
                        max_supported_transaction_version=0
                    )
                    
                    if tx.value:
                        # Parse transaction for mint
                        # This is simplified - real implementation would parse properly
                        self.processed.add(sig)
                        
                        logger.info(f"Found transaction: {sig[:20]}...")
            
            return launches
            
        except Exception as e:
            logger.error(f"Failed to check recent launches: {e}")
            return []
