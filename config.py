"""
config
"""

import os
from solders.pubkey import Pubkey
from dotenv import load_dotenv

load_dotenv()

# ============================================
# CORE WALLET CONFIGURATION
# ============================================
PRIVATE_KEY = os.getenv('PRIVATE_KEY') or os.getenv('SOLANA_PRIVATE_KEY')
if not PRIVATE_KEY:
    raise ValueError("PRIVATE_KEY not found in environment variables")

# ============================================
# RPC CONFIGURATION
# ============================================
HELIUS_API_KEY = os.getenv('HELIUS_API') or os.getenv('HELIUS_API_KEY', '')
RPC_ENDPOINT = os.getenv('RPC_URL') or os.getenv('RPC_ENDPOINT') or f'https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}'
WS_ENDPOINT = RPC_ENDPOINT.replace('https://', 'wss://').replace('http://', 'ws://')

BACKUP_RPC_ENDPOINTS = [
    os.getenv('BACKUP_RPC_1', 'https://api.mainnet-beta.solana.com'),
    os.getenv('BACKUP_RPC_2', 'https://solana-api.projectserum.com')
]

# ============================================
# TRADING PARAMETERS
# ============================================
APPROX_SOL_PRICE_USD = 235.0

BUY_AMOUNT_SOL = float(os.getenv('BUY_AMOUNT_SOL', '0.05'))
PUMPFUN_EARLY_AMOUNT = float(os.getenv('PUMPFUN_EARLY_AMOUNT', BUY_AMOUNT_SOL))
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '2'))
MIN_SOL_BALANCE = float(os.getenv('MIN_SOL_BALANCE', '0.05'))

STOP_LOSS_PERCENTAGE = float(os.getenv('STOP_LOSS_PERCENT', '10'))
TAKE_PROFIT_PERCENTAGE = float(os.getenv('TAKE_PROFIT_1', '200')) / 100 * 100

# Tiered take-profit (whale strategy - let winners run)
TIER_1_PROFIT_PERCENT = float(os.getenv('TIER_1_PROFIT', '30.0'))
TIER_1_SELL_PERCENT = float(os.getenv('TIER_1_SELL', '40.0'))

TIER_2_PROFIT_PERCENT = float(os.getenv('TIER_2_PROFIT', '60.0'))
TIER_2_SELL_PERCENT = float(os.getenv('TIER_2_SELL', '40.0'))

TIER_3_PROFIT_PERCENT = float(os.getenv('TIER_3_PROFIT', '100.0'))
TIER_3_SELL_PERCENT = float(os.getenv('TIER_3_SELL', '20.0'))  # Final 20%

# ============================================
# VELOCITY GATE SETTINGS
# ============================================
VELOCITY_MIN_SOL_PER_SECOND = float(os.getenv('VELOCITY_MIN_SOL_PER_SECOND', '2.0'))  # Lowered from 2.5
VELOCITY_MIN_BUYERS = int(os.getenv('VELOCITY_MIN_BUYERS', '5'))
VELOCITY_MAX_TOKEN_AGE = float(os.getenv('VELOCITY_MAX_TOKEN_AGE', '16.0'))           # Raised from 15.0
VELOCITY_MIN_RECENT_1S_SOL = float(os.getenv('VELOCITY_MIN_RECENT_1S_SOL', '2.0'))    # Lowered from 2.5
VELOCITY_MIN_RECENT_3S_SOL = float(os.getenv('VELOCITY_MIN_RECENT_3S_SOL', '4.0'))    # Lowered from 5.0
VELOCITY_MAX_DROP_PERCENT = float(os.getenv('VELOCITY_MAX_DROP_PERCENT', '25.0'))
VELOCITY_MIN_SNAPSHOTS = int(os.getenv('VELOCITY_MIN_SNAPSHOTS', '1'))                # ✅ CRITICAL: 1 for instant entry

# ============================================
# VELOCITY CEILING (BOT PUMP PROTECTION)
# ============================================
VELOCITY_MAX_SOL_PER_SECOND = float(os.getenv('VELOCITY_MAX_SOL_PER_SECOND', '15.0'))  # Raised from 8.0
VELOCITY_MAX_RECENT_1S_SOL = float(os.getenv('VELOCITY_MAX_RECENT_1S_SOL', '20.0'))    # Raised from 8.0
VELOCITY_MAX_RECENT_3S_SOL = float(os.getenv('VELOCITY_MAX_RECENT_3S_SOL', '35.0'))    # Raised from 12.0

# ============================================
# TIMER-BASED EXIT SETTINGS (DISABLED - using whale tiered exits)
# ============================================
# Timer settings (disabled but keep variables for compatibility)
TIMER_EXIT_BASE_SECONDS = int(os.getenv('TIMER_EXIT_BASE_SECONDS', '999'))  # Disabled (set to 999)
TIMER_EXIT_VARIANCE_SECONDS = int(os.getenv('TIMER_EXIT_VARIANCE_SECONDS', '0'))
TIMER_EXTENSION_SECONDS = int(os.getenv('TIMER_EXTENSION_SECONDS', '0'))
TIMER_EXTENSION_PNL_THRESHOLD = float(os.getenv('TIMER_EXTENSION_PNL_THRESHOLD', '999'))
TIMER_MAX_EXTENSIONS = int(os.getenv('TIMER_MAX_EXTENSIONS', '0'))

# ============================================
# MOMENTUM EXIT SETTINGS
# ============================================
MOMENTUM_MAX_DRAWDOWN_PP = float(os.getenv('MOMENTUM_MAX_DRAWDOWN_PP', '30.0'))     # Raised from 25.0
MOMENTUM_MIN_PEAK_PERCENT = float(os.getenv('MOMENTUM_MIN_PEAK_PERCENT', '20.0'))   # Raised from 15.0
MOMENTUM_DRAWDOWN_MIN_AGE = float(os.getenv('MOMENTUM_DRAWDOWN_MIN_AGE', '20.0'))   # Raised from 15.0
MOMENTUM_VELOCITY_DEATH_PERCENT = float(os.getenv('MOMENTUM_VELOCITY_DEATH_PERCENT', '40.0'))  # Lowered from 50.0
MOMENTUM_BIG_WIN_PERCENT = float(os.getenv('MOMENTUM_BIG_WIN_PERCENT', '80.0'))     # Raised from 50.0
MOMENTUM_MAX_HOLD_SECONDS = float(os.getenv('MOMENTUM_MAX_HOLD_SECONDS', '45.0'))   # Raised from 15.0

# ============================================
# PROFIT PROTECTION SETTINGS
# ============================================
EXTREME_TP_PERCENT = float(os.getenv('EXTREME_TP_PERCENT', '100.0'))     # Raised from 30.0
TRAIL_START_PERCENT = float(os.getenv('TRAIL_START_PERCENT', '50.0'))    # Lowered from 70.0
TRAIL_GIVEBACK_PERCENT = float(os.getenv('TRAIL_GIVEBACK_PERCENT', '25.0'))  # Lowered from 35.0

# ============================================
# FAIL-FAST SETTINGS
# ============================================
FAIL_FAST_CHECK_TIME = float(os.getenv('FAIL_FAST_CHECK_TIME', '3.0'))
FAIL_FAST_PNL_THRESHOLD = float(os.getenv('FAIL_FAST_PNL_THRESHOLD', '-5.0'))
FAIL_FAST_VELOCITY_THRESHOLD = float(os.getenv('FAIL_FAST_VELOCITY_THRESHOLD', '40.0'))

# ============================================
# PARTIAL TAKE PROFIT (LEGACY)
# ============================================
PARTIAL_TAKE_PROFIT = {}
tp1, sp1 = os.getenv('TAKE_PROFIT_1'), os.getenv('SELL_PERCENT_1')
tp2, sp2 = os.getenv('TAKE_PROFIT_2'), os.getenv('SELL_PERCENT_2')
tp3, sp3 = os.getenv('TAKE_PROFIT_3'), os.getenv('SELL_PERCENT_3')

if tp1 and sp1:
    PARTIAL_TAKE_PROFIT[float(tp1)] = float(sp1) / 100.0
if tp2 and sp2:
    PARTIAL_TAKE_PROFIT[float(tp2)] = float(sp2) / 100.0
if tp3 and sp3:
    PARTIAL_TAKE_PROFIT[float(tp3)] = float(sp3) / 100.0

SELL_DELAY_SECONDS = int(os.getenv('SELL_DELAY_SECONDS', '0'))
MAX_POSITION_AGE_SECONDS = int(os.getenv('MAX_HOLD_TIME_SEC', '120'))  # Let winners run to 2 min
MONITOR_CHECK_INTERVAL = float(os.getenv('MONITOR_CHECK_INTERVAL', '0.5'))
DATA_FAILURE_TOLERANCE = int(os.getenv('DATA_FAILURE_TOLERANCE', '10'))

# ============================================
# LIQUIDITY VALIDATION
# ============================================
LIQUIDITY_MULTIPLIER = float(os.getenv('LIQUIDITY_MULTIPLIER', '5.0'))
MIN_LIQUIDITY_SOL = float(os.getenv('MIN_LIQUIDITY_SOL', '0.5'))  # Lowered from 0.6
MAX_SLIPPAGE_PERCENT = float(os.getenv('MAX_SLIPPAGE_PERCENT', '2.5'))

# ============================================
# SLIPPAGE PROTECTION
# ============================================
MAX_ENTRY_SLIPPAGE_PERCENT = float(os.getenv('MAX_ENTRY_SLIPPAGE_PERCENT', '40.0'))

# ============================================
# PUMPFUN CONFIG
# ============================================
PUMPFUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMPFUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

MIN_BONDING_CURVE_SOL = 10.0  # Lowered from 15.0 - whale zone
MAX_BONDING_CURVE_SOL = 18.0  # Lowered from 45.0 - whale zone
MIGRATION_THRESHOLD_SOL = 85

MIN_VIRTUAL_SOL_RESERVES = 30
MIN_VIRTUAL_TOKEN_RESERVES = 1_000_000_000
MAX_PRICE_IMPACT_PERCENTAGE = 5

AUTO_BUY = os.getenv('AUTO_BUY', 'true').lower() == 'true'
PUMPFUN_EARLY_BUY = os.getenv('PUMPFUN_EARLY_BUY', 'true').lower() == 'true'

# ============================================
# DEX CONFIGURATION
# ============================================
RAYDIUM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbJEqQ1t8ahN3Hje29jZiuJRm9Kv2b")

SYSTEM_PROGRAM_ID = Pubkey.from_string("11111111111111111111111111111111")

# ============================================
# TOKEN PROGRAM IDs - UPDATED FOR TOKEN-2022
# ============================================
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

RENT_PROGRAM_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# ============================================
# MONITORING
# ============================================
MONITOR_PROGRAMS = [
    str(PUMPFUN_PROGRAM_ID),
    str(RAYDIUM_PROGRAM_ID)
]

LOG_CONTAINS_FILTERS = [
    "Program log: Instruction: InitializeBondingCurve",
    "Program log: Instruction: Buy",
    "Program log: Instruction: Sell",
    "initialize2"
]

# ============================================
# TOKEN FILTERS
# ============================================
BLACKLISTED_TOKENS = set()
REQUIRE_METADATA = True
REQUIRE_SOCIAL_LINKS = False
MIN_HOLDER_COUNT = 60

# ============================================
# METRICS
# ============================================
TRACK_METRICS = True
METRICS_UPDATE_INTERVAL = 60
PROFIT_TARGET_DAILY = 100
PROFIT_TARGET_PHASE1 = 3.5

# ============================================
# NOTIFICATIONS
# ============================================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
ENABLE_TELEGRAM_NOTIFICATIONS = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

NOTIFY_ON_BUY = True
NOTIFY_ON_SELL = True
NOTIFY_ON_PROFIT = True
NOTIFY_ON_LOSS = True
NOTIFY_PROFIT_THRESHOLD = 50

# ============================================
# RETRY CONFIG
# ============================================
MAX_RETRIES = 3
RETRY_DELAY = 1
RPC_TIMEOUT = 30

# ============================================
# LOGGING
# ============================================
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_FILE = None

# ============================================
# DEVELOPMENT
# ============================================
DRY_RUN = os.getenv('DRY_RUN', 'false').lower() == 'true'
DEBUG_MODE = os.getenv('DEBUG', 'false').lower() == 'true'

if DRY_RUN:
    print("⚠️ DRY RUN MODE - No real transactions will be executed")
if DEBUG_MODE:
    print("🔍 DEBUG MODE - Verbose logging enabled")
