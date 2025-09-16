# config.py - COMPLETE PRODUCTION VERSION WITH PHASE ONE PATCHES AND PUMPFUN SUPPORT
import os
from dataclasses import dataclass

def _b(name, default):
    v = os.getenv(name)
    return (str(v).lower() in ("1","true","yes","y","on")) if v is not None else default

def _f(name, default):
    try: return float(os.getenv(name, default))
    except: return default

def _i(name, default):
    try: return int(float(os.getenv(name, default)))
    except: return default

@dataclass(frozen=True)
class Config:
    # Core settings
    BUY_AMOUNT_SOL: float
    USE_DYNAMIC_SIZING: bool
    FORCE_JUPITER_SELL: bool
    SIMULATE_BEFORE_SEND: bool
    CACHE_TTL_SECONDS: int
    SELL_MULTIPLIERS: str
    SELL_TIMEOUT_SEC: int
    RUG_LP_THRESHOLD: float
    
    # Phase One: Buy-specific settings
    BUY_SLIPPAGE_BPS: int
    BUY_PRIORITY_FEE_LAMPORTS: int
    BUY_RETRY_DELAY_1_MS: int
    BUY_RETRY_DELAY_2_MS: int
    NEWBORN_RAYDIUM_MIN_LP_SOL: float
    
    # PumpFun Direct Buy settings
    PUMPFUN_PROGRAM_ID: str
    PUMPFUN_COMPUTE_UNIT_LIMIT: int
    PUMPFUN_PRIORITY_FEE_LAMPORTS: int
    PUMPFUN_REFERRER: str
    
    # Profit targets
    TAKE_PROFIT_1: float
    TAKE_PROFIT_2: float
    TAKE_PROFIT_3: float
    SELL_PERCENT_1: float
    SELL_PERCENT_2: float
    SELL_PERCENT_3: float
    
    # Risk management
    TRAILING_STOP_PERCENT: float
    MAX_HOLD_TIME_SEC: int
    
    # PumpFun strategy
    PUMPFUN_USE_MOON_STRATEGY: bool
    PUMPFUN_TAKE_PROFIT_1: float
    PUMPFUN_TAKE_PROFIT_2: float
    PUMPFUN_TAKE_PROFIT_3: float
    PUMPFUN_SELL_PERCENT_1: float
    PUMPFUN_SELL_PERCENT_2: float
    PUMPFUN_MOON_BAG: float
    NO_SELL_FIRST_MINUTES: int
    TRAILING_STOP_ACTIVATION: float
    
    # Trending strategy
    TRENDING_USE_CUSTOM: bool
    TRENDING_TAKE_PROFIT_1: float
    TRENDING_TAKE_PROFIT_2: float
    TRENDING_TAKE_PROFIT_3: float
    
    # Price handling
    OVERRIDE_DECIMALS_TO_9: bool
    IGNORE_JUPITER_PRICE_FIELD: bool
    LP_CHECK_TIMEOUT: int
    SOL_PRICE_USD: float
    
    # Scaling
    SCALE_WITH_BALANCE: bool
    MIGRATION_BOOST_MULTIPLIER: float
    TRENDING_BOOST_MULTIPLIER: float
    
    # Pre-trade safety
    MIN_LP_SOL: float
    REQUIRE_AUTH_RENOUNCED: bool
    MAX_TRADE_TAX_BPS: int
    
    # Stop-loss engine
    STOP_LOSS_PCT: float
    STOP_CHECK_INTERVAL_SEC: int
    STOP_MAX_SLIPPAGE_BPS: int
    STOP_EMERGENCY_SLIPPAGE_BPS: int
    STOP_MIN_OUT_BPS_FLOOR: int
    STOP_ALERT_EVERY_SEC: int
    ROUTE_AMOUNT_MODE: str
    
    # Alert switches - NEW CONSOLIDATED CONFIG
    ALERTS_NOTIFY: dict
    
    # API endpoints
    JUPITER_QUOTE_BASE_URL: str
    JUPITER_SWAP_URL: str
    ROUTE_TIMEOUT_SEC: int
    
    # Connection settings
    RPC_URL: str
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    TELEGRAM_USER_ID: str
    BIRDEYE_API_KEY: str
    HELIUS_API: str
    SOLANA_PRIVATE_KEY: str
    BLACKLISTED_TOKENS: str

def load() -> Config:
    # Parse alert config from env or use defaults
    alerts_notify = {
        "startup": _b("ALERT_STARTUP", True),
        "buy": _b("ALERT_ON_BUY", True),
        "sell": _b("ALERT_ON_SELL", True),
        "stop_triggered": _b("ALERT_ON_STOP_TRIGGER", True),
        "stop_filled": _b("ALERT_ON_STOP_FILLED", True),
        "buy_failed": _b("ALERT_ON_BUY_FAILED", False),
        "sell_failed": _b("ALERT_ON_SELL_FAILED", False),
        "blocked": _b("ALERT_ON_BLOCKED", False),
        "attempt": _b("ALERT_ON_ATTEMPT", False),
        "skip": _b("ALERT_ON_SKIP", False),
        "error": _b("ALERT_ON_ERROR", False),
        "cooldown_secs": _i("ALERT_COOLDOWN_SECS", 60)
    }
    
    return Config(
        # Core settings
        BUY_AMOUNT_SOL=_f("BUY_AMOUNT_SOL", 0.1),
        USE_DYNAMIC_SIZING=_b("USE_DYNAMIC_SIZING", True),
        FORCE_JUPITER_SELL=_b("FORCE_JUPITER_SELL", True),
        SIMULATE_BEFORE_SEND=_b("SIMULATE_BEFORE_SEND", True),
        CACHE_TTL_SECONDS=_i("POOL_CACHE_TTL", 60),
        SELL_MULTIPLIERS=os.getenv("SELL_MULTIPLIERS", "1.5,3,10"),
        SELL_TIMEOUT_SEC=_i("SELL_TIMEOUT_SEC", 300),
        RUG_LP_THRESHOLD=_f("RUG_LP_THRESHOLD", 1.0),
        
        # Phase One: Buy-specific settings
        BUY_SLIPPAGE_BPS=_i("BUY_SLIPPAGE_BPS", 2500),
        BUY_PRIORITY_FEE_LAMPORTS=_i("BUY_PRIORITY_FEE_LAMPORTS", 1000000),
        BUY_RETRY_DELAY_1_MS=_i("BUY_RETRY_DELAY_1_MS", 200),
        BUY_RETRY_DELAY_2_MS=_i("BUY_RETRY_DELAY_2_MS", 400),
        NEWBORN_RAYDIUM_MIN_LP_SOL=_f("NEWBORN_RAYDIUM_MIN_LP_SOL", 0.2),
        
        # PumpFun Direct Buy settings
        PUMPFUN_PROGRAM_ID=os.getenv("PUMPFUN_PROGRAM_ID", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"),
        PUMPFUN_COMPUTE_UNIT_LIMIT=_i("PUMPFUN_COMPUTE_UNIT_LIMIT", 1000000),
        PUMPFUN_PRIORITY_FEE_LAMPORTS=_i("PUMPFUN_PRIORITY_FEE_LAMPORTS", 1000000),
        PUMPFUN_REFERRER=os.getenv("PUMPFUN_REFERRER", ""),
        
        # Profit targets
        TAKE_PROFIT_1=_f("TAKE_PROFIT_1", 1.5),
        TAKE_PROFIT_2=_f("TAKE_PROFIT_2", 3.0),
        TAKE_PROFIT_3=_f("TAKE_PROFIT_3", 10.0),
        SELL_PERCENT_1=_f("SELL_PERCENT_1", 50),
        SELL_PERCENT_2=_f("SELL_PERCENT_2", 25),
        SELL_PERCENT_3=_f("SELL_PERCENT_3", 25),
        
        # Risk management
        TRAILING_STOP_PERCENT=_f("TRAILING_STOP_PERCENT", 20),
        MAX_HOLD_TIME_SEC=_i("MAX_HOLD_TIME_SEC", 3600),
        
        # PumpFun strategy
        PUMPFUN_USE_MOON_STRATEGY=_b("PUMPFUN_USE_MOON_STRATEGY", True),
        PUMPFUN_TAKE_PROFIT_1=_f("PUMPFUN_TAKE_PROFIT_1", 10.0),
        PUMPFUN_TAKE_PROFIT_2=_f("PUMPFUN_TAKE_PROFIT_2", 25.0),
        PUMPFUN_TAKE_PROFIT_3=_f("PUMPFUN_TAKE_PROFIT_3", 50.0),
        PUMPFUN_SELL_PERCENT_1=_f("PUMPFUN_SELL_PERCENT_1", 20),
        PUMPFUN_SELL_PERCENT_2=_f("PUMPFUN_SELL_PERCENT_2", 30),
        PUMPFUN_MOON_BAG=_f("PUMPFUN_MOON_BAG", 50),
        NO_SELL_FIRST_MINUTES=_i("NO_SELL_FIRST_MINUTES", 30),
        TRAILING_STOP_ACTIVATION=_f("TRAILING_STOP_ACTIVATION", 5.0),
        
        # Trending strategy
        TRENDING_USE_CUSTOM=_b("TRENDING_USE_CUSTOM", False),
        TRENDING_TAKE_PROFIT_1=_f("TRENDING_TAKE_PROFIT_1", 3.0),
        TRENDING_TAKE_PROFIT_2=_f("TRENDING_TAKE_PROFIT_2", 8.0),
        TRENDING_TAKE_PROFIT_3=_f("TRENDING_TAKE_PROFIT_3", 15.0),
        
        # Price handling
        OVERRIDE_DECIMALS_TO_9=_b("OVERRIDE_DECIMALS_TO_9", False),
        IGNORE_JUPITER_PRICE_FIELD=_b("IGNORE_JUPITER_PRICE_FIELD", False),
        LP_CHECK_TIMEOUT=_i("LP_CHECK_TIMEOUT", 3),
        SOL_PRICE_USD=_f("SOL_PRICE_USD", 150.0),
        
        # Scaling
        SCALE_WITH_BALANCE=_b("SCALE_WITH_BALANCE", True),
        MIGRATION_BOOST_MULTIPLIER=_f("MIGRATION_BOOST_MULTIPLIER", 2.0),
        TRENDING_BOOST_MULTIPLIER=_f("TRENDING_BOOST_MULTIPLIER", 1.5),
        
        # Pre-trade safety
        MIN_LP_SOL=_f("MIN_LP_SOL", 1.0),
        REQUIRE_AUTH_RENOUNCED=_b("REQUIRE_AUTH_RENOUNCED", True),
        MAX_TRADE_TAX_BPS=_i("MAX_TRADE_TAX_BPS", 300),
        
        # Stop-loss engine
        STOP_LOSS_PCT=_f("STOP_LOSS_PCT", 0.30),
        STOP_CHECK_INTERVAL_SEC=_i("STOP_CHECK_INTERVAL_SEC", 2),
        STOP_MAX_SLIPPAGE_BPS=_i("STOP_MAX_SLIPPAGE_BPS", 200),
        STOP_EMERGENCY_SLIPPAGE_BPS=_i("STOP_EMERGENCY_SLIPPAGE_BPS", 500),
        STOP_MIN_OUT_BPS_FLOOR=_i("STOP_MIN_OUT_BPS_FLOOR", 10),
        STOP_ALERT_EVERY_SEC=_i("STOP_ALERT_EVERY_SEC", 30),
        ROUTE_AMOUNT_MODE=os.getenv("ROUTE_AMOUNT_MODE", "POSITION"),
        
        # Alert switches
        ALERTS_NOTIFY=alerts_notify,
        
        # API endpoints
        JUPITER_QUOTE_BASE_URL=os.getenv("JUPITER_QUOTE_BASE_URL", "https://quote-api.jup.ag/v6/quote"),
        JUPITER_SWAP_URL=os.getenv("JUPITER_SWAP_URL", "https://quote-api.jup.ag/v6/swap"),
        ROUTE_TIMEOUT_SEC=_i("ROUTE_TIMEOUT_SEC", 20),
        
        # Connection settings
        RPC_URL=os.getenv("RPC_URL", ""),
        TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID", ""),
        TELEGRAM_USER_ID=os.getenv("TELEGRAM_USER_ID", ""),
        BIRDEYE_API_KEY=os.getenv("BIRDEYE_API_KEY", ""),
        HELIUS_API=os.getenv("HELIUS_API", ""),
        SOLANA_PRIVATE_KEY=os.getenv("SOLANA_PRIVATE_KEY", ""),
        BLACKLISTED_TOKENS=os.getenv("BLACKLISTED_TOKENS", ""),
    )
