# =========================
# main.py — Final
# =========================

import asyncio
from sniper_logic import start_sniper
from utils import start_command_bot

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(start_sniper())
    loop.run_in_executor(None, start_command_bot)
    loop.run_forever()
