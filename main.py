import asyncio
from sniper_logic import start_sniper
from utils import start_command_bot  # make sure this is defined

async def main():
    await asyncio.gather(
        start_sniper(),
        start_command_bot()
    )

if __name__ == "__main__":
    asyncio.run(main())
