import asyncio
from dotenv import load_dotenv
from sniper_logic import buy_token

# Load .env with private key
load_dotenv()

async def main():
    # Replace with any tradable token mint address (example is PUMP)
    mint_address = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"

    # Updated test amount: ~0.06 SOL (~15 AUD)
    await buy_token(mint_address, amount_sol=0.06)

asyncio.run(main())
