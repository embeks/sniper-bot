import requests
import os

BIRDEYE_API_KEY = os.getenv("BIRDEYE_API_KEY")

# 📈 Get current token price
def get_token_price(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/price/token_price?address={token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        data = res.json().get("data", {})
        price = data.get(token_address, {}).get("value", 0)
        return float(price)
    except Exception as e:
        print(f"[!] Failed to fetch price: {e}")
        return 0

# 💧 Get current token liquidity
def get_token_liquidity(token_address):
    try:
        url = f"https://public-api.birdeye.so/public/token/{token_address}"
        headers = {"X-API-KEY": BIRDEYE_API_KEY}
        res = requests.get(url, headers=headers)
        res.raise_for_status()
        data = res.json().get("data", {})
        liquidity = data.get("liquidity", 0)
        return float(liquidity)
    except Exception as e:
        print(f"[!] Failed to fetch liquidity: {e}")
        return 0
