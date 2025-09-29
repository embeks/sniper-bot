"""
Jupiter V6 Trader - Execute swaps on graduated tokens
"""

import aiohttp
import json
import logging
import base64
from typing import Optional, Dict
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts

logger = logging.getLogger(__name__)

class JupiterTrader:
    """Execute trades via Jupiter aggregator"""
    
    def __init__(self, wallet_manager, client=None):
        self.wallet = wallet_manager
        if client:
            self.client = client
        else:
            from config import RPC_ENDPOINT
            self.client = Client(RPC_ENDPOINT)
        
        # Jupiter endpoints
        self.JUPITER_API_URL = "https://quote-api.jup.ag/v6"
        self.JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
        self.JUPITER_SLIPPAGE_BPS = 1000  # 10% slippage for volatile graduations
        
    async def get_quote(self, input_mint: str, output_mint: str, amount: int) -> Optional[Dict]:
        """Get a swap quote from Jupiter"""
        try:
            params = {
                'inputMint': input_mint,
                'outputMint': output_mint,
                'amount': str(amount),
                'slippageBps': self.JUPITER_SLIPPAGE_BPS,
                'onlyDirectRoutes': 'false',
                'asLegacyTransaction': 'false'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.JUPITER_API_URL}/quote", params=params) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        error = await response.text()
                        logger.error(f"Quote failed: {error}")
                        return None
                        
        except Exception as e:
            logger.error(f"Failed to get quote: {e}")
            return None
    
    async def create_swap_transaction(
        self,
        quote: Dict,
        user_public_key: str
    ) -> Optional[str]:
        """Create a swap transaction"""
        try:
            payload = {
                'quoteResponse': quote,
                'userPublicKey': user_public_key,
                'wrapAndUnwrapSol': True,
                'prioritizationFeeLamports': 'auto'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.JUPITER_SWAP_URL,
                    json=payload,
                    headers={'Content-Type': 'application/json'}
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get('swapTransaction')
                    else:
                        error = await response.text()
                        logger.error(f"Swap creation failed: {error}")
                        return None
                        
        except Exception as e:
            logger.error(f"Failed to create swap: {e}")
            return None
    
    async def create_buy_transaction(
        self,
        mint: str,
        sol_amount: float,
        bonding_curve_key: str = None,  # Not used for Jupiter, kept for compatibility
        slippage: int = 50  # Override with higher slippage
    ) -> Optional[str]:
        """Execute a buy transaction for graduated token - compatible with existing interface"""
        try:
            # Override slippage for graduations
            self.JUPITER_SLIPPAGE_BPS = slippage * 20  # Convert to BPS
            
            # SOL mint address
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Convert SOL to lamports
            amount_lamports = int(sol_amount * 1e9)
            
            logger.info(f"Getting Jupiter quote for {sol_amount} SOL -> {mint[:8]}...")
            
            # Get quote
            quote = await self.get_quote(sol_mint, mint, amount_lamports)
            if not quote:
                logger.error("Failed to get Jupiter quote")
                return None
            
            # Log expected output
            out_amount = int(quote.get('outAmount', 0))
            price_impact = quote.get('priceImpactPct', 0)
            logger.info(f"Expected tokens: {out_amount:,}")
            logger.info(f"Price impact: {price_impact}%")
            
            # Create transaction
            swap_tx = await self.create_swap_transaction(
                quote,
                str(self.wallet.pubkey)
            )
            
            if not swap_tx:
                logger.error("Failed to create swap transaction")
                return None
            
            # Decode and sign
            tx_bytes = base64.b64decode(swap_tx)
            
            # Parse and sign the versioned transaction
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])
            
            # Send transaction
            opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            response = self.client.send_raw_transaction(bytes(signed_tx), opts)
            signature = str(response.value)
            
            if signature and not signature.startswith("1111111"):
                logger.info(f"✅ Buy transaction sent via Jupiter: {signature}")
                return signature
            else:
                logger.error(f"Invalid signature returned: {signature}")
                return None
            
        except Exception as e:
            logger.error(f"Buy execution failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    async def create_sell_transaction(
        self,
        mint: str,
        token_amount: float,  # UI amount
        bonding_curve_key: str = None,  # Not used
        slippage: int = 50,
        token_decimals: int = 6
    ) -> Optional[str]:
        """Execute a sell transaction - compatible with existing interface"""
        try:
            # Override slippage
            self.JUPITER_SLIPPAGE_BPS = slippage * 20  # Convert to BPS
            
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Handle decimals tuple if needed
            if isinstance(token_decimals, tuple):
                token_decimals = token_decimals[0]
            
            # Convert UI amount to raw amount
            token_amount_raw = int(token_amount * (10 ** token_decimals))
            
            logger.info(f"Selling {token_amount:.2f} tokens ({token_amount_raw} raw) for SOL")
            
            # Get quote for selling tokens for SOL
            quote = await self.get_quote(mint, sol_mint, token_amount_raw)
            if not quote:
                logger.error("Failed to get sell quote")
                return None
            
            # Log expected SOL
            sol_out = int(quote.get('outAmount', 0)) / 1e9
            logger.info(f"Expected SOL: {sol_out:.4f}")
            
            # Create and send transaction
            swap_tx = await self.create_swap_transaction(
                quote,
                str(self.wallet.pubkey)
            )
            
            if not swap_tx:
                logger.error("Failed to create sell swap")
                return None
            
            tx_bytes = base64.b64decode(swap_tx)
            tx = VersionedTransaction.from_bytes(tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.wallet.keypair])
            
            opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
            response = self.client.send_raw_transaction(bytes(signed_tx), opts)
            signature = str(response.value)
            
            if signature and not signature.startswith("1111111"):
                logger.info(f"✅ Sell transaction sent via Jupiter: {signature}")
                return signature
            else:
                logger.error(f"Invalid signature returned: {signature}")
                return None
            
        except Exception as e:
            logger.error(f"Sell execution failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    async def get_price(self, token_mint: str) -> Optional[Dict]:
        """Get current price of token in USD"""
        try:
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Get 1 token quote (with 6 decimals assumed)
            quote = await self.get_quote(token_mint, sol_mint, 1_000_000)  
            
            if quote:
                sol_out = int(quote.get('outAmount', 0)) / 1e9
                price_usd = sol_out * 250  # Assuming $250/SOL
                
                return {
                    'price': price_usd,
                    'sol_price': sol_out
                }
            
            return None
            
        except Exception as e:
            logger.error(f"Price fetch failed: {e}")
            return None
