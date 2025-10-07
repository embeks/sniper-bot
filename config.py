
"""
config - Path B: MC + Holder Strategy
"""

import os
from solders.pubkey import Pubkey
from dotenv import load_dotenv

# Load environment variables
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

# Backup RPC endpoints
BACKUP_RPC_ENDPOINTS = [
    os.getenv('BACKUP_RPC_1', 'https://api.mainnet-beta.solana.com'),
    os.getenv('BACKUP_RPC_2', 'https://solana-api.projectserum.com')
]

# ============================================
# PHASE 1.5 TRADING PARAMETERS - PATH B
# ============================================
# Position sizing
BUY_AMOUNT_SOL = float(os.getenv('BUY_AMOUNT_SOL', '0.01'))
PUMPFUN_EARLY_AMOUNT = float(os.getenv('PUMPFUN_EARLY_AMOUNT', BUY_AMOUNT_SOL))
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '10'))
MIN_SOL_BALANCE = float(os.getenv('MIN_SOL_BALANCE', '0.05'))

# Risk management - PATH B
STOP_LOSS_PERCENTAGE = float(os.getenv('STOP_LOSS_PERCENT', '35'))
TAKE_PROFIT_PERCENTAGE = float(os.getenv('TAKE_PROFIT_1', '200')) / 100 * 100

# Partial profit taking from env
PARTIAL_TAKE_PROFIT = {}
tp1, sp1 = os.getenv('TAKE_PROFIT_1'), os.getenv('SELL_PERCENT_1')
tp2, sp2 = os.getenv('TAKE_PROFIT_2'), os.getenv('SELL_PERCENT_2')
tp3, sp3 = os.getenv('TAKE_PROFIT_3'), os.getenv('SELL_PERCENT_3')

if tp1 and sp1:
    PARTIAL_TAKE_PROFIT[float(tp1)] = float(sp1) / 100.0  # REMOVED * 100
if tp2 and sp2:
    PARTIAL_TAKE_PROFIT[float(tp2)] = float(sp2) / 100.0  # REMOVED * 100
if tp3 and sp3:
    PARTIAL_TAKE_PROFIT[float(tp3)] = float(sp3) / 100.0  # REMOVED * 100

# Timing - PATH B
SELL_DELAY_SECONDS = int(os.getenv('SELL_DELAY_SECONDS', '15'))
MAX_POSITION_AGE_SECONDS = int(os.getenv('MAX_HOLD_TIME_SEC', '180'))
MONITOR_CHECK_INTERVAL = int(os.getenv('MONITOR_CHECK_INTERVAL', '2'))
DATA_FAILURE_TOLERANCE = int(os.getenv('DATA_FAILURE_TOLERANCE', '10'))

# ============================================
# PUMPFUN SPECIFIC CONFIGURATION
# ============================================
PUMPFUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
PUMPFUN_FEE_RECIPIENT = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

# Bonding curve parameters - PATH B: Higher minimums
MIN_BONDING_CURVE_SOL = 25.0  # CHANGED: Up from 1.5 - wait for Helius indexing
MAX_BONDING_CURVE_SOL = 60.0  # CHANGED: Down from 85 - exit before peak
MIGRATION_THRESHOLD_SOL = 85

# Buy criteria
MIN_VIRTUAL_SOL_RESERVES = 30
MIN_VIRTUAL_TOKEN_RESERVES = 1_000_000_000
MAX_PRICE_IMPACT_PERCENTAGE = 5

# Auto buy setting
AUTO_BUY = os.getenv('AUTO_BUY', 'true').lower() == 'true'
PUMPFUN_EARLY_BUY = os.getenv('PUMPFUN_EARLY_BUY', 'true').lower() == 'true'

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
    str(PUMPFUN_PROGRAM_ID),
    str(RAYDIUM_PROGRAM_ID)
]

# Log filters
LOG_CONTAINS_FILTERS = [
    "Program log: Instruction: InitializeBondingCurve",
    "Program log: Instruction: Buy",
    "Program log: Instruction: Sell",
    "initialize2"
]

# ============================================
# TOKEN FILTERS (PATH B - MC + HOLDER BASED)
# ============================================
# Blacklisted tokens (known rugs/scams)
BLACKLISTED_TOKENS = set()

# Required token metadata
REQUIRE_METADATA = True
REQUIRE_SOCIAL_LINKS = False
MIN_HOLDER_COUNT = 60  # CHANGED: Up from 0 - require real distribution

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

# Notification triggers
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
