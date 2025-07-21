import asyncio
from utils import snipe_token

# Direct call bypassing env logic
async def manual_test():
    await snipe_token("7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr")

asyncio.run(manual_test())
