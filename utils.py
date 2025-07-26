import os
import time
import json
import base64
import logging
import aiofiles
import asyncio
import traceback
from dotenv import load_dotenv
from datetime import datetime
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from typing import Optional

from jupiter_aggregator import JupiterAggregatorClient

load_dotenv()

# === ENV & GLOBALS ===
RPC_URL = os.getenv("RPC_URL")
BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", 0.1))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", 100))
PRIVATE_KEY_ARRAY = json.loads(os.getenv("SOLANA_PRIVATE_KEY"))

keypair = Keypair.from_bytes(bytes(PRIVATE_KEY_ARRAY))
wallet_pubkey = keypair.pubkey()
client = Client(RPC_URL)
aggregator = JupiterAggregatorClient(RPC_URL)

# === LOGGER ===
logging.basicConfig(level=logging.INFO)
def log(msg):
    logging.info(msg)

def log_and_alert(msg):
    log(msg)
    # You can optionally add Telegram alert logic here too

# === BUY FUNCTION ===
async def buy_token(mint: str):
    try:
        input_mint = Pubkey.from_string("So11111111111111111111111111111111111111112")
        output_mint = Pubkey.from_string(mint)

        # Convert amount to lamports
        lamports = int(BUY_AMOUNT_SOL * 1_000_000_000)

        log_and_alert(f"\ud83d\udd34 Force buying: {mint}")

        quote = await aggregator.get_quote(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=lamports,
            slippage_bps=SLIPPAGE_BPS,
            user_pubkey=wallet_pubkey
        )

        if not quote:
            log_and_alert(f"\u274c Failed to get quote for {mint}")
            return

        log_and_alert(f"\u2705 Quote received. Building swap for {mint}")

        tx_base64 = await aggregator.get_swap_transaction(quote, keypair)
        if not tx_base64:
            log_and_alert(f"\u274c Failed to build swap transaction for {mint}")
            return

        signed_tx = aggregator.build_swap_transaction(tx_base64, keypair)
        if not signed_tx:
            log_and_alert(f"\u274c Failed to create signed transaction for {mint}")
            return

        tx_sig = aggregator.send_transaction(signed_tx, keypair)
        if not tx_sig:
            log_and_alert(f"\u274c Failed to send transaction for {mint}")
            return

        log_and_alert(f"\u2705 Swap transaction sent for {mint}: {tx_sig}")

    except Exception as e:
        tb = traceback.format_exc()
        log_and_alert(f"\u274c Exception while buying {mint}: {str(e)}\n{tb}")

# === FOR TESTING ===
if __name__ == "__main__":
    asyncio.run(buy_token("DUSTawucrTsGU8hcqRdHDCbuYhCPADMLM2VcCb8VnFnQ"))
