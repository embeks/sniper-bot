"""
config - UPDATED: Added velocity gate and timer-based exit settings
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
BUY_AMOUNT_SOL = float(os.getenv('BUY_AMOUNT_SOL', '0.03'))
PUMPFUN_EARLY_AMOUNT = float(os.getenv('PUMPFUN_EARLY_AMOUNT', BUY_AMOUNT_SOL))
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '2'))
MIN_SOL_BALANCE = float(os.getenv('MIN_SOL_BALANCE', '0.05'))

# Risk management
STOP_LOSS_PERCENTAGE = float(os.getenv('STOP_LOSS_PERCENT', '25'))
TAKE_PROFIT_PERCENTAGE = float(os.getenv('TAKE_PROFIT_1', '200')) / 100 * 100

# ============================================
# VELOCITY GATE SETTINGS (NEW)
# ============================================
# Minimum SOL/second inflow rate to enter
VELOCITY_MIN_SOL_PER_SECOND = float(os.getenv('VELOCITY_MIN_SOL_PER_SECOND', '2.0'))

# Minimum unique buyers required (estimated)
VELOCITY_MIN_BUYERS = int(os.getenv('VELOCITY_MIN_BUYERS', '5'))

# Maximum token age to even check velocity (skip older tokens)
VELOCITY_MAX_TOKEN_AGE = float(os.getenv('VELOCITY_MAX_TOKEN_AGE', '3.0'))

# ============================================
# TIMER-BASED EXIT SETTINGS (NEW)
# ============================================
# Base hold time in seconds (will add random variance)
TIMER_EXIT_BASE_SECONDS = int(os.getenv('TIMER_EXIT_BASE_SECONDS', '30'))

# Random variance to add (+/- seconds)
TIMER_EXIT_VARIANCE_SECONDS = int(os.getenv('TIMER_EXIT_VARIANCE_SECONDS', '5'))

# Extension for mega-pumps (if velocity still rising and P&L > threshold)
TIMER_EXTENSION_SECONDS = int(os.getenv('TIMER_EXTENSION_SECONDS', '10'))

# P&L threshold to consider extension (%)
TIMER_EXTENSION_PNL_THRESHOLD = float(os.getenv('TIMER_EXTENSION_PNL_THRESHOLD', '80'))

# Maximum total extensions allowed
TIMER_MAX_EXTENSIONS = int(os.getenv('TIMER_MAX_EXTENSIONS', '2'))

# ============================================
# LEGACY PARTIAL PROFIT SETTINGS (DEPRECATED)
# These are kept for backward compatibility but not used in timer mode
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

# Timing (legacy)
SELL_DELAY_SECONDS = int(os.getenv('SELL_DELAY_SECONDS', '0'))  # No grace period in timer mode
MAX_POSITION_AGE_SECONDS = int(os.getenv('MAX_HOLD_TIME_SEC', '120'))  # Fallback safety
MONITOR_CHECK_INTERVAL = int(os.getenv('MONITOR_CHECK_INTERVAL', '1'))  # Check every second
DATA_FAILURE_TOLERANCE = int(os.getenv('DATA_FAILURE_TOLERANCE', '10'))

# ============================================
# LIQUIDITY VALIDATION
# ============================================
# Require 5x liquidity (e.g. 0.15 SOL raised for 0.03 SOL buy)
LIQUIDITY_MULTIPLIER = float(os.getenv('LIQUIDITY_MULTIPLIER', '5.0'))
# Absolute minimum SOL raised
MIN_LIQUIDITY_SOL = float(os.getenv('MIN_LIQUIDITY_SOL', '0.6'))
# Maximum slippage tolerance
MAX_SLIPPAGE_PERCENT = float(os.getenv('MAX_SLIPPAGE_PERCENT', '2.5'))

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
