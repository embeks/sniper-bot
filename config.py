
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
# JITO BUNDLE CONFIGURATION
# ============================================
JITO_ENABLED = os.getenv('JITO_ENABLED', 'true').lower() == 'true'
JITO_TIP_AMOUNT_SOL = float(os.getenv('JITO_TIP_SOL', '0.003'))  # Default 0.005 SOL - increased for better tx landing
JITO_TIP_AGGRESSIVE_SOL = float(os.getenv('JITO_TIP_AGGRESSIVE_SOL', '0.003'))  # For 0-sell tokens

# Jito Block Engine endpoints (rotate randomly to reduce contention)
JITO_ENDPOINTS = [
    "https://mainnet.block-engine.jito.wtf/api/v1/transactions",
    "https://amsterdam.mainnet.block-engine.jito.wtf/api/v1/transactions",
    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/transactions",
    "https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions",
    "https://tokyo.mainnet.block-engine.jito.wtf/api/v1/transactions",
]

# Jito tip accounts (pick one randomly per TX to reduce contention)
JITO_TIP_ACCOUNTS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
    "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
    "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
    "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
    "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

# ============================================
# TRADING PARAMETERS
# ============================================
APPROX_SOL_PRICE_USD = 235.0

BUY_AMOUNT_SOL = float(os.getenv('BUY_AMOUNT_SOL', '0.05'))
PUMPFUN_EARLY_AMOUNT = float(os.getenv('PUMPFUN_EARLY_AMOUNT', BUY_AMOUNT_SOL))

# FIXED POSITION SIZING - no confidence scaling
POSITION_SIZE_DEFAULT = float(os.getenv('BUY_AMOUNT_SOL', '0.05'))
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '3'))
MIN_SOL_BALANCE = float(os.getenv('MIN_SOL_BALANCE', '0.05'))

# STOP_LOSS_PERCENTAGE - Now handled by MOMENTUM_DEATH_SOL in curve-based exits
# Kept for backward compatibility but not used for exit decisions
STOP_LOSS_PERCENTAGE = float(os.getenv('STOP_LOSS_PERCENT', '35'))
TAKE_PROFIT_PERCENTAGE = float(os.getenv('TAKE_PROFIT_1', '200')) / 100 * 100

# Tiered take-profit (whale strategy - let winners run)
# 2-tier system based on 11-trade analysis: reduces fees, lets winners run
# TIER_1 DISABLED - exits now handled by curve-based system
# TIER_1_PROFIT_PERCENT = 9999.0  # REMOVED - dead code path
TIER_1_SELL_PERCENT = float(os.getenv('TIER_1_SELL', '75.0'))      # Was 40%

TIER_2_PROFIT_PERCENT = float(os.getenv('TIER_2_PROFIT', '30.0'))  # Was 40%, lowered to capture +25-35% peaks
TIER_2_SELL_PERCENT = float(os.getenv('TIER_2_SELL', '100.0'))      # Was 40% - sells remainder

# TIER 3 DISABLED - 2-tier system reduces fees
# Tier2 now sells remaining 50% at +60%
# TIER_3_PROFIT_PERCENT = float(os.getenv('TIER_3_PROFIT', '60.0'))
# TIER_3_SELL_PERCENT = float(os.getenv('TIER_3_SELL', '20.0'))  # Final 20%

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
# RUG DETECTION - CURVE-BASED (Absolute SOL thresholds)
# ============================================
# OLD percentage-based (caused false exits like EsFdhfGd):
# RUG_CURVE_DROP_PERCENT = 55  # REMOVED - percentages lie during volatility

# NEW absolute thresholds (never false trigger):
RUG_FLOOR_SOL = 1.5           # Below this = liquidity gone = definite rug
MOMENTUM_DEATH_SOL = 1.5      # Lost this much from entry = sellers winning
PROFIT_PEAK_THRESHOLD_SOL = 3.0   # Must hit this profit before decay triggers
PROFIT_DECAY_PERCENT = 0.30  # 30% drop from peak triggers exit
WHALE_SELL_PERCENT = 15.0     # Single sell this % of curve = smart money exit
BUY_DROUGHT_SECONDS = 8.0     # No buys for this long + declining = dead
MIN_EXIT_AGE_SECONDS = 8.0    # Min age before non-emergency exits

# ============================================
# TWO-TIER EXIT SYSTEM
# ============================================
# Tier 1: Under 25 SOL - exit on momentum death (inflow dies)
TIER1_MAX_CURVE_SOL = 25.0
TIER1_MIN_PROFIT_PCT = 20.0           # Must be up 20%+ before momentum exit activates
TIER1_INFLOW_WINDOW_SEC = 2.0         # Check inflow over 2 seconds
TIER1_INFLOW_DEATH_THRESHOLD = 0.3    # If recent inflow < 30% of previous window, momentum dead

# Tier 2: 25+ SOL - exit on 30% drop from peak
TIER2_DROP_FROM_PEAK_PCT = 0.30       # 30% drop from peak

# ============================================
# MOMENTUM EXIT SETTINGS
# ============================================
MOMENTUM_MAX_DRAWDOWN_PP = float(os.getenv('MOMENTUM_MAX_DRAWDOWN_PP', '15.0'))     # Raised from 25.0
MOMENTUM_MIN_PEAK_PERCENT = float(os.getenv('MOMENTUM_MIN_PEAK_PERCENT', '20.0'))   # Raised from 15.0
MOMENTUM_DRAWDOWN_MIN_AGE = float(os.getenv('MOMENTUM_DRAWDOWN_MIN_AGE', '20.0'))   # Raised from 15.0
MOMENTUM_VELOCITY_DEATH_PERCENT = float(os.getenv('MOMENTUM_VELOCITY_DEATH_PERCENT', '40.0'))  # Lowered from 50.0
MOMENTUM_BIG_WIN_PERCENT = float(os.getenv('MOMENTUM_BIG_WIN_PERCENT', '80.0'))     # Raised from 50.0
MOMENTUM_MAX_HOLD_SECONDS = float(os.getenv('MOMENTUM_MAX_HOLD_SECONDS', '45.0'))   # Raised from 15.0

# ============================================
# DYNAMIC CRASH THRESHOLDS (based on 11-trade analysis)
# ============================================
CRASH_THRESHOLD_DEFAULT = 25.0      # Drop from peak to trigger exit
CRASH_THRESHOLD_RELAXED = 35.0      # When peak >= 30%, allow more room
CRASH_RELAXED_PEAK_THRESHOLD = 30.0 # Peak P&L needed to use relaxed threshold

# ============================================
# RUNNER EXTENSION (based on 11-trade analysis)
# ============================================
RUNNER_EXTENDED_MAX_AGE = 180       # Extended hold for confirmed runners (was 120)
RUNNER_EXTEND_BONDING_THRESHOLD = 12.0  # Extend if bonding curve > this %

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

# Early entry slippage (competing with other bots on fast tokens)
EARLY_ENTRY_SLIPPAGE_BPS = int(os.getenv('EARLY_ENTRY_SLIPPAGE_BPS', '10000'))  # 100% base

# ============================================
# PUMPFUN CONFIG
# ============================================
PUMPFUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMPFUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

# Entry range - EARLIER to avoid slippage war
MIN_BONDING_CURVE_SOL = 4.0
MAX_BONDING_CURVE_SOL = 12.0
MIGRATION_THRESHOLD_SOL = 85

MIN_VIRTUAL_SOL_RESERVES = 30
MIN_VIRTUAL_TOKEN_RESERVES = 1_000_000_000
MAX_PRICE_IMPACT_PERCENTAGE = 5

AUTO_BUY = os.getenv('AUTO_BUY', 'true').lower() == 'true'
PUMPFUN_EARLY_BUY = os.getenv('PUMPFUN_EARLY_BUY', 'true').lower() == 'true'

# ============================================
# EARLY ENTRY QUALITY GATES
# ============================================
MIN_UNIQUE_BUYERS = int(os.getenv('MIN_UNIQUE_BUYERS', '3'))   # 3 unique buyers minimum
MAX_SELLS_BEFORE_ENTRY = int(os.getenv('MAX_SELLS_BEFORE_ENTRY', '3'))   # Allow some sells, rely on sell burst detection
MAX_SINGLE_BUY_PERCENT = float(os.getenv('MAX_SINGLE_BUY_PERCENT', '50.0'))  # RE-ENABLED: 6CdZ47UW had 53% single buy = rug
MIN_VELOCITY = float(os.getenv('MIN_VELOCITY', '1.5'))  # Minimum SOL velocity to filter weak entries
MIN_BUYERS_PER_SECOND = float(os.getenv('MIN_BUYERS_PER_SECOND', '1.0'))  # Minimum buyer velocity for organic traction
MAX_TOKEN_AGE_SECONDS = float(os.getenv('MAX_TOKEN_AGE_SECONDS', '15.0'))
MIN_TOKEN_AGE_SECONDS = float(os.getenv('MIN_TOKEN_AGE_SECONDS', '0.2'))   # Enter faster

# NEW FILTERS (21-trade baseline learnings)
# MAX_VELOCITY = float(os.getenv('MAX_VELOCITY', '15.0'))  # Redundant - using buyer velocity instead
MAX_BUYERS_PER_SECOND = float(os.getenv('MAX_BUYERS_PER_SECOND', '10.0'))  # Re-enabled: 16.8/s = coordinated bots (4oZTd3yQ), 4.7/s = organic (YJ8PUzVJ)
MAX_SELLS_AT_ENTRY = int(os.getenv('MAX_SELLS_AT_ENTRY', '5'))
MIN_BUY_SELL_RATIO = float(os.getenv('MIN_BUY_SELL_RATIO', '1.5'))
MAX_TOP2_BUY_PERCENT = float(os.getenv('MAX_TOP2_BUY_PERCENT', '70.0'))  # Blocks coordinated pump-and-dumps (78-95% top-2 = rug, <25% = organic)

# SELL BURST DETECTION (timing-based, not count-based)
SELL_BURST_COUNT = int(os.getenv('SELL_BURST_COUNT', '4'))        # Number of sells that indicates dump
SELL_BURST_WINDOW = float(os.getenv('SELL_BURST_WINDOW', '3.0'))  # Time window in seconds

# Dev token filter toggle
ENABLE_DEV_TOKEN_FILTER = False  # Set True to re-enable

# CURVE MOMENTUM GATE (ensure pump is still active)
CURVE_MOMENTUM_WINDOW_RECENT = float(os.getenv('CURVE_MOMENTUM_WINDOW_RECENT', '2.0'))  # Recent window (seconds)
CURVE_MOMENTUM_WINDOW_OLDER = float(os.getenv('CURVE_MOMENTUM_WINDOW_OLDER', '5.0'))    # Older window (seconds)
CURVE_MOMENTUM_MIN_GROWTH = float(os.getenv('CURVE_MOMENTUM_MIN_GROWTH', '1.02'))       # Min 2% growth required

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
# ORDER FLOW EXIT SETTINGS (FLOW-BASED v2)
# ============================================
# Legacy toggle - set False to use new flow-based exits
USE_LEGACY_EXITS = False

# Minimum conditions before ANY exit signals activate
EXIT_MIN_AGE_SECONDS = float(os.getenv('EXIT_MIN_AGE_SECONDS', '5.0'))
EXIT_MIN_TRANSACTIONS = int(os.getenv('EXIT_MIN_TRANSACTIONS', '8'))

# Time windows for flow analysis
FLOW_WINDOW_SHORT = float(os.getenv('FLOW_WINDOW_SHORT', '5.0'))   # 5 second window
FLOW_WINDOW_MEDIUM = float(os.getenv('FLOW_WINDOW_MEDIUM', '10.0'))  # 10 second window

# === EMERGENCY EXITS (highest priority) ===
# Whale exit: single large sell relative to curve size
RUG_SINGLE_SELL_PERCENT = int(os.getenv('RUG_SINGLE_SELL_PERCENT', '40'))  # Whale exit if single sell is 40%+ of curve

# Curve drain: Now handled by RUG_FLOOR_SOL in curve-based exits

# === HIGH PRIORITY EXITS ===
# Sell burst: coordinated dump starting
DUMP_SELL_COUNT = int(os.getenv('DUMP_SELL_COUNT', '4'))
DUMP_SELL_WINDOW = float(os.getenv('DUMP_SELL_WINDOW', '5.0'))

# Heavy sell volume in SOL
DUMP_SELL_SOL_TOTAL = float(os.getenv('DUMP_SELL_SOL_TOTAL', '2.0'))

# === MEDIUM PRIORITY EXITS ===
# Sell ratio: tide turning (sells dominating buys)
PRESSURE_SELL_RATIO = float(os.getenv('PRESSURE_SELL_RATIO', '0.6'))  # 60%+ sells
PRESSURE_MIN_SELLS = int(os.getenv('PRESSURE_MIN_SELLS', '5'))  # Min sells to calculate ratio

# Net flow: more leaving than entering
FLOW_NET_NEGATIVE_SOL = float(os.getenv('FLOW_NET_NEGATIVE_SOL', '-1.5'))

# === LOW PRIORITY EXITS ===
# Buyer death: momentum completely dead
DEATH_NO_BUY_SECONDS = float(os.getenv('DEATH_NO_BUY_SECONDS', '12.0'))

# === HOLD CONDITIONS (override exit signals) ===
HOLD_MIN_BUYS_SHORT = int(os.getenv('HOLD_MIN_BUYS_SHORT', '2'))  # 2+ buys in 5s = still pumping
HOLD_MAX_TIME_SINCE_BUY = float(os.getenv('HOLD_MAX_TIME_SINCE_BUY', '8.0'))  # Last buy < 8s ago

# === SAFETY BACKSTOPS (unchanged) ===
# Stop loss at -35% (already exists as STOP_LOSS_PERCENTAGE)
# Max hold at 120s (already exists as MAX_POSITION_AGE_SECONDS)

# ============================================
# DEVELOPMENT
# ============================================
DRY_RUN = os.getenv('DRY_RUN', 'false').lower() == 'true'
DEBUG_MODE = os.getenv('DEBUG', 'false').lower() == 'true'

if DRY_RUN:
    print("⚠️ DRY RUN MODE - No real transactions will be executed")
if DEBUG_MODE:
    print("🔍 DEBUG MODE - Verbose logging enabled")
