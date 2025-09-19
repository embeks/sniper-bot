"""
Phase 1 Configuration - Minimal PumpFun Bonding Curve Sniper
Focus: Catch launches, execute trades, prove profitability
"""

import os
from solders.pubkey import Pubkey
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ============================================
# CORE WALLET CONFIGURATION
# ============================================
PRIVATE_KEY = os.getenv('PRIVATE_KEY')
if not PRIVATE_KEY:
    raise ValueError("PRIVATE_KEY not found in environment variables")

# ============================================
# RPC CONFIGURATION
# ============================================
RPC_ENDPOINT = os.getenv('RPC_ENDPOINT', 'https://mainnet.helius-rpc.com/?api-key=' + os.getenv('HELIUS_API_KEY', ''))
WS_ENDPOINT = RPC_ENDPOINT.replace('https://', 'wss://').replace('http://', 'ws://')

# Backup RPC endpoints
BACKUP_RPC_ENDPOINTS = [
    os.getenv('BACKUP_RPC_1', 'https://api.mainnet-beta.solana.com'),
    os.getenv('BACKUP_RPC_2', 'https://solana-api.projectserum.com')
]

# ============================================
# PHASE 1 TRADING PARAMETERS
# ============================================
# Position sizing
BUY_AMOUNT_SOL = 0.02  # Fixed 0.02 SOL per trade
MAX_POSITIONS = 10  # Maximum concurrent positions
MIN_SOL_BALANCE = 0.5  # Minimum SOL to keep for fees

# Risk management
STOP_LOSS_PERCENTAGE = 50  # Sell at -50% loss
TAKE_PROFIT_PERCENTAGE = 200  # Take profit at 2x (200% gain)
PARTIAL_TAKE_PROFIT = {
    50: 0.25,   # Sell 25% at 50% gain
    100: 0.25,  # Sell 25% at 100% gain
    200: 0.5    # Sell remaining 50% at 200% gain
}

# Timing
SELL_DELAY_SECONDS = 30  # Wait 30 seconds before allowing sells
MAX_POSITION_AGE_SECONDS = 600  # Force sell after 10 minutes

# ============================================
# PUMPFUN SPECIFIC CONFIGURATION
# ============================================
PUMPFUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwkvq")
PUMPFUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

# Bonding curve parameters
MIN_BONDING_CURVE_SOL = 1.5  # Minimum SOL in bonding curve to consider
MAX_BONDING_CURVE_SOL = 85  # Maximum SOL (near migration)
MIGRATION_THRESHOLD_SOL = 85  # When PumpFun migrates to Raydium

# Buy criteria
MIN_VIRTUAL_SOL_RESERVES = 30  # Minimum virtual SOL reserves
MIN_VIRTUAL_TOKEN_RESERVES = 1_000_000_000  # Minimum virtual token reserves
MAX_PRICE_IMPACT_PERCENTAGE = 5  # Maximum acceptable price impact

# ============================================
# DEX CONFIGURATION
# ============================================
# Raydium
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbJEqQ1t8ahN3Hje29jZiuJRm9Kv2b")

# System programs
SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
RENT_PROGRAM_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# ============================================
# MONITORING CONFIGURATION
# ============================================
# WebSocket subscriptions
MONITOR_PROGRAMS = [
    str(PUMPFUN_PROGRAM_ID),  # PumpFun launches
    str(RAYDIUM_PROGRAM_ID)    # Raydium pool creations
]

# Log filters
LOG_CONTAINS_FILTERS = [
    "Program log: Instruction: InitializeBondingCurve",  # PumpFun launch
    "Program log: Instruction: Buy",  # PumpFun buy
    "Program log: Instruction: Sell",  # PumpFun sell
    "initialize2"  # Raydium pool creation
]

# ============================================
# TOKEN FILTERS (PHASE 1 - BASIC)
# ============================================
# Blacklisted tokens (known rugs/scams)
BLACKLISTED_TOKENS = set()  # Add known scam tokens here

# Required token metadata
REQUIRE_METADATA = True
REQUIRE_SOCIAL_LINKS = False  # Not required in Phase 1
MIN_HOLDER_COUNT = 0  # No minimum in Phase 1

# ============================================
# PERFORMANCE TRACKING
# ============================================
TRACK_METRICS = True
METRICS_UPDATE_INTERVAL = 60  # Update metrics every 60 seconds
PROFIT_TARGET_DAILY = 100  # $100-500 daily target (minimum)
PROFIT_TARGET_PHASE1 = 3.5  # Grow from 1.5 SOL to 5+ SOL

# ============================================
# NOTIFICATIONS
# ============================================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ENABLE_TELEGRAM_NOTIFICATIONS = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# Notification triggers
NOTIFY_ON_BUY = True
NOTIFY_ON_SELL = True
NOTIFY_ON_PROFIT = True
NOTIFY_ON_LOSS = True
NOTIFY_PROFIT_THRESHOLD = 50  # Notify on profits > $50

# ============================================
# RETRY CONFIGURATION
# ============================================
MAX_RETRIES = 3
RETRY_DELAY = 1  # Seconds between retries
RPC_TIMEOUT = 30  # RPC call timeout in seconds

# ============================================
# LOGGING
# ============================================
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_FILE = 'v2/sniper_bot.log'

# ============================================
# DEVELOPMENT/TESTING
# ============================================
DRY_RUN = os.getenv('DRY_RUN', 'false').lower() == 'true'
DEBUG_MODE = os.getenv('DEBUG', 'false').lower() == 'true'

if DRY_RUN:
    print("⚠️ DRY RUN MODE - No real transactions will be executed")
if DEBUG_MODE:
    print("🔍 DEBUG MODE - Verbose logging enabled")
