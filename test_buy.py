import asyncio
from dotenv import load_dotenv
from sniper_logic import buy_token

# Load .env secrets
load_dotenv()

async def main():
    # Token to test (PUMP token mint)
    mint_address = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"

    # Amount to buy in SOL (0.06 = ~$9 AUD)
    amount_in_sol = 0.06

    print(f"[TEST] Attempting to buy {amount_in_sol} SOL of token: {mint_address}")
    await buy_token(mint_address, amount_sol=amount_in_sol)

asyncio.run(main())
