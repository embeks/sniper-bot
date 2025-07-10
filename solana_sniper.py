import os
import time
import json
from datetime import datetime
from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.transaction import Transaction
from solana.system_program import TransferParams, transfer
from solana.keypair import Keypair
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

from price_utils import get_token_price, get_token_liquidity
from utils import (
    send_telegram_alert,
    is_contract_verified,
    has_blacklist_or_mint_functions,
    is_lp_locked_or_burned,
    check_token_owner_permissions,
    simulate_sell,
    is_blacklisted,
    fetch_token_metadata
)

# Load private key
solana_key_str = os.getenv("SOLANA_PRIVATE_KEY")
if not solana_key_str:
    raise Exception("❌ SOLANA_PRIVATE_KEY not set in environment!")
solana_private_key = json.loads(solana_key_str)
keypair = Keypair.from_secret_key(bytes(solana_private_key))
wallet_public_key = keypair.public_key
client = Client("https://api.mainnet-beta.solana.com")

# Tracking for sells
tracked_tokens = {}
PARTIAL_SELLS = {"2x": 0.33, "5x": 0.33, "10x": 1.0}
TIMEOUT_SELL_SECONDS = 300
RUG_THRESHOLD = 0.75

# Sniping time filter (optional)
def is_sniping_window():
    current_hour = datetime.now().hour
    return 10 <= current_hour <= 14 or 1 <= current_hour <= 5

# Pre-buy checks

def should_buy(token_data):
    if token_data['liquidity'] < 2000:
        return False
    if is_blacklisted(token_data['creator_wallet']):
        return False
    if not simulate_sell(token_data['token_address']):
        return False
    if token_data['owner_permissions'].get('can_disable_trading') or token_data['owner_permissions'].get('can_drain_lp'):
        return False
    if token_data['pair_program'] not in ['raydium', 'jupiter']:
        return False
    if token_data['buys_in_first_5s'] < 10 or token_data['volume'] < 1.0:
        return False
    return True

# Sell logic

def sell_token(token_address, percentage=1.0):
    try:
        token_pubkey = PublicKey(token_address)
        tx = Transaction()
        tx.add(
            transfer(
                TransferParams(
                    from_pubkey=wallet_public_key,
                    to_pubkey=token_pubkey,
                    lamports=int(500_000 * percentage)  # Dummy logic
                )
            )
        )
        resp = client.send_transaction(
            tx, keypair, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )
        send_telegram_alert(f"[💰] Sell TX sent — {resp['result']}")
    except Exception as e:
        send_telegram_alert(f"[‼️] Sell failed: {e}")

# Token tracker

def track_token(token_address, buy_price, initial_liquidity, symbol):
    tracked_tokens[token_address] = {
        "buy_price": buy_price,
        "initial_liquidity": initial_liquidity,
        "buy_time": time.time(),
        "sells": set(),
        "symbol": symbol
    }
    send_telegram_alert(f"✅ Tracking {symbol} at ${buy_price:.6f}")

# Monitor and exit strategy

def monitor_tokens():
    for token_address, data in list(tracked_tokens.items()):
        current_price = get_token_price(token_address)
        current_liquidity = get_token_liquidity(token_address)
        buy_price = data["buy_price"]
        sells = data["sells"]

        if current_price >= 2 * buy_price and "2x" not in sells:
            sell_token(token_address, 0.33)
            sells.add("2x")
        elif current_price >= 5 * buy_price and "5x" not in sells:
            sell_token(token_address, 0.33)
            sells.add("5x")
        elif current_price >= 10 * buy_price and "10x" not in sells:
            sell_token(token_address, 1.0)
            sells.add("10x")

        if time.time() - data["buy_time"] > TIMEOUT_SELL_SECONDS and "timeout" not in sells:
            sell_token(token_address, 1.0)
            sells.add("timeout")

        if current_liquidity < data["initial_liquidity"] * RUG_THRESHOLD and "rug" not in sells:
            sell_token(token_address, 1.0)
            sells.add("rug")

        send_telegram_alert(f"✅ Sold {data['symbol']}\nROI: {current_price / buy_price:.2f}x\nHeld: {int(time.time() - data['buy_time'])}s\nTriggers: {', '.join(sells)}")

# Token buying

def buy_token(token_address):
    if not is_sniping_window():
        return

    token_data = fetch_token_metadata(token_address)
    if not should_buy(token_data):
        return

    send_telegram_alert(f"""
👀 [NEW TOKEN ALERT]

• Token: {token_data['name']}
• LP: {token_data['liquidity']} SOL
• Raydium/Jupiter: ✅
• Blacklist: ❌
• Honeypot: ❌
• Volume: {token_data['volume']} SOL
• Buys in 5s: {token_data['buys_in_first_5s']}

Auto-sniping in 3 seconds... 🚀
""")
    time.sleep(3)

    entry_price = get_token_price(token_address)
    liquidity = get_token_liquidity(token_address)

    tx = Transaction()
    tx.add(
        transfer(
            TransferParams(
                from_pubkey=wallet_public_key,
                to_pubkey=PublicKey(token_address),
                lamports=int(0.01 * 1_000_000_000)
            )
        )
    )
    resp = client.send_transaction(
        tx, keypair, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
    )

    send_telegram_alert(f"✅ Buy TX: {resp['result']}")
    track_token(token_address, entry_price, liquidity, token_data['symbol'])

# Main loop

if __name__ == "__main__":
    send_telegram_alert("✅ Sniper launched — scanning...")
    while True:
        # This should be connected to real-time feed
        dummy_token = "So11111111111111111111111111111111111111112"
        buy_token(dummy_token)
        monitor_tokens()
        time.sleep(15)
