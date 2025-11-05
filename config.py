"""
config - PROBE/CONFIRM + DYNAMIC TPS + HEALTH CHECK + TRAILING STOP
- Split entry: 0.03 SOL probe + 0.05 SOL confirm (total 0.08 SOL)
- Health check moved to 8-12s (from 10-15s)
- Dynamic TP levels based on entry MC
- Trailing stop after first TP
- Timer extended to 25-33s
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
# NEW: Increased to 0.08 SOL (split as 0.03 probe + 0.05 confirm)
BUY_AMOUNT_SOL = float(os.getenv('BUY_AMOUNT_SOL', '0.08'))
PUMPFUN_EARLY_AMOUNT = float(os.getenv('PUMPFUN_EARLY_AMOUNT', BUY_AMOUNT_SOL))
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '2'))
MIN_SOL_BALANCE = float(os.getenv('MIN_SOL_BALANCE', '0.05'))

# Risk management
STOP_LOSS_PERCENTAGE = float(os.getenv('STOP_LOSS_PERCENT', '25'))
TAKE_PROFIT_PERCENTAGE = float(os.getenv('TAKE_PROFIT_1', '200')) / 100 * 100

# ============================================
# PROBE/CONFIRM ENTRY SETTINGS (NEW!)
# ============================================
# Enable split entry strategy
ENABLE_PROBE_ENTRY = True

# Probe: Fast entry without Helius check
PROBE_AMOUNT_SOL = 0.03  # 37.5% of total position

# Confirm: Add remaining after validation
CONFIRM_AMOUNT_SOL = 0.05  # 62.5% of total position

# Skip Helius holder check for probe (speed over safety)
PROBE_SKIP_HELIUS = True

# Wait time before momentum check (seconds)
CONFIRM_DELAY_SECONDS = 1.2

# Momentum requirements to confirm
CONFIRM_MIN_VELOCITY_RATIO = 1.15  # Need 15% velocity increase
CONFIRM_MIN_BUYER_DELTA = 6  # Need +6 unique buyers (estimated)

# ============================================
# VELOCITY GATE SETTINGS
# ============================================
VELOCITY_MIN_SOL_PER_SECOND = float(os.getenv('VELOCITY_MIN_SOL_PER_SECOND', '2.0'))
VELOCITY_MIN_BUYERS = int(os.getenv('VELOCITY_MIN_BUYERS', '5'))
VELOCITY_MAX_TOKEN_AGE = float(os.getenv('VELOCITY_MAX_TOKEN_AGE', '25.0'))
VELOCITY_MIN_RECENT_1S_SOL = float(os.getenv('VELOCITY_MIN_RECENT_1S_SOL', '2.0'))
VELOCITY_MIN_RECENT_3S_SOL = float(os.getenv('VELOCITY_MIN_RECENT_3S_SOL', '4.0'))
VELOCITY_MAX_DROP_PERCENT = float(os.getenv('VELOCITY_MAX_DROP_PERCENT', '25.0'))
VELOCITY_MIN_SNAPSHOTS = int(os.getenv('VELOCITY_MIN_SNAPSHOTS', '1'))

# ============================================
# HEALTH CHECK SETTINGS (UPDATED!)
# ============================================
# NEW: Moved from 10-15s to 8-12s (faster detection of dying tokens)
HEALTH_CHECK_START = 8.0
HEALTH_CHECK_END = 12.0

# Exit if velocity drops below this % of pre-buy
HEALTH_CHECK_VELOCITY_THRESHOLD = 0.35  # 35%

# Exit if MC drops below this % of entry
HEALTH_CHECK_MC_THRESHOLD = 0.80  # 80%

# ============================================
# FAIL-FAST EXIT SETTINGS (UPDATED!)
# ============================================
FAIL_FAST_CHECK_TIME = float(os.getenv('FAIL_FAST_CHECK_TIME', '5.0'))

# NEW: Adjusted from -10% to -15% (less aggressive)
FAIL_FAST_PNL_THRESHOLD = float(os.getenv('FAIL_FAST_PNL_THRESHOLD', '-15.0'))

# NEW: Aligned with health check (35% of pre-buy velocity)
FAIL_FAST_VELOCITY_THRESHOLD = float(os.getenv('FAIL_FAST_VELOCITY_THRESHOLD', '35.0'))

# ============================================
# DYNAMIC TAKE-PROFIT SETTINGS (NEW!)
# ============================================
# Enable MC-based scaled exits
ENABLE_DYNAMIC_TPS = True

# TP multipliers based on entry MC
# Early entries (≤12K MC): More room to run
TP_LEVELS_EARLY = [2.6, 3.2, 3.9, 4.8]

# Mid entries (12-22K MC): Moderate targets
TP_LEVELS_MID = [2.4, 3.0, 3.6, 4.5]

# Late entries (>22K MC): Tighter targets (less headroom)
TP_LEVELS_LATE = [2.2, 2.8, 3.4, 4.2]

# Percentage to sell at each TP level
TP_SELL_PERCENTS = [25, 25, 30, 20]  # 25%, 25%, 30%, 20%

# Wait after buy before allowing TPs (avoid post-buy spikes)
TP_COOLDOWN_SECONDS = 2.0

# ============================================
# TRAILING STOP SETTINGS (NEW!)
# ============================================
# Enable trailing stop after first TP
ENABLE_TRAILING_STOP = True

# Exit if price drops this % from peak MC (after first TP taken)
TRAILING_STOP_DRAWDOWN = 18.0  # -18% from peak

# ============================================
# TIMER-BASED EXIT SETTINGS (UPDATED!)
# ============================================
# NEW: Extended from 20s to 25s base
TIMER_EXIT_BASE_SECONDS = int(os.getenv('TIMER_EXIT_BASE_SECONDS', '25'))

# Random variance to add (+/- seconds)
TIMER_EXIT_VARIANCE_SECONDS = int(os.getenv('TIMER_EXIT_VARIANCE_SECONDS', '5'))

# Extension for mega-pumps
TIMER_EXTENSION_SECONDS = int(os.getenv('TIMER_EXTENSION_SECONDS', '10'))

# P&L threshold to consider extension (%)
TIMER_EXTENSION_PNL_THRESHOLD = float(os.getenv('TIMER_EXTENSION_PNL_THRESHOLD', '80'))

# Maximum total extensions allowed
TIMER_MAX_EXTENSIONS = int(os.getenv('TIMER_MAX_EXTENSIONS', '2'))

# NEW: Auto-extend to this duration if momentum strong
TIMER_AUTO_EXTEND_TO = 33  # seconds

# ============================================
# PROFIT PROTECTION SETTINGS (EXISTING)
# ============================================
# Extreme Take-Profit: Exit immediately if profit goes parabolic
EXTREME_TP_PERCENT = float(os.getenv('EXTREME_TP_PERCENT', '150.0'))

# Trailing Stop: Protect profits after hitting a certain level
TRAIL_START_PERCENT = float(os.getenv('TRAIL_START_PERCENT', '100.0'))
TRAIL_GIVEBACK_PERCENT = float(os.getenv('TRAIL_GIVEBACK_PERCENT', '50.0'))

# ============================================
# LIQUIDITY VALIDATION
# ============================================
LIQUIDITY_MULTIPLIER = float(os.getenv('LIQUIDITY_MULTIPLIER', '5.0'))
MIN_LIQUIDITY_SOL = float(os.getenv('MIN_LIQUIDITY_SOL', '0.6'))
MAX_SLIPPAGE_PERCENT = float(os.getenv('MAX_SLIPPAGE_PERCENT', '2.5'))

# ============================================
# LEGACY PARTIAL PROFIT SETTINGS (DEPRECATED)
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

# Timing
SELL_DELAY_SECONDS = int(os.getenv('SELL_DELAY_SECONDS', '0'))
MAX_POSITION_AGE_SECONDS = int(os.getenv('MAX_HOLD_TIME_SEC', '120'))
MONITOR_CHECK_INTERVAL = float(os.getenv('MONITOR_CHECK_INTERVAL', '0.5'))
DATA_FAILURE_TOLERANCE = int(os.getenv('DATA_FAILURE_TOLERANCE', '10'))

# ============================================
# PUMPFUN SPECIFIC CONFIGURATION
# ============================================
PUMPFUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMPFUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

MIN_BONDING_CURVE_SOL = 15.0
MAX_BONDING_CURVE_SOL = 45.0
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
TOKEN_PROGRAM_ID = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM_ID = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
RENT_PROGRAM_ID = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# ============================================
# MONITORING CONFIGURATION
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
# PERFORMANCE TRACKING
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
# RETRY CONFIGURATION
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
# DEVELOPMENT/TESTING
# ============================================
DRY_RUN = os.getenv('DRY_RUN', 'false').lower() == 'true'
DEBUG_MODE = os.getenv('DEBUG', 'false').lower() == 'true'

if DRY_RUN:
    print("⚠️ DRY RUN MODE - No real transactions will be executed")
if DEBUG_MODE:
    print("🔍 DEBUG MODE - Verbose logging enabled")
