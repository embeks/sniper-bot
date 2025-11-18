
"""
config - FINAL: All fixes applied + VELOCITY AGE FIX + PROFIT PROTECTION
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
# Approximate SOL price (used only if Birdeye API fails)
APPROX_SOL_PRICE_USD = 235.0  # used only if Birdeye fails

# FIXED: Increased from 0.01 to 0.05 SOL (fees require larger positions)
BUY_AMOUNT_SOL = float(os.getenv('BUY_AMOUNT_SOL', '0.05'))
PUMPFUN_EARLY_AMOUNT = float(os.getenv('PUMPFUN_EARLY_AMOUNT', BUY_AMOUNT_SOL))
MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '2'))
MIN_SOL_BALANCE = float(os.getenv('MIN_SOL_BALANCE', '0.05'))

# Risk management
STOP_LOSS_PERCENTAGE = float(os.getenv('STOP_LOSS_PERCENT', '25'))
TAKE_PROFIT_PERCENTAGE = float(os.getenv('TAKE_PROFIT_1', '200')) / 100 * 100

# ============================================
# VELOCITY GATE SETTINGS
# ============================================
# Minimum SOL/second inflow rate to enter
VELOCITY_MIN_SOL_PER_SECOND = float(os.getenv('VELOCITY_MIN_SOL_PER_SECOND', '2.5'))

# Minimum unique buyers required (estimated)
VELOCITY_MIN_BUYERS = int(os.getenv('VELOCITY_MIN_BUYERS', '5'))

# CRITICAL FIX: Increased from 6.0 to 25.0 to account for monitor delays
# Timeline: 0.5s cooldown + 3s sleep + 0-6s retries + processing = ~10-20s
VELOCITY_MAX_TOKEN_AGE = float(os.getenv('VELOCITY_MAX_TOKEN_AGE', '15.0'))

# Recent velocity thresholds (last 1-3 seconds)
VELOCITY_MIN_RECENT_1S_SOL = float(os.getenv('VELOCITY_MIN_RECENT_1S_SOL', '2.5'))
VELOCITY_MIN_RECENT_3S_SOL = float(os.getenv('VELOCITY_MIN_RECENT_3S_SOL', '5.0'))

# FIXED: Tightened from 40% to 25% (reject if velocity dropping >25%)
VELOCITY_MAX_DROP_PERCENT = float(os.getenv('VELOCITY_MAX_DROP_PERCENT', '25.0'))

# CRITICAL FIX: Changed from 2 to 1 (allow buy on first detection)
VELOCITY_MIN_SNAPSHOTS = int(os.getenv('VELOCITY_MIN_SNAPSHOTS', '1'))

# ============================================
# NEW: VELOCITY CEILING (BOT PUMP PROTECTION)
# ============================================
# Maximum average velocity - reject parabolic bot pumps
# Whale wins: 0.38 SOL/s (organic) vs Your losses: 14.2 SOL/s (bot pump)
VELOCITY_MAX_SOL_PER_SECOND = float(os.getenv('VELOCITY_MAX_SOL_PER_SECOND', '8.0'))

# Maximum recent velocity - reject parabolic spikes
VELOCITY_MAX_RECENT_1S_SOL = float(os.getenv('VELOCITY_MAX_RECENT_1S_SOL', '8.0'))
VELOCITY_MAX_RECENT_3S_SOL = float(os.getenv('VELOCITY_MAX_RECENT_3S_SOL', '12.0'))

# ============================================
# TIMER-BASED EXIT SETTINGS
# ============================================
# FIXED: Shortened from 30s to 20s base
TIMER_EXIT_BASE_SECONDS = int(os.getenv('TIMER_EXIT_BASE_SECONDS', '20'))

# Random variance to add (+/- seconds)
TIMER_EXIT_VARIANCE_SECONDS = int(os.getenv('TIMER_EXIT_VARIANCE_SECONDS', '5'))

# Extension for mega-pumps (if velocity still rising and P&L > threshold)
TIMER_EXTENSION_SECONDS = int(os.getenv('TIMER_EXTENSION_SECONDS', '10'))

# P&L threshold to consider extension (%)
TIMER_EXTENSION_PNL_THRESHOLD = float(os.getenv('TIMER_EXTENSION_PNL_THRESHOLD', '60'))

# Maximum total extensions allowed
TIMER_MAX_EXTENSIONS = int(os.getenv('TIMER_MAX_EXTENSIONS', '1'))

# ============================================
# MOMENTUM EXIT SETTINGS
# ============================================
# Peak drawdown before exit (percentage points)
MOMENTUM_MAX_DRAWDOWN_PP = float(os.getenv('MOMENTUM_MAX_DRAWDOWN_PP', '25.0'))

# Minimum peak required before drawdown matters
MOMENTUM_MIN_PEAK_PERCENT = float(os.getenv('MOMENTUM_MIN_PEAK_PERCENT', '15.0'))

# Minimum age before drawdown exit applies (seconds)
MOMENTUM_DRAWDOWN_MIN_AGE = float(os.getenv('MOMENTUM_DRAWDOWN_MIN_AGE', '15.0'))

# Velocity death threshold (% of pre-buy velocity)
MOMENTUM_VELOCITY_DEATH_PERCENT = float(os.getenv('MOMENTUM_VELOCITY_DEATH_PERCENT', '50.0'))

# Big win take profit threshold
MOMENTUM_BIG_WIN_PERCENT = float(os.getenv('MOMENTUM_BIG_WIN_PERCENT', '70.0'))

# Max hold time backstop (seconds)
MOMENTUM_MAX_HOLD_SECONDS = float(os.getenv('MOMENTUM_MAX_HOLD_SECONDS', '45.0'))

# ============================================
# CONSOLIDATION PROTECTION SETTINGS (NEW!)
# ============================================
# Prevent selling during healthy consolidations
# Consolidation floor - don't exit if price is above this % of peak
CONSOLIDATION_FLOOR_PERCENT = float(os.getenv('CONSOLIDATION_FLOOR_PERCENT', '0.65'))

# First pump hold period - ignore stop loss during early development
FIRST_PUMP_HOLD_SECONDS = float(os.getenv('FIRST_PUMP_HOLD_SECONDS', '30.0'))

# HOW CONSOLIDATION PROTECTION WORKS:
#
# BEFORE (what was breaking):
# - Token pumps to $10K (+100%)
# - Dips to $7K (-30% from peak)
# - Bot sees -30% drop → PANIC SELLS
# - Token recovers to $11K
# - You sold the dip like a paper-handed degen
#
# AFTER (whale behavior):
# - Token pumps to $10K (+100%)
# - Dips to $7K = 70% of peak
# - 70% > 65% floor → HEALTHY CONSOLIDATION
# - Bot HOLDS through the dip
# - Token pumps to $11K
# - You catch the second leg like a whale
#
# TUNING GUIDE:
# Conservative (tighter - more exits):
#   CONSOLIDATION_FLOOR_PERCENT = 0.75  # Exit if below 75% of peak
#   FIRST_PUMP_HOLD_SECONDS = 20.0      # Shorter hold period
#
# Moderate (recommended):
#   CONSOLIDATION_FLOOR_PERCENT = 0.65  # Exit if below 65% of peak
#   FIRST_PUMP_HOLD_SECONDS = 30.0      # Standard hold period
#
# Aggressive (let it ride):
#   CONSOLIDATION_FLOOR_PERCENT = 0.55  # Exit if below 55% of peak
#   FIRST_PUMP_HOLD_SECONDS = 40.0      # Longer hold period

# ============================================
# PROFIT PROTECTION SETTINGS (NEW!)
# ============================================
# These work alongside your timer-based exits to protect extreme gains
# All checks are CHAIN-GATED (only trigger on blockchain data, not WebSocket)

# Extreme Take-Profit: Exit immediately if profit goes parabolic
# Example: If you hit +150% (2.5x), lock it in regardless of timer
# Set to 999.0 to disable
EXTREME_TP_PERCENT = float(os.getenv('EXTREME_TP_PERCENT', '150.0'))

# Trailing Stop: Protect profits after hitting a certain level
# TRAIL_START_PERCENT: Start trailing once you've hit this profit
# TRAIL_GIVEBACK_PERCENT: Exit if you give back this much from peak
# Example: Hit +191% peak, currently +85% = 106pp drop → Exit if drop >= 50pp
# Set both to 999.0 to disable
TRAIL_START_PERCENT = float(os.getenv('TRAIL_START_PERCENT', '70.0'))
TRAIL_GIVEBACK_PERCENT = float(os.getenv('TRAIL_GIVEBACK_PERCENT', '35.0'))

# ============================================
# HOW PROFIT PROTECTION WORKS:
# ============================================
# 
# Priority order (from highest to lowest):
# 1. Extreme TP (150%+) - Locks in parabolic gains immediately
# 2. Trailing Stop (100%+ then -50pp drop) - Protects from fast rugs
# 3. Fail-Fast (5s check at -10%) - Exits early losers
# 4. Rug Trap (-40% or -60% if <3s) - Emergency exits
# 5. Stop Loss (-40%) - Standard loss protection  
# 6. Timer Exit (20s) - Your main strategy (80% of trades)
#
# Most trades (80%): Exit on timer as normal
# Extreme pumps (10%): Exit early via Extreme TP or Trailing Stop
# Fast rugs (10%): Exit early via Fail-Fast or Rug Trap
#
# All profit exits require CHAIN confirmation (same as stop-loss)
# This prevents WebSocket false signals from triggering exits
#
# EXAMPLE WITH YOUR ACTUAL TRADE (GOLDALON):
# Without profit protection:
#   06:29:43 - Peak: +191.7% (bot keeps holding)
#   06:30:04 - Timer exit: -0.8%
#   Result: -0.0085 SOL loss
#
# With Extreme TP = 150%:
#   06:29:43 - Peak: +191.7%
#   → +191.7% >= 150% ✓ on [chain] tick
#   → EXTREME TP TRIGGERED!
#   → Exit at +191.7% (~2s after buy)
#   Result: +0.0383 SOL profit (+0.0468 SOL better!)
#
# TUNING GUIDE:
# Conservative (lock profits early):
#   EXTREME_TP_PERCENT = 100.0      # Exit at 2x
#   TRAIL_START_PERCENT = 75.0      # Trail after 1.75x
#   TRAIL_GIVEBACK_PERCENT = 30.0   # Tighter trail
#
# Moderate (recommended):
#   EXTREME_TP_PERCENT = 150.0      # Exit at 2.5x
#   TRAIL_START_PERCENT = 100.0     # Trail after 2x
#   TRAIL_GIVEBACK_PERCENT = 50.0   # Exit if -50pp from peak
#
# Aggressive (let winners run):
#   EXTREME_TP_PERCENT = 200.0      # Exit at 3x
#   TRAIL_START_PERCENT = 150.0     # Trail after 2.5x
#   TRAIL_GIVEBACK_PERCENT = 70.0   # Wider trail
#
# Disabled (pure timer strategy):
#   EXTREME_TP_PERCENT = 999.0      # Never trigger
#   TRAIL_START_PERCENT = 999.0     # Never trigger
#   TRAIL_GIVEBACK_PERCENT = 999.0  # Never trigger
#
# ============================================

# ============================================
# FAIL-FAST EXIT SETTINGS
# ============================================
# Time after buy to check for early failure (seconds)
FAIL_FAST_CHECK_TIME = float(os.getenv('FAIL_FAST_CHECK_TIME', '3.0'))

# P&L threshold for early exit (%)
FAIL_FAST_PNL_THRESHOLD = float(os.getenv('FAIL_FAST_PNL_THRESHOLD', '-5.0'))

# Velocity death threshold (% of pre-buy velocity)
FAIL_FAST_VELOCITY_THRESHOLD = float(os.getenv('FAIL_FAST_VELOCITY_THRESHOLD', '40.0'))

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

# Timing
SELL_DELAY_SECONDS = int(os.getenv('SELL_DELAY_SECONDS', '0'))
MAX_POSITION_AGE_SECONDS = int(os.getenv('MAX_HOLD_TIME_SEC', '60'))

# FIXED: Changed from 1s to 0.5s for more precise fail-fast timing
MONITOR_CHECK_INTERVAL = float(os.getenv('MONITOR_CHECK_INTERVAL', '0.5'))
DATA_FAILURE_TOLERANCE = int(os.getenv('DATA_FAILURE_TOLERANCE', '10'))

# ============================================
# LIQUIDITY VALIDATION
# ============================================
# Require 5x liquidity (e.g. 0.25 SOL raised for 0.05 SOL buy)
LIQUIDITY_MULTIPLIER = float(os.getenv('LIQUIDITY_MULTIPLIER', '5.0'))
# Absolute minimum SOL raised
MIN_LIQUIDITY_SOL = float(os.getenv('MIN_LIQUIDITY_SOL', '0.6'))
# Maximum slippage tolerance
MAX_SLIPPAGE_PERCENT = float(os.getenv('MAX_SLIPPAGE_PERCENT', '2.5'))

# ============================================
# SLIPPAGE PROTECTION
# ============================================
# Maximum allowed slippage between estimated and actual entry price
# If actual slippage exceeds this, immediately exit the position
# This protects against buying during bot swarms and price spikes
MAX_ENTRY_SLIPPAGE_PERCENT = float(os.getenv('MAX_ENTRY_SLIPPAGE_PERCENT', '40.0'))

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
