import asyncio
from mempool_listener import manual_trigger

if __name__ == "__main__":
    token_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # Replace with actual token address
    asyncio.run(manual_trigger(token_mint))
