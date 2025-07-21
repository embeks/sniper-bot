import asyncio
from utils import snipe_token

async def manual_test():
    await snipe_token("7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")

if __name__ == "__main__":
    asyncio.run(manual_test())
