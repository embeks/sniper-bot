# pumpfun_buy.py - PumpFun Direct Buy on Bonding Curve
import logging
import asyncio
import base64
from typing import Dict, Any, Optional
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.sysvar import RENT
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

# Import config
import config

# Load config
CONFIG = config.load()

# PumpFun Program Constants
PUMPFUN_PROGRAM_ID = Pubkey.from_string(getattr(CONFIG, "PUMPFUN_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwusU"))
PUMPFUN_GLOBAL_STATE_SEED = b"global"
PUMPFUN_BONDING_CURVE_SEED = b"bonding-curve"
PUMPFUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbdZzAhmCgAdBx")  # PumpFun fee account

# Buy instruction discriminator (from PumpFun IDL)
BUY_DISCRIMINATOR = bytes([102, 6, 61, 18, 1, 218, 235, 234])  # buy instruction

async def derive_pumpfun_pdas(mint: Pubkey) -> Dict[str, Pubkey]:
    """Derive PumpFun PDAs for the given mint"""
    try:
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
    priority_fee_lamports: int = 500000,
    cu_limit: int = 1_000_000
) -> Dict[str, Any]:
    """
    Execute a buy on PumpFun bonding curve
    
    Args:
        mint: Token mint address
        sol_amount: Amount of SOL to spend
        slippage_bps: Slippage in basis points (not used currently)
        priority_fee_lamports: Priority fee in lamports
        cu_limit: Compute unit limit
        
    Returns:
        Dict with:
        - ok: bool - success flag
        - sig: str - transaction signature (if successful)
        - tokens_received: int - amount of tokens received
        - reason: str - error reason if failed
    """
    try:
        logging.info(f"[PumpFun] Starting buy for {mint[:8]}... with {sol_amount:.4f} SOL")
        
        # Convert mint string to Pubkey
        mint_pubkey = Pubkey.from_string(mint)
        
        # Derive PDAs
        pdas = await derive_pumpfun_pdas(mint_pubkey)
        
        # Get buyer's associated token account
        buyer_ata = get_associated_token_address(keypair.pubkey(), mint_pubkey)
        
        # Check if buyer ATA exists, create if not
        ata_exists = False
        try:
            ata_info = rpc.get_account_info(buyer_ata)
            if ata_info and ata_info.value:
                ata_exists = True
        except:
            pass
        
        instructions = []
        
        # Add compute budget instructions
        instructions.append(set_compute_unit_limit(cu_limit))
        # Convert priority_fee_lamports to micro-lamports per CU
        priority_fee_per_cu = priority_fee_lamports // 1000  # Rough conversion
        instructions.append(set_compute_unit_price(priority_fee_per_cu))
        
        # Create ATA if needed
        if not ata_exists:
            logging.info(f"[PumpFun] Creating buyer token account...")
            instructions.append(
                create_associated_token_account(
                    payer=keypair.pubkey(),
                    owner=keypair.pubkey(),
                    mint=mint_pubkey
                )
            )
        
        # Convert SOL amount to lamports
        amount_lamports = int(sol_amount * 1e9)
        
        # Calculate minimum tokens out (with slippage tolerance)
        # This is a simplified calculation - ideally fetch from bonding curve state
        min_tokens_out = 0  # Accept any amount for now
        
        # Build buy instruction data
        # Layout: [discriminator(8)] + [amount(8)] + [min_tokens_out(8)]
        instruction_data = (
            BUY_DISCRIMINATOR +
            amount_lamports.to_bytes(8, 'little') +
            min_tokens_out.to_bytes(8, 'little')
        )
        
        # Build buy instruction accounts
        accounts = [
            AccountMeta(pdas["global_state"], False, False),  # Global state
            AccountMeta(PUMPFUN_FEE_RECIPIENT, False, True),   # Fee recipient
            AccountMeta(mint_pubkey, False, False),            # Mint
            AccountMeta(pdas["bonding_curve"], False, True),   # Bonding curve
            AccountMeta(pdas["bonding_curve_ata"], False, True),  # Bonding curve ATA
            AccountMeta(buyer_ata, False, True),               # Buyer ATA
            AccountMeta(keypair.pubkey(), True, True),         # Buyer (signer + writable)
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),      # System program
            AccountMeta(TOKEN_PROGRAM_ID, False, False),       # Token program
            AccountMeta(RENT, False, False),                   # Rent sysvar
            AccountMeta(Pubkey.from_string("SysvarC1ock11111111111111111111111111111111"), False, False),  # Clock
            AccountMeta(ASSOCIATED_TOKEN_PROGRAM_ID, False, False),  # Associated token program
        ]
        
        # Add referrer if configured
        referrer = getattr(CONFIG, "PUMPFUN_REFERRER", "")
        if referrer:
            try:
                referrer_pubkey = Pubkey.from_string(referrer)
                accounts.append(AccountMeta(referrer_pubkey, False, True))
                logging.info(f"[PumpFun] Using referrer: {referrer[:8]}...")
            except:
                logging.debug("[PumpFun] Invalid referrer address, skipping")
        
        # Create buy instruction
        buy_instruction = Instruction(
            program_id=PUMPFUN_PROGRAM_ID,
            data=instruction_data,
            accounts=accounts
        )
        
        instructions.append(buy_instruction)
        
        # Build and sign transaction
        recent_blockhash = rpc.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(
            payer=keypair.pubkey(),
            instructions=instructions,
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash,
        )
        
        tx = VersionedTransaction(msg, [keypair])
        
        # Simulate if configured
        if CONFIG.SIMULATE_BEFORE_SEND:
            logging.info("[PumpFun] Simulating transaction...")
            sim_result = rpc.simulate_transaction(tx, commitment=Confirmed)
            if sim_result.value and sim_result.value.err:
                logging.error(f"[PumpFun] Simulation failed: {sim_result.value.err}")
                return {"ok": False, "reason": "SIM_FAIL", "sig": None, "tokens_received": 0}
            logging.info("[PumpFun] Simulation passed")
        
        # Send transaction
        logging.info("[PumpFun] Sending transaction...")
        result = rpc.send_transaction(
            tx,
            opts=TxOpts(
                skip_preflight=True,
                preflight_commitment=Confirmed,
                max_retries=3
            )
        )
        
        if not result.value:
            logging.error("[PumpFun] Failed to send transaction")
            return {"ok": False, "reason": "SEND_ERR", "sig": None, "tokens_received": 0}
        
        sig = str(result.value)
        logging.info(f"[PumpFun] Transaction sent: {sig}")
        
        # Wait for confirmation and get token balance
        await asyncio.sleep(2)
        
        # Poll for token balance
        tokens_received = 0
        for retry in range(10):  # Try for ~3 seconds
            try:
                response = rpc.get_token_account_balance(buyer_ata)
                if response and response.value:
                    tokens_received = int(response.value.amount)
                    if tokens_received > 0:
                        logging.info(f"[PumpFun] Received {tokens_received} tokens")
                        break
            except:
                pass
            await asyncio.sleep(0.3)
        
        if tokens_received == 0:
            logging.warning("[PumpFun] No tokens received after 3 seconds")
            # Transaction might still be pending, return success with sig
            return {"ok": True, "sig": sig, "tokens_received": 0, "reason": "OK"}
        
        return {
            "ok": True,
            "sig": sig,
            "tokens_received": tokens_received,
            "reason": "OK"
        }
        
    except Exception as e:
        logging.error(f"[PumpFun] Buy execution error: {e}")
        
        # Clean up any stranded WSOL
        await cleanup_wsol_on_failure()
        
        # Determine error reason
        error_str = str(e)
        if "insufficient" in error_str.lower() or "balance" in error_str.lower():
            reason = "INSUFFICIENT_BALANCE"
        elif "slippage" in error_str.lower():
            reason = "SLIPPAGE"
        else:
            reason = "BUILD_FAILED"
        
        return {
            "ok": False,
            "reason": reason,
            "sig": None,
            "tokens_received": 0
        }
