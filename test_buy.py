import asyncio
from dotenv import load_dotenv
from sniper_logic import buy_token

# Load your .env file with private key
load_dotenv()

async def main():
    # Replace this with the mint address of a real token listed on Jupiter
    mint_address = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
    
    # Set a safe test amount (0.02 SOL is ~8-9 AUD)
    await buy_token(mint_address, amount_sol=0.02)

asyncio.run(main())
