# pumpfun_buy.py - FIXED VERSION WITH MULTIPLE FALLBACKS
import logging
import asyncio
import base64
from typing import Dict, Any, Optional
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.sysvar import RENT, INSTRUCTIONS as SYSVAR_INSTRUCTIONS, CLOCK as SYSVAR_CLOCK
from solders.instruction import Instruction, AccountMeta
from solders.message import MessageV0
from solders.transaction import VersionedTransaction
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.instructions import (
    get_associated_token_address,
    create_associated_token_account,
    sync_native,
    SyncNativeParams,
    close_account,
    CloseAccountParams
)
from spl.token.constants import TOKEN_PROGRAM_ID, WRAPPED_SOL_MINT, ASSOCIATED_TOKEN_PROGRAM_ID

# Try to import TOKEN_2022_PROGRAM_ID, define it manually if not available
try:
    from spl.token.constants import TOKEN_2022_PROGRAM_ID
except ImportError:
    TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhqTBzKfTwRoFqbakB5uVBBBKgiV")

# Import config for other settings
import config
CONFIG = config.load()

# FIXED: Multiple fallback methods for PumpFun Program ID
PUMPFUN_PROGRAM_ID = None

# Method 1: Try from raw bytes (most reliable)
try:
    PUMPFUN_PROGRAM_BYTES = [
        6, 221, 246, 225, 215, 101, 161, 147, 217, 203, 225, 70, 206, 235, 121, 172,
        28, 180, 133, 237, 95, 91, 55, 145, 58, 140, 245, 133, 126, 255, 0, 169
    ]
    PUMPFUN_PROGRAM_ID = Pubkey(bytes(PUMPFUN_PROGRAM_BYTES))
    logging.info(f"[PumpFun] Program ID loaded from bytes: {str(PUMPFUN_PROGRAM_ID)[:8]}...")
except Exception as e:
    logging.warning(f"[PumpFun] Failed to load from bytes: {e}")

# Method 2: Try from base58 decode if bytes failed
if PUMPFUN_PROGRAM_ID is None:
    try:
        import base58
        decoded = base58.b58decode("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
        PUMPFUN_PROGRAM_ID = Pubkey(decoded)
        logging.info(f"[PumpFun] Program ID loaded from base58: {str(PUMPFUN_PROGRAM_ID)[:8]}...")
    except Exception as e:
        logging.warning(f"[PumpFun] Failed to load from base58: {e}")

# Method 3: Try from string with cleanup
if PUMPFUN_PROGRAM_ID is None:
    try:
        # Clean the string of any potential issues
        program_id_str = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        # Remove any whitespace, newlines, or invisible characters
        program_id_str = ''.join(c for c in program_id_str if c.isalnum())
        PUMPFUN_PROGRAM_ID = Pubkey.from_string(program_id_str)
        logging.info(f"[PumpFun] Program ID loaded from string: {str(PUMPFUN_PROGRAM_ID)[:8]}...")
    except Exception as e:
        logging.error(f"[PumpFun] All methods failed to load program ID: {e}")
        # Last resort: use a placeholder that will fail gracefully
        PUMPFUN_PROGRAM_ID = None

# Verify we have a valid program ID
if PUMPFUN_PROGRAM_ID is None:
    logging.error("[PumpFun] CRITICAL: No valid program ID loaded!")
else:
    logging.info(f"[PumpFun] Program ID ready: {str(PUMPFUN_PROGRAM_ID)}")

# PumpFun Program Constants
PUMPFUN_GLOBAL_STATE_SEED = b"global"
PUMPFUN_BONDING_CURVE_SEED = b"bonding-curve"
PUMPFUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbdZzAhmCgAdBx")  # PumpFun fee account

# Buy instruction discriminator (from PumpFun IDL)
BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])  # buy instruction

async def derive_pumpfun_pdas(mint: Pubkey) -> Dict[str, Pubkey]:
    """Derive PumpFun PDAs for the given mint"""
    try:
        if PUMPFUN_PROGRAM_ID is None:
            raise Exception("PumpFun program ID not loaded")
            
        # Derive global state PDA
        global_state, _ = Pubkey.find_program_address(
            [PUMPFUN_GLOBAL_STATE_SEED],
            PUMPFUN_PROGRAM_ID
        )
        
        # Derive bonding curve PDA
        bonding_curve, _ = Pubkey.find_program_address(
            [PUMPFUN_BONDING_CURVE_SEED, bytes(mint)],
            PUMPFUN_PROGRAM_ID
        )
        
        # Derive associated bonding curve token account
        bonding_curve_ata = get_associated_token_address(
            bonding_curve,
            mint
        )
        
        return {
            "global_state": global_state,
            "bonding_curve": bonding_curve,
            "bonding_curve_ata": bonding_curve_ata
        }
    except Exception as e:
        logging.error(f"[PumpFun] Failed to derive PDAs: {e}")
        raise

async def execute_pumpfun_buy(
    mint: str,
    sol_amount: float,
    slippage_bps: int = 2000,
    priority_fee_lamports: int = None,
    cu_limit: int = None
) -> Dict[str, Any]:
    """
    Execute a buy on PumpFun bonding curve with proper token program detection and account setup
    """
    from utils import keypair, rpc, cleanup_wsol_on_failure  # lazy import

    try:
        # Check if program ID is loaded
        if PUMPFUN_PROGRAM_ID is None:
            logging.error("[PumpFun] Program ID not loaded, cannot execute buy")
            return {"ok": False, "reason": "PROGRAM_ID_ERROR", "sig": None, "tokens_received": 0}
            
        logging.info(f"[PumpFun] Starting buy for {mint[:8]}... with {sol_amount:.4f} SOL")

        # Validate mint address format before proceeding
        mint_str = str(mint).strip()
        if len(mint_str) < 43 or len(mint_str) > 44:
            logging.error(f"[PumpFun] Invalid mint address length: {len(mint_str)}")
            return {"ok": False, "reason": "INVALID_MINT", "sig": None, "tokens_received": 0}

        mint_pubkey = Pubkey.from_string(mint_str)
        pdas = await derive_pumpfun_pdas(mint_pubkey)

        # Detect which token program owns the mint (SPL vs Token-2022)
        mint_acc = rpc.get_account_info(mint_pubkey)
        if not (mint_acc and mint_acc.value):
            logging.error("[PumpFun] Mint account not found")
            return {"ok": False, "reason": "MINT_NOT_FOUND", "sig": None, "tokens_received": 0}
        mint_owner = str(mint_acc.value.owner)
        token_prog = TOKEN_2022_PROGRAM_ID if mint_owner == str(TOKEN_2022_PROGRAM_ID) else TOKEN_PROGRAM_ID

        buyer_ata = get_associated_token_address(keypair.pubkey(), mint_pubkey)

        instructions = []

        # Compute budget: use hardcoded values or config defaults
        cu_limit = cu_limit or 1000000  # Hardcoded default
        priority_fee_lamports = priority_fee_lamports or 1000000  # Hardcoded default
        
        instructions.append(set_compute_unit_limit(cu_limit))
        micro_lamports_per_cu = max(1, int(priority_fee_lamports / cu_limit))
        instructions.append(set_compute_unit_price(micro_lamports_per_cu))

        # Ensure buyer ATA exists
        try:
            ata_info = rpc.get_account_info(buyer_ata)
            if not (ata_info and ata_info.value):
                logging.info("[PumpFun] Creating buyer token account...")
                instructions.append(create_associated_token_account(
                    payer=keypair.pubkey(),
                    owner=keypair.pubkey(),
                    mint=mint_pubkey
                ))
        except Exception as e:
            logging.debug(f"[PumpFun] Buyer ATA precheck error: {e}")

        # Ensure bonding-curve ATA exists (owner = bonding_curve PDA)
        try:
            bc_ata_info = rpc.get_account_info(pdas["bonding_curve_ata"])
            if not (bc_ata_info and bc_ata_info.value):
                logging.info("[PumpFun] Creating bonding curve ATA...")
                instructions.append(create_associated_token_account(
                    payer=keypair.pubkey(),
                    owner=pdas["bonding_curve"],
                    mint=mint_pubkey
                ))
        except Exception as e:
            logging.debug(f"[PumpFun] BC ATA precheck error: {e}")

        amount_lamports = int(sol_amount * 1e9)
        min_tokens_out = 0  # accept any for now (you can refine later)

        # buy(mint, amount, min_out) layout: discriminator + u64 + u64
        instruction_data = (
            BUY_DISCRIMINATOR +
            amount_lamports.to_bytes(8, "little") +
            min_tokens_out.to_bytes(8, "little")
        )

        accounts = [
            AccountMeta(pdas["global_state"], False, False),
            AccountMeta(PUMPFUN_FEE_RECIPIENT, False, True),
            AccountMeta(mint_pubkey, False, True),
            AccountMeta(pdas["bonding_curve"], False, True),
            AccountMeta(pdas["bonding_curve_ata"], False, True),
            AccountMeta(buyer_ata, False, True),
            AccountMeta(keypair.pubkey(), True, True),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(token_prog, False, False),   # << correct program (SPL or Token-2022)
            AccountMeta(RENT, False, False),
            AccountMeta(SYSVAR_CLOCK, False, False),
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),
            AccountMeta(SYSVAR_INSTRUCTIONS, False, False),
        ]

        # Skip referrer for now - can add later if needed

        buy_ix = Instruction(program_id=PUMPFUN_PROGRAM_ID, data=instruction_data, accounts=accounts)
        instructions.append(buy_ix)

        # Build & (optionally) simulate
        recent_blockhash = rpc.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash,
        )
        tx = VersionedTransaction(msg, [keypair])

        # Skip simulation for speed in hardcoded version
        simulate = getattr(CONFIG, 'SIMULATE_BEFORE_SEND', False)
        if simulate:
            logging.info("[PumpFun] Simulating transaction...")
            sim_result = rpc.simulate_transaction(tx, commitment=Confirmed)
            if sim_result.value and sim_result.value.err:
                # Check logs for specific error
                logs = []
                try:
                    logs = list(sim_result.value.logs or [])
                except Exception:
                    pass
                joined = "\n".join(logs)
                if "ProgramAccountNotFound" in joined:
                    logging.error("[PumpFun] Simulation failed: ProgramAccountNotFound")
                    return {"ok": False, "reason": "ProgramAccountNotFound", "sig": None, "tokens_received": 0}
                logging.error(f"[PumpFun] Simulation failed: {sim_result.value.err}")
                return {"ok": False, "reason": "SIM_FAIL", "sig": None, "tokens_received": 0}
            logging.info("[PumpFun] Simulation passed")

        # Send
        logging.info("[PumpFun] Sending transaction...")
        result = rpc.send_transaction(
            tx,
            opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3)
        )
        if not result.value:
            logging.error("[PumpFun] Failed to send transaction")
            return {"ok": False, "reason": "SEND_ERR", "sig": None, "tokens_received": 0}

        sig = str(result.value)
        logging.info(f"[PumpFun] Transaction sent: {sig}")

        # Quick balance probe (non-blocking)
        await asyncio.sleep(2)
        tokens_received = 0
        for _ in range(10):  # ~3s
            try:
                bal = rpc.get_token_account_balance(buyer_ata)
                if bal and bal.value:
                    tokens_received = int(bal.value.amount)
                    if tokens_received > 0:
                        logging.info(f"[PumpFun] Received {tokens_received} tokens")
                        break
            except:
                pass
            await asyncio.sleep(0.3)

        return {"ok": True, "sig": sig, "tokens_received": tokens_received, "reason": "OK"}

    except Exception as e:
        logging.error(f"[PumpFun] Buy execution error: {e}")
        await cleanup_wsol_on_failure()

        es = str(e).lower()
        if "insufficient" in es or "balance" in es:
            reason = "INSUFFICIENT_BALANCE"
        elif "slippage" in es:
            reason = "SLIPPAGE"
        else:
            reason = "BUILD_FAILED"

        return {"ok": False, "reason": reason, "sig": None, "tokens_received": 0}
