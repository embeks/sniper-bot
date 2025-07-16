import asyncio
from dotenv import load_dotenv
from sniper_logic import buy_token

# Load .env secrets
load_dotenv()

async def main():
    # Token to test (example token mint)
    mint_address = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"

    # Updated test amount
    amount_in_sol = 0.03

    print(f"[TEST] Attempting to buy {amount_in_sol} SOL of token: {mint_address}")
    await buy_token(mint_address, amount_sol=amount_in_sol)

if __name__ == "__main__":
    asyncio.run(main())
