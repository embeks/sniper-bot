# pumpfun_buy.py
import asyncio
import base64
import logging
from typing import Optional, Dict
from solders.transaction import VersionedTransaction
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.api import Client
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from spl.token.instructions import get_associated_token_address
from spl.token.constants import WRAPPED_SOL_MINT, TOKEN_PROGRAM_ID
from solders.message import MessageV0
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

import config

CONFIG = config.load()
rpc = Client(CONFIG.RPC_URL, commitment=Confirmed)

# We get keypair from utils (inject or re-import if circular). Easiest: pass keypair in if needed.
from utils import keypair, cleanup_wsol_on_failure

async def execute_pumpfun_buy(mint: str, sol_amount: float, *, cu_limit: int, priority_fee: int) -> Dict[str, Optional[str]]:
    """
    Returns: {"ok": bool, "sig": Optional[str], "tokens_received": int, "reason": str}
    """
    try:
        owner = keypair.pubkey()
        mint_pk = Pubkey.from_string(mint)
        token_ata = get_associated_token_address(owner, mint_pk)

        # Ensure WSOL ATA if PF expects WSOL; otherwise we can pay lamports directly.
        # Claude: Verify PF buy flow (WSOL vs SOL from payer) and implement the correct path.
        wsol_ata = get_associated_token_address(owner, WRAPPED_SOL_MINT)

        # 1) (If required) wrap SOL into WSOL ATA for input
        # Claude: implement ensure_wsol_ata(...) and wrap lamports=int(sol_amount*1e9)

        # 2) Build PF buy ix
        # Claude: implement build_pumpfun_buy_ix(...) with the correct program id and accounts
        pf_ix = await build_pumpfun_buy_ix(
            mint_pk,
            payer=owner,
            wsol_ata=wsol_ata,
            token_ata=token_ata,
            lamports=int(sol_amount * 1e9)
        )

        # 3) Compute budget + priority fee
        cu_ix = set_compute_unit_limit(cu_limit)
        fee_ix = set_compute_unit_price(priority_fee)

        # 4) Compile + sign
        recent_blockhash = rpc.get_latest_blockhash().value.blockhash
        msg = MessageV0.try_compile(
            payer=owner,
            instructions=[cu_ix, fee_ix, pf_ix],
            address_lookup_table_accounts=[],
            recent_blockhash=recent_blockhash,
        )
        tx = VersionedTransaction(msg, [keypair])

        # 5) (Optional) simulate first if CONFIG.SIMULATE_BEFORE_SEND
        if CONFIG.SIMULATE_BEFORE_SEND:
            sim = rpc.simulate_transaction(tx, commitment=Confirmed)
            if not sim or not sim.value or sim.value.err:
                return {"ok": False, "sig": None, "tokens_received": 0, "reason": f"SIM_FAIL:{sim.value.err if sim and sim.value else 'no_result'}"}

        # 6) Send
        res = rpc.send_transaction(tx, opts=TxOpts(skip_preflight=True, preflight_commitment=Confirmed, max_retries=3))
        if not res.value:
            # cleanup WSOL on failure
            await cleanup_wsol_on_failure()
            return {"ok": False, "sig": None, "tokens_received": 0, "reason": "SEND_ERR"}

        sig = str(res.value)

        # 7) Probe status (non-blocking)
        await asyncio.sleep(2)

        # 8) Poll for real balance
        tokens = await fetch_real_token_balance(owner, mint_pk, retries=10)
        return {"ok": tokens > 0, "sig": sig, "tokens_received": tokens, "reason": "OK" if tokens > 0 else "NO_BAL"}

    except Exception as e:
        logging.error(f"[PF BUY] error for {mint}: {e}")
        try:
            await cleanup_wsol_on_failure()
        except Exception:
            pass
        return {"ok": False, "sig": None, "tokens_received": 0, "reason": "EXC"}

async def build_pumpfun_buy_ix(mint: Pubkey, payer: Pubkey, wsol_ata: Pubkey, token_ata: Pubkey, lamports: int):
    """
    Claude: Fill this with the real PumpFun buy instruction.
    - Get PROGRAM_ID from CONFIG.PUMPFUN_PROGRAM_ID
    - Derive any required PDAs (bonding curve, global state, etc.)
    - Include optional referrer from CONFIG.PUMPFUN_REFERRER if supported
    - Construct the correct instruction data payload
    - Return a `solders.instruction.Instruction`
    """
    raise NotImplementedError("Claude: implement PF buy instruction")

async def fetch_real_token_balance(owner: Pubkey, mint: Pubkey, retries=10) -> int:
    ata = get_associated_token_address(owner, mint)
    for _ in range(retries):
        try:
            resp = rpc.get_token_account_balance(ata)
            if resp and resp.value and resp.value.amount:
                return int(resp.value.amount)
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return 0
