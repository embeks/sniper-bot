"""
Phase 1 Configuration - Minimal PumpFun Bonding Curve Sniper
Focus: Catch launches, execute trades, prove profitability
UPDATED: Extended monitoring window and grace period
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
# PHASE 1 TRADING PARAMETERS
# ============================================
# Position sizing
BUY_AMOUNT_SOL = float(os.getenv('BUY_AMOUNT_SOL', '0.02'))
PUMPFUN_EARLY_AMOUNT = float(os.getenv('PUMPFUN_EARLY_AMOUNT', BUY_AMOUNT_SOL))
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '10'))
MIN_SOL_BALANCE = float(os.getenv('MIN_SOL_BALANCE', '0.05'))  # Changed to 0.05 SOL minimum

# Risk management
STOP_LOSS_PERCENTAGE = float(os.getenv('STOP_LOSS_PERCENT', '50'))
TAKE_PROFIT_PERCENTAGE = float(os.getenv('TAKE_PROFIT_1', '200')) / 100 * 100  # Convert to percentage

# Partial profit taking from env - FIXED key collision
PARTIAL_TAKE_PROFIT = {}
tp1, sp1 = os.getenv('TAKE_PROFIT_1'), os.getenv('SELL_PERCENT_1')
tp2, sp2 = os.getenv('TAKE_PROFIT_2'), os.getenv('SELL_PERCENT_2')
tp3, sp3 = os.getenv('TAKE_PROFIT_3'), os.getenv('SELL_PERCENT_3')

if tp1 and sp1:
    PARTIAL_TAKE_PROFIT[float(tp1) * 100] = float(sp1) / 100.0  # e.g., 200 for 2.0x
if tp2 and sp2:
    PARTIAL_TAKE_PROFIT[float(tp2) * 100] = float(sp2) / 100.0  # e.g., 300 for 3.0x
if tp3 and sp3:
    PARTIAL_TAKE_PROFIT[float(tp3) * 100] = float(sp3) / 100.0  # e.g., 500 for 5.0x

# Timing - UPDATED FOR ENHANCED MONITORING
SELL_DELAY_SECONDS = int(os.getenv('SELL_DELAY_SECONDS', '15'))  # Grace period before allowing sells
MAX_POSITION_AGE_SECONDS = int(os.getenv('MAX_HOLD_TIME_SEC', '180'))  # 3 minutes
MONITOR_CHECK_INTERVAL = int(os.getenv('MONITOR_CHECK_INTERVAL', '2'))  # Check every 2 seconds
DATA_FAILURE_TOLERANCE = int(os.getenv('DATA_FAILURE_TOLERANCE', '10'))  # Allow 10 consecutive failures

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
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')  # Back to INFO level
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_FILE = None  # No file logging, only console output

# ============================================
# DEVELOPMENT/TESTING
# ============================================
DRY_RUN = os.getenv('DRY_RUN', 'false').lower() == 'true'
DEBUG_MODE = os.getenv('DEBUG', 'false').lower() == 'true'

if DRY_RUN:
    print("⚠️ DRY RUN MODE - No real transactions will be executed")
if DEBUG_MODE:
    print("🔍 DEBUG MODE - Verbose logging enabled")
