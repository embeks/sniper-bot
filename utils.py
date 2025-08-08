# Add these imports at the top of utils.py
import base64
from typing import Optional
from solders.transaction import VersionedTransaction

# Add these new functions to utils.py (don't remove anything, just add):

async def get_jupiter_quote(input_mint: str, output_mint: str, amount: int, slippage_bps: int = 100):
    """Get swap quote from Jupiter API"""
    try:
        url = "https://quote-api.jup.ag/v6/quote"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false"
        }
        
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params)
            if response.status_code == 200:
                quote = response.json()
                logging.info(f"[Jupiter] Got quote: {amount/1e9:.4f} SOL -> {float(quote.get('outAmount', 0))/1e9:.4f} tokens")
                return quote
            else:
                logging.warning(f"[Jupiter] Quote request failed: {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"[Jupiter] Error getting quote: {e}")
        return None

async def get_jupiter_swap_transaction(quote: dict, user_pubkey: str):
    """Get the swap transaction from Jupiter"""
    try:
        url = "https://quote-api.jup.ag/v6/swap"
        
        body = {
            "quoteResponse": quote,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,  # Handles WSOL automatically!
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": "auto"
        }
        
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, json=body)
            if response.status_code == 200:
                data = response.json()
                logging.info("[Jupiter] Swap transaction received")
                return data
            else:
                logging.warning(f"[Jupiter] Swap request failed: {response.status_code}")
                return None
    except Exception as e:
        logging.error(f"[Jupiter] Error getting swap transaction: {e}")
        return None

async def execute_jupiter_swap(mint: str, amount_lamports: int) -> Optional[str]:
    """Execute a swap using Jupiter"""
    try:
        input_mint = "So11111111111111111111111111111111111111112"  # SOL
        output_mint = mint
        
        # Step 1: Get quote
        logging.info(f"[Jupiter] Getting quote for {amount_lamports/1e9:.4f} SOL -> {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount_lamports)
        if not quote:
            logging.warning("[Jupiter] Failed to get quote")
            return None
        
        # Check if output amount is reasonable
        out_amount = int(quote.get("outAmount", 0))
        if out_amount == 0:
            logging.warning("[Jupiter] Quote returned zero output amount")
            return None
        
        # Step 2: Get swap transaction
        logging.info("[Jupiter] Building swap transaction...")
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data:
            logging.warning("[Jupiter] Failed to get swap transaction")
            return None
        
        # Step 3: Deserialize and sign transaction
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)
        
        # Sign with our keypair
        tx.sign([keypair])
        
        # Step 4: Send transaction
        logging.info("[Jupiter] Sending transaction...")
        result = rpc.send_transaction(
            tx,
            opts=TxOpts(
                skip_preflight=True,
                preflight_commitment=Confirmed,
                max_retries=3
            )
        )
        
        if result.value:
            sig = str(result.value)
            logging.info(f"[Jupiter] Transaction sent: {sig}")
            return sig
        else:
            logging.error("[Jupiter] Failed to send transaction")
            return None
            
    except Exception as e:
        logging.error(f"[Jupiter] Swap execution error: {e}")
        import traceback
        logging.error(traceback.format_exc())
        return None

# REPLACE your existing buy_token function with this enhanced version:
async def buy_token(mint: str):
    """Execute buy transaction for a token - NOW WITH JUPITER!"""
    amount = int(BUY_AMOUNT_SOL * 1e9)  # Convert SOL to lamports

    try:
        if mint in BROKEN_TOKENS:
            await send_telegram_alert(f"❌ Skipped {mint} — broken token")
            log_skipped_token(mint, "Broken token")
            record_skip("malformed")
            return False

        increment_stat("snipes_attempted", 1)
        update_last_activity()

        # ========== TRY JUPITER FIRST (works for 95% of tokens) ==========
        logging.info(f"[Buy] Attempting Jupiter swap for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_swap(mint, amount)
        
        if jupiter_sig:
            await send_telegram_alert(
                f"✅ Sniped {mint} via Jupiter — bought with {BUY_AMOUNT_SOL} SOL\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            
            OPEN_POSITIONS[mint] = {
                "expected_token_amount": 0,
                "buy_amount_sol": BUY_AMOUNT_SOL,
                "sold_stages": set(),
                "buy_sig": jupiter_sig
            }
            
            increment_stat("snipes_succeeded", 1)
            log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
            return True
        
        # ========== FALLBACK TO RAYDIUM (for new tokens not on Jupiter yet) ==========
        logging.info(f"[Buy] Jupiter failed, trying Raydium for {mint[:8]}...")
        
        input_mint = "So11111111111111111111111111111111111111112"  # SOL
        output_mint = mint
        
        # Check if pool exists
        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            # Try reverse direction
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No pool found on Jupiter or Raydium for {mint}. Skipping.")
                log_skipped_token(mint, "No pool on Jupiter or Raydium")
                record_skip("malformed")
                return False

        # Build swap transaction
        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build Raydium swap TX for {mint}")
            mark_broken_token(mint, 0)
            return False

        # Send transaction
        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"📉 Trade failed — TX send error for {mint}")
            mark_broken_token(mint, 0)
            return False

        await send_telegram_alert(
            f"✅ Sniped {mint} via Raydium — bought with {BUY_AMOUNT_SOL} SOL\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        
        OPEN_POSITIONS[mint] = {
            "expected_token_amount": 0,
            "buy_amount_sol": BUY_AMOUNT_SOL,
            "sold_stages": set(),
            "buy_sig": sig
        }
        
        increment_stat("snipes_succeeded", 1)
        log_trade(mint, "BUY", BUY_AMOUNT_SOL, 0)
        return True

    except Exception as e:
        await send_telegram_alert(f"❌ Buy failed for {mint}: {e}")
        log_skipped_token(mint, f"Buy failed: {e}")
        return False

# Similarly, enhance sell_token with Jupiter:
async def execute_jupiter_sell(mint: str, amount: int) -> Optional[str]:
    """Execute a sell using Jupiter"""
    try:
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"  # SOL
        
        # Get quote
        logging.info(f"[Jupiter] Getting sell quote for {mint[:8]}...")
        quote = await get_jupiter_quote(input_mint, output_mint, amount)
        if not quote:
            return None
        
        # Get swap transaction
        swap_data = await get_jupiter_swap_transaction(quote, wallet_pubkey)
        if not swap_data:
            return None
        
        # Sign and send
        tx_bytes = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(tx_bytes)
        tx.sign([keypair])
        
        result = rpc.send_transaction(
            tx,
            opts=TxOpts(
                skip_preflight=True,
                preflight_commitment=Confirmed,
                max_retries=3
            )
        )
        
        if result.value:
            return str(result.value)
        return None
            
    except Exception as e:
        logging.error(f"[Jupiter] Sell execution error: {e}")
        return None

async def sell_token(mint: str, percent: float = 100.0):
    """Execute sell transaction for a token - NOW WITH JUPITER!"""
    try:
        # Get token balance
        owner = keypair.pubkey()
        token_account = get_associated_token_address(owner, Pubkey.from_string(mint))
        
        # Get actual balance
        response = rpc.get_token_account_balance(token_account)
        if not response.value:
            await send_telegram_alert(f"⚠️ No token balance found for {mint}")
            return False
        
        balance = int(response.value.amount)
        amount = int(balance * percent / 100)
        
        if amount == 0:
            await send_telegram_alert(f"⚠️ Zero balance to sell for {mint}")
            return False

        # ========== TRY JUPITER FIRST ==========
        logging.info(f"[Sell] Attempting Jupiter sell for {mint[:8]}...")
        jupiter_sig = await execute_jupiter_sell(mint, amount)
        
        if jupiter_sig:
            await send_telegram_alert(
                f"✅ Sold {percent}% of {mint} via Jupiter\n"
                f"TX: https://solscan.io/tx/{jupiter_sig}"
            )
            log_trade(mint, f"SELL {percent}%", 0, amount)
            return True
        
        # ========== FALLBACK TO RAYDIUM ==========
        logging.info(f"[Sell] Jupiter failed, trying Raydium for {mint[:8]}...")
        
        input_mint = mint
        output_mint = "So11111111111111111111111111111111111111112"  # SOL
        
        # Find pool
        pool = raydium.find_pool(input_mint, output_mint)
        if not pool:
            pool = raydium.find_pool(output_mint, input_mint)
            if not pool:
                await send_telegram_alert(f"⚠️ No pool for {mint}. Cannot sell.")
                log_skipped_token(mint, "No pool for sell")
                return False

        # Build and send transaction
        tx = raydium.build_swap_transaction(keypair, input_mint, output_mint, amount)
        if not tx:
            await send_telegram_alert(f"❌ Failed to build sell TX for {mint}")
            return False

        sig = raydium.send_transaction(tx, keypair)
        if not sig:
            await send_telegram_alert(f"❌ Failed to send sell tx for {mint}")
            return False

        await send_telegram_alert(
            f"✅ Sold {percent}% of {mint} via Raydium\n"
            f"TX: https://solscan.io/tx/{sig}"
        )
        log_trade(mint, f"SELL {percent}%", 0, amount)
        return True
        
    except Exception as e:
        await send_telegram_alert(f"❌ Sell failed for {mint}: {e}")
        log_skipped_token(mint, f"Sell failed: {e}")
        return False
