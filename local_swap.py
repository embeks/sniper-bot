
"""
Local Pump.fun Swap Builder - Eliminates PumpPortal API latency
Saves 200-500ms per trade by building transactions locally
"""

import struct
import logging
import time
import random
import aiohttp
import asyncio
from typing import Optional, Tuple
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.message import Message
from solana.rpc.api import Client
from solana.rpc.types import TxOpts

from config import (
    PUMPFUN_PROGRAM_ID,
    PUMPFUN_FEE_RECIPIENT,
    TOKEN_PROGRAM_ID,
    TOKEN_2022_PROGRAM_ID,
    ASSOCIATED_TOKEN_PROGRAM_ID,
    SYSTEM_PROGRAM_ID,
    RENT_PROGRAM_ID,
)

logger = logging.getLogger(__name__)

# Fee Program (for feeConfig PDA)
FEE_PROGRAM_ID = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

# Pump.fun instruction discriminators (first 8 bytes of sha256("global:buy") etc)
BUY_DISCRIMINATOR = bytes([0x66, 0x06, 0x3d, 0x12, 0x01, 0xda, 0xeb, 0xea])
SELL_DISCRIMINATOR = bytes([0x33, 0xe6, 0x85, 0xa4, 0x01, 0x7f, 0x83, 0xad])


class LocalSwapBuilder:
    """Build Pump.fun swap transactions locally - no external API calls"""
    
    def __init__(self, wallet_manager, rpc_client: Client):
        self.wallet = wallet_manager
        self.client = rpc_client
        
        # Derive global PDA once (constant)
        self.global_pda = Pubkey.find_program_address(
            [b"global"],
            PUMPFUN_PROGRAM_ID
        )[0]
        
        # Event authority PDA
        self.event_authority = Pubkey.find_program_address(
            [b"__event_authority"],
            PUMPFUN_PROGRAM_ID
        )[0]

        # Global Volume Accumulator PDA
        self.global_volume_accumulator = Pubkey.find_program_address(
            [b"global_volume_accumulator"],
            PUMPFUN_PROGRAM_ID
        )[0]

        # Fee Config PDA (owned by Fee Program, seeds include Pump program ID)
        self.fee_config = Pubkey.find_program_address(
            [b"fee_config", bytes(PUMPFUN_PROGRAM_ID)],
            FEE_PROGRAM_ID
        )[0]

        # Blockhash caching - refresh every 800ms in background
        self._cached_blockhash = None
        self._blockhash_lock = asyncio.Lock()
        self._blockhash_task = None

        # Jito endpoint latency tracking - pick fastest responding endpoint
        self._jito_latencies = {}  # endpoint -> list of recent latencies (ms)

        logger.info(f"LocalSwapBuilder initialized")
        logger.info(f"  Global PDA: {self.global_pda}")
        logger.info(f"  Event Authority: {self.event_authority}")
        logger.info(f"  Global Volume Accumulator: {self.global_volume_accumulator}")
        logger.info(f"  Fee Config: {self.fee_config}")

    async def start_blockhash_cache(self):
        """Start background task to refresh blockhash every 800ms"""
        if self._blockhash_task is None:
            self._blockhash_task = asyncio.create_task(self._refresh_blockhash_loop())
            logger.info("🔄 Blockhash cache started (refreshes every 800ms)")

    async def _refresh_blockhash_loop(self):
        """Background loop to refresh blockhash every 800ms (Solana slots are ~400ms)"""
        while True:
            try:
                blockhash_resp = self.client.get_latest_blockhash()
                async with self._blockhash_lock:
                    self._cached_blockhash = blockhash_resp.value.blockhash
            except Exception as e:
                logger.warning(f"⚠️ Blockhash refresh failed: {e}")
            await asyncio.sleep(0.8)  # Reduced from 2s - fresher blockhash = less rejection risk

    def _build_jito_tip_instruction(self, tip_lamports: int) -> Instruction:
        """Build a SOL transfer instruction to a random Jito tip account"""
        from config import JITO_TIP_ACCOUNTS

        tip_account = Pubkey.from_string(random.choice(JITO_TIP_ACCOUNTS))

        # System program transfer instruction (discriminator = 2 for Transfer)
        data = bytes([2, 0, 0, 0]) + struct.pack('<Q', tip_lamports)

        accounts = [
            AccountMeta(self.wallet.pubkey, is_signer=True, is_writable=True),
            AccountMeta(tip_account, is_signer=False, is_writable=True),
        ]

        return Instruction(SYSTEM_PROGRAM_ID, data, accounts)

    def _get_fastest_jito_endpoint(self, endpoints: list) -> str:
        """Pick the Jito endpoint with lowest average latency, or random if no data"""
        if not self._jito_latencies:
            return random.choice(endpoints)

        # Calculate average latency for each endpoint (last 5 requests)
        avg_latencies = {}
        for ep in endpoints:
            if ep in self._jito_latencies and self._jito_latencies[ep]:
                recent = self._jito_latencies[ep][-5:]
                avg_latencies[ep] = sum(recent) / len(recent)
            else:
                avg_latencies[ep] = 500  # Default 500ms for unknown endpoints

        # Pick fastest, with some randomness to avoid always hammering one endpoint
        sorted_eps = sorted(avg_latencies.items(), key=lambda x: x[1])
        # Pick from top 2 fastest endpoints randomly
        top_eps = [ep for ep, _ in sorted_eps[:2]]
        return random.choice(top_eps)

    def _record_jito_latency(self, endpoint: str, latency_ms: float, success: bool):
        """Record latency for an endpoint (penalize failures)"""
        if endpoint not in self._jito_latencies:
            self._jito_latencies[endpoint] = []

        # Penalize failures with high latency value
        recorded_latency = latency_ms if success else 1000.0
        self._jito_latencies[endpoint].append(recorded_latency)

        # Keep only last 10 measurements
        if len(self._jito_latencies[endpoint]) > 10:
            self._jito_latencies[endpoint] = self._jito_latencies[endpoint][-10:]

    async def _send_via_jito(self, signed_tx_bytes: bytes) -> Optional[str]:
        """Send transaction via Jito block engine for priority inclusion"""
        from config import JITO_ENDPOINTS
        import base64

        tx_base64 = base64.b64encode(signed_tx_bytes).decode('utf-8')
        endpoint = self._get_fastest_jito_endpoint(JITO_ENDPOINTS)
        start_time = time.time()

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
                    timeout=aiohttp.ClientTimeout(total=1.0)  # Reduced from 5s - Jito responds in 50-200ms normally
                ) as response:
                    result = await response.json()
                    latency_ms = (time.time() - start_time) * 1000

                    if "result" in result:
                        sig = result["result"]
                        self._record_jito_latency(endpoint, latency_ms, success=True)
                        logger.info(f"🚀 Jito accepted: {sig[:16]}...")
                        return sig
                    elif "error" in result:
                        self._record_jito_latency(endpoint, latency_ms, success=False)
                        logger.warning(f"⚠️ Jito rejected: {result['error'].get('message', result['error'])}")
                        return None
                    else:
                        self._record_jito_latency(endpoint, latency_ms, success=False)
                        logger.warning(f"⚠️ Unexpected Jito response: {result}")
                        return None

        except asyncio.TimeoutError:
            self._record_jito_latency(endpoint, 1000.0, success=False)
            logger.warning(f"⚠️ Jito timeout ({endpoint.split('/')[2]})")
            return None
        except Exception as e:
            self._record_jito_latency(endpoint, 1000.0, success=False)
            logger.warning(f"⚠️ Jito error: {e}")
            return None

    def derive_bonding_curve_pda(self, mint: Pubkey) -> Tuple[Pubkey, int]:
        """Derive bonding curve PDA for a token"""
        return Pubkey.find_program_address(
            [b"bonding-curve", bytes(mint)],
            PUMPFUN_PROGRAM_ID
        )

    def derive_creator_vault_pda(self, creator: Pubkey) -> Pubkey:
        """
        Derive Creator Vault PDA - REQUIRED for PumpFun buy transactions
        Seeds: ["creator-vault", creator_pubkey]  # NO MINT!
        """
        return Pubkey.find_program_address(
            [b"creator-vault", bytes(creator)],
            PUMPFUN_PROGRAM_ID
        )[0]

    def derive_user_volume_accumulator(self, user: Pubkey) -> Pubkey:
        """Derive User Volume Accumulator PDA"""
        return Pubkey.find_program_address(
            [b"user_volume_accumulator", bytes(user)],
            PUMPFUN_PROGRAM_ID
        )[0]

    def derive_associated_token_account(self, owner: Pubkey, mint: Pubkey) -> Pubkey:
        """Derive ATA address for Token-2022"""
        return Pubkey.find_program_address(
            [bytes(owner), bytes(TOKEN_2022_PROGRAM_ID), bytes(mint)],
            ASSOCIATED_TOKEN_PROGRAM_ID
        )[0]
    
    def calculate_tokens_out(
        self,
        sol_amount_lamports: int,
        virtual_sol_reserves: int,
        virtual_token_reserves: int
    ) -> int:
        """
        Calculate tokens received for SOL input (constant product AMM)
        Formula: tokens_out = (sol_in * token_reserves) / (sol_reserves + sol_in)
        """
        tokens_out = (sol_amount_lamports * virtual_token_reserves) // (virtual_sol_reserves + sol_amount_lamports)
        return tokens_out
    
    def calculate_sol_out(
        self,
        token_amount: int,
        virtual_sol_reserves: int,
        virtual_token_reserves: int
    ) -> int:
        """
        Calculate SOL received for token input (constant product AMM)
        Formula: sol_out = (token_in * sol_reserves) / (token_reserves + token_in)
        """
        sol_out = (token_amount * virtual_sol_reserves) // (virtual_token_reserves + token_amount)
        return sol_out
    
    def build_buy_instruction(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        associated_bonding_curve: Pubkey,
        user_ata: Pubkey,
        creator_vault: Pubkey,
        user_volume_accumulator: Pubkey,
        token_amount: int,
        max_sol_cost: int
    ) -> Instruction:
        """
        Build Pump.fun buy instruction (15 accounts per current IDL)
        """
        # Instruction data: discriminator + amount (u64) + max_sol_cost (u64) + track_volume (u8: 0=None, 1=False, 2=True)
        data = BUY_DISCRIMINATOR + struct.pack('<Q', token_amount) + struct.pack('<Q', max_sol_cost) + bytes([0])  # 0 = None/don't track

        # Account order per IDL (15 accounts total)
        accounts = [
            AccountMeta(self.global_pda, is_signer=False, is_writable=False),             # 0 Global
            AccountMeta(PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),        # 1 Fee Recipient
            AccountMeta(mint, is_signer=False, is_writable=False),                        # 2 Mint
            AccountMeta(bonding_curve, is_signer=False, is_writable=True),                # 3 Bonding Curve
            AccountMeta(associated_bonding_curve, is_signer=False, is_writable=True),     # 4 Associated Bonding Curve
            AccountMeta(user_ata, is_signer=False, is_writable=True),                     # 5 Associated User (ATA)
            AccountMeta(self.wallet.pubkey, is_signer=True, is_writable=True),            # 6 User
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),           # 7 System Program
            AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),       # 8 Token Program
            AccountMeta(creator_vault, is_signer=False, is_writable=True),                # 9 Creator Vault PDA
            AccountMeta(self.event_authority, is_signer=False, is_writable=False),        # 10 Event Authority
            AccountMeta(PUMPFUN_PROGRAM_ID, is_signer=False, is_writable=False),          # 11 Program
            AccountMeta(self.global_volume_accumulator, is_signer=False, is_writable=True), # 12 Global Volume Accumulator
            AccountMeta(user_volume_accumulator, is_signer=False, is_writable=True),      # 13 User Volume Accumulator
            AccountMeta(self.fee_config, is_signer=False, is_writable=False),             # 14 Fee Config
            AccountMeta(FEE_PROGRAM_ID, is_signer=False, is_writable=False),              # 15 Fee Program
        ]

        return Instruction(PUMPFUN_PROGRAM_ID, data, accounts)

    def build_sell_instruction(
        self,
        mint: Pubkey,
        bonding_curve: Pubkey,
        associated_bonding_curve: Pubkey,
        user_ata: Pubkey,
        creator_vault: Pubkey,
        token_amount: int,
        min_sol_output: int
    ) -> Instruction:
        """
        Build Pump.fun sell instruction (14 accounts per current IDL)
        """
        # Instruction data: discriminator + token_amount (u64) + min_sol_output (u64)
        data = SELL_DISCRIMINATOR + struct.pack('<Q', token_amount) + struct.pack('<Q', min_sol_output)

        accounts = [
            AccountMeta(self.global_pda, is_signer=False, is_writable=False),             # 0 Global
            AccountMeta(PUMPFUN_FEE_RECIPIENT, is_signer=False, is_writable=True),        # 1 Fee Recipient
            AccountMeta(mint, is_signer=False, is_writable=False),                        # 2 Mint
            AccountMeta(bonding_curve, is_signer=False, is_writable=True),                # 3 Bonding Curve
            AccountMeta(associated_bonding_curve, is_signer=False, is_writable=True),     # 4 Associated Bonding Curve
            AccountMeta(user_ata, is_signer=False, is_writable=True),                     # 5 Associated User
            AccountMeta(self.wallet.pubkey, is_signer=True, is_writable=True),            # 6 User
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),           # 7 System Program
            AccountMeta(creator_vault, is_signer=False, is_writable=True),                # 8 Creator Vault
            AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),       # 9 Token Program
            AccountMeta(self.event_authority, is_signer=False, is_writable=False),        # 10 Event Authority
            AccountMeta(PUMPFUN_PROGRAM_ID, is_signer=False, is_writable=False),          # 11 Program
            AccountMeta(self.fee_config, is_signer=False, is_writable=True),              # 12 Fee Config
            AccountMeta(FEE_PROGRAM_ID, is_signer=False, is_writable=False),              # 13 Fee Program
        ]

        return Instruction(PUMPFUN_PROGRAM_ID, data, accounts)
    
    async def create_buy_transaction(
        self,
        mint: str,
        sol_amount: float,
        curve_data: dict,
        slippage_bps: int = 5000,
        creator: str = None,
        velocity: float = 0.0
    ) -> Optional[str]:
        """
        Build and send a buy transaction locally
        Tries Jito first, immediate RPC fallback if Jito fails
        """
        try:
            start = time.time()

            if not creator:
                logger.error(f"❌ Creator pubkey required for local TX")
                return None

            # Dynamic slippage based on velocity
            # Faster tokens = more competition = need higher slippage
            if velocity > 0:
                if velocity >= 15.0:
                    # Hyper-fast: 200% slippage (3x max cost)
                    slippage_bps = max(slippage_bps, 20000)
                    logger.info(f"   🚀 Hyper velocity ({velocity:.1f}/s) → 200% slippage")
                elif velocity >= 8.0:
                    # Fast: 150% slippage (2.5x max cost)
                    slippage_bps = max(slippage_bps, 15000)
                    logger.info(f"   ⚡ Fast velocity ({velocity:.1f}/s) → 150% slippage")
                elif velocity >= 4.0:
                    # Medium: 100% slippage (2x max cost)
                    slippage_bps = max(slippage_bps, 10000)
                    logger.info(f"   📈 Medium velocity ({velocity:.1f}/s) → 100% slippage")
                # else: use passed slippage_bps (default 50%)

            mint_pubkey = Pubkey.from_string(mint)
            creator_pubkey = Pubkey.from_string(creator)

            # Derive PDAs
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            associated_bonding_curve = self.derive_associated_token_account(bonding_curve, mint_pubkey)
            user_ata = self.derive_associated_token_account(self.wallet.pubkey, mint_pubkey)
            creator_vault = self.derive_creator_vault_pda(creator_pubkey)
            user_volume_accumulator = self.derive_user_volume_accumulator(self.wallet.pubkey)

            # Get reserves from curve_data
            virtual_sol = curve_data.get('virtual_sol_reserves', 0)
            virtual_tokens = curve_data.get('virtual_token_reserves', 0)

            if virtual_sol == 0 or virtual_tokens == 0:
                logger.error(f"Invalid curve data: sol={virtual_sol}, tokens={virtual_tokens}")
                return None

            # Calculate tokens out
            sol_lamports = int(sol_amount * 1e9)
            tokens_out_raw = self.calculate_tokens_out(sol_lamports, virtual_sol, virtual_tokens)

            # Dynamic slippage: reduce expected tokens to account for price movement
            # With 150% slippage (slippage_bps=15000), we expect 40% of calculated tokens
            # This means we're saying "I'll accept getting in at a higher price"
            token_slippage_factor = 10000 / (10000 + slippage_bps)
            tokens_out = int(tokens_out_raw * token_slippage_factor)

            # Max SOL cost: the absolute ceiling we'll pay
            # For fast tokens, allow up to 3x input to compete with other bots
            # We only PAY what tokens actually cost - this is just the ceiling
            max_sol_cost = int(sol_lamports * (10000 + slippage_bps) / 10000)

            logger.info(f"⚡ Building LOCAL buy TX for {mint[:8]}...")
            logger.info(f"   Creator: {creator[:16]}...")
            logger.info(f"   SOL in: {sol_amount} ({sol_lamports:,} lamports)")
            logger.info(f"   Raw tokens: {tokens_out_raw:,}")
            logger.info(f"   Adjusted tokens (slippage): {tokens_out:,} ({token_slippage_factor:.1%})")
            logger.info(f"   Max SOL cost: {max_sol_cost:,} lamports ({max_sol_cost/1e9:.4f} SOL)")

            # Build buy instruction
            buy_ix = self.build_buy_instruction(
                mint_pubkey,
                bonding_curve,
                associated_bonding_curve,
                user_ata,
                creator_vault,
                user_volume_accumulator,
                tokens_out,
                max_sol_cost
            )

            # Create ATA instruction (always needed for new tokens)
            ata_accounts = [
                AccountMeta(self.wallet.pubkey, is_signer=True, is_writable=True),
                AccountMeta(user_ata, is_signer=False, is_writable=True),
                AccountMeta(self.wallet.pubkey, is_signer=False, is_writable=False),
                AccountMeta(mint_pubkey, is_signer=False, is_writable=False),
                AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(TOKEN_2022_PROGRAM_ID, is_signer=False, is_writable=False),
            ]
            create_ata_ix = Instruction(ASSOCIATED_TOKEN_PROGRAM_ID, bytes(), ata_accounts)

            # Get blockhash
            if self._cached_blockhash:
                recent_blockhash = self._cached_blockhash
            else:
                blockhash_resp = self.client.get_latest_blockhash()
                recent_blockhash = blockhash_resp.value.blockhash

            # ===== ATTEMPT 1: JITO =====
            from config import JITO_ENABLED, JITO_TIP_AMOUNT_SOL, JITO_TIP_AGGRESSIVE_SOL

            sig = None
            if JITO_ENABLED:
                jito_tip_sol = JITO_TIP_AGGRESSIVE_SOL if slippage_bps >= 5000 else JITO_TIP_AMOUNT_SOL
                tip_lamports = int(jito_tip_sol * 1e9)
                tip_ix = self._build_jito_tip_instruction(tip_lamports)

                jito_instructions = [create_ata_ix, buy_ix, tip_ix]

                message = Message.new_with_blockhash(
                    jito_instructions,
                    self.wallet.pubkey,
                    recent_blockhash
                )
                tx = Transaction.new_unsigned(message)
                tx.sign([self.wallet.keypair], recent_blockhash)

                logger.info(f"   💰 Trying Jito first (tip: {jito_tip_sol} SOL)...")
                sig = await self._send_via_jito(bytes(tx))

                if sig:
                    total_time = (time.time() - start) * 1000
                    logger.info(f"✅ LOCAL buy TX via Jito in {total_time:.1f}ms: {sig}")
                    return sig
                else:
                    logger.warning(f"⚠️ Jito failed - immediate RPC fallback")

            # ===== ATTEMPT 2: RPC + PRIORITY FEE =====
            from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

            # 0.002 SOL priority fee
            compute_limit_ix = set_compute_unit_limit(200_000)
            compute_price_ix = set_compute_unit_price(10_000_000)

            rpc_instructions = [compute_limit_ix, compute_price_ix, create_ata_ix, buy_ix]

            # Use cached blockhash - don't add 100-500ms delay for fresh one
            # Blockhash refreshes every 800ms, still valid from Jito attempt

            message = Message.new_with_blockhash(
                rpc_instructions,
                self.wallet.pubkey,
                recent_blockhash
            )
            tx = Transaction.new_unsigned(message)
            tx.sign([self.wallet.keypair], recent_blockhash)

            logger.info(f"   💰 RPC fallback with 0.002 SOL priority fee...")

            opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            response = self.client.send_raw_transaction(bytes(tx), opts)
            sig = str(response.value)

            if sig.startswith("1111111"):
                logger.error("Transaction failed - invalid signature")
                return None

            total_time = (time.time() - start) * 1000
            logger.info(f"✅ LOCAL buy TX via RPC in {total_time:.1f}ms: {sig}")

            return sig

        except Exception as e:
            logger.error(f"Failed to create local buy transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    async def create_sell_transaction(
        self,
        mint: str,
        token_amount_ui: float,
        curve_data: dict = None,
        slippage_bps: int = 5000,
        token_decimals: int = 6,
        creator: str = None
    ) -> Optional[str]:
        """
        Build and send a sell transaction locally - JITO FIRST like buys

        Args:
            mint: Token mint address
            token_amount_ui: Tokens to sell (UI/human-readable amount)
            curve_data: Bonding curve data from Helius (optional, will query chain if not provided)
            slippage_bps: Slippage in basis points
            token_decimals: Token decimals (default 6 for PumpFun)

        Returns:
            Transaction signature or None on failure
        """
        try:
            start = time.time()

            mint_pubkey = Pubkey.from_string(mint)
            token_amount = int(token_amount_ui * (10 ** token_decimals))

            # Derive PDAs
            bonding_curve, _ = self.derive_bonding_curve_pda(mint_pubkey)
            associated_bonding_curve = self.derive_associated_token_account(bonding_curve, mint_pubkey)
            user_ata = self.derive_associated_token_account(self.wallet.pubkey, mint_pubkey)

            # Derive creator vault (required for sell)
            if not creator:
                logger.error(f"❌ Creator pubkey required for local sell TX")
                return None
            creator_pubkey = Pubkey.from_string(creator)
            creator_vault = self.derive_creator_vault_pda(creator_pubkey)

            # Use passed curve_data if available (from Helius - faster)
            # Otherwise query chain (slower but accurate)
            if curve_data and curve_data.get('virtual_sol_reserves') and curve_data.get('virtual_token_reserves'):
                virtual_sol_reserves = curve_data['virtual_sol_reserves']
                virtual_token_reserves = curve_data['virtual_token_reserves']
                logger.info(f"⚡ Using passed curve data (Helius): {virtual_sol_reserves/1e9:.2f} vSOL")
            else:
                # Fallback: Query chain
                curve_account = self.client.get_account_info(bonding_curve)
                if not curve_account.value:
                    logger.error(f"❌ Could not fetch bonding curve from chain")
                    return None

                curve_bytes = bytes(curve_account.value.data)
                virtual_token_reserves = struct.unpack('<Q', curve_bytes[8:16])[0]
                virtual_sol_reserves = struct.unpack('<Q', curve_bytes[16:24])[0]
                logger.info(f"📊 Queried chain for curve: {virtual_sol_reserves/1e9:.2f} vSOL")

            if virtual_sol_reserves == 0 or virtual_token_reserves == 0:
                logger.error(f"Invalid curve data: sol={virtual_sol_reserves}, tokens={virtual_token_reserves}")
                return None

            # Calculate SOL out
            sol_out = self.calculate_sol_out(token_amount, virtual_sol_reserves, virtual_token_reserves)
            # For emergency slippage (95%+), accept ANY output - we want OUT
            if slippage_bps >= 9500:
                min_sol_output = 1  # 1 lamport = accept anything
            else:
                min_sol_output = int(sol_out * (10000 - slippage_bps) / 10000)

            logger.info(f"⚡ Building LOCAL sell TX for {mint[:8]}...")
            logger.info(f"   Tokens: {token_amount_ui:,.2f} ({token_amount:,} atomic)")
            logger.info(f"   Expected SOL: {sol_out / 1e9:.6f}")
            logger.info(f"   Min SOL ({slippage_bps/100:.0f}% slip): {min_sol_output / 1e9:.6f}")

            # Build sell instruction
            sell_ix = self.build_sell_instruction(
                mint_pubkey,
                bonding_curve,
                associated_bonding_curve,
                user_ata,
                creator_vault,
                token_amount,
                min_sol_output
            )

            # Get blockhash (use cache if available)
            if self._cached_blockhash:
                recent_blockhash = self._cached_blockhash
            else:
                blockhash_resp = self.client.get_latest_blockhash()
                recent_blockhash = blockhash_resp.value.blockhash

            # ===== ATTEMPT 1: JITO (same as buys) =====
            from config import JITO_ENABLED, JITO_TIP_AMOUNT_SOL

            sig = None
            if JITO_ENABLED:
                # Use same tip as buys for priority
                jito_tip_sol = JITO_TIP_AMOUNT_SOL  # 0.003 SOL
                tip_lamports = int(jito_tip_sol * 1e9)
                tip_ix = self._build_jito_tip_instruction(tip_lamports)

                jito_instructions = [sell_ix, tip_ix]

                message = Message.new_with_blockhash(
                    jito_instructions,
                    self.wallet.pubkey,
                    recent_blockhash
                )
                tx = Transaction.new_unsigned(message)
                tx.sign([self.wallet.keypair], recent_blockhash)

                logger.info(f"   💰 Trying Jito first (tip: {jito_tip_sol} SOL)...")
                sig = await self._send_via_jito(bytes(tx))

                if sig:
                    total_time = (time.time() - start) * 1000
                    logger.info(f"✅ LOCAL sell TX via Jito in {total_time:.1f}ms: {sig}")
                    return sig
                else:
                    logger.warning(f"⚠️ Jito failed - falling back to RPC")

            # ===== ATTEMPT 2: RPC with priority fee =====
            from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

            # 0.002 SOL priority fee for fast inclusion
            compute_limit_ix = set_compute_unit_limit(200_000)
            compute_price_ix = set_compute_unit_price(10_000_000)

            rpc_instructions = [compute_limit_ix, compute_price_ix, sell_ix]

            # Use cached blockhash - don't add 100-500ms delay for fresh one
            # Blockhash refreshes every 800ms, still valid from Jito attempt

            message = Message.new_with_blockhash(
                rpc_instructions,
                self.wallet.pubkey,
                recent_blockhash
            )
            tx = Transaction.new_unsigned(message)
            tx.sign([self.wallet.keypair], recent_blockhash)

            logger.info(f"   💰 RPC fallback with priority fee...")

            opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            response = self.client.send_raw_transaction(bytes(tx), opts)
            sig = str(response.value)

            if sig.startswith("1111111"):
                logger.error("Transaction failed - invalid signature")
                return None

            total_time = (time.time() - start) * 1000
            logger.info(f"✅ LOCAL sell TX via RPC in {total_time:.1f}ms: {sig}")

            return sig

        except Exception as e:
            logger.error(f"Failed to create local sell transaction: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
