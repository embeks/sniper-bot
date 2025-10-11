"""
Jupiter v6 Swapper - Buy/Sell any SPL token
"""

import aiohttp
import base64
import logging
from typing import Optional
from solana.rpc.types import TxOpts

logger = logging.getLogger(__name__)

# Solana native mint (wrapped SOL)
SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterSwapper:
    """Jupiter v6 aggregator for token swaps"""
    
    def __init__(self, wallet_manager, client, config):
        self.wallet = wallet_manager
        self.client = client
        self.config = config
        self.quote_url = "https://quote-api.jup.ag/v6/quote"
        self.swap_url = "https://quote-api.jup.ag/v6/swap"
    
    async def buy_token(
        self,
        token_mint: str,
        amount_sol: float,
        slippage_bps: int = 300
    ) -> Optional[str]:
        """
        Buy token with SOL using Jupiter
        
        Args:
            token_mint: Token to buy
            amount_sol: Amount of SOL to spend
            slippage_bps: Slippage in basis points (300 = 3%)
        
        Returns:
            Transaction signature or None
        """
        try:
            amount_lamports = int(amount_sol * 1e9)
            
            logger.info(f"🔍 Getting Jupiter quote: {amount_sol} SOL → {token_mint[:8]}...")
            
            # Get quote
            quote = await self._get_quote(
                input_mint=SOL_MINT,
                output_mint=token_mint,
                amount=amount_lamports,
                slippage_bps=slippage_bps
            )
            
            if not quote:
                logger.error("Failed to get quote from Jupiter")
                return None
            
            out_amount = int(quote.get('outAmount', 0))
            logger.info(f"Quote: {amount_sol} SOL → {out_amount:,} tokens")
            
            # Get swap transaction
            signature = await self._execute_swap(quote, slippage_bps)
            
            if signature:
                logger.info(f"✅ Buy executed: {signature}")
            
            return signature
            
        except Exception as e:
            logger.error(f"Buy error: {e}")
            return None
    
    async def sell_token(
        self,
        token_mint: str,
        amount_tokens: float,
        slippage_bps: int = 500
    ) -> Optional[str]:
        """
        Sell token for SOL using Jupiter
        
        Args:
            token_mint: Token to sell
            amount_tokens: Amount of tokens to sell (UI amount)
            slippage_bps: Slippage in basis points (500 = 5%)
        
        Returns:
            Transaction signature or None
        """
        try:
            # Get token decimals
            decimals = self.wallet.get_token_decimals(token_mint)
            amount_raw = int(amount_tokens * (10 ** decimals))
            
            logger.info(f"🔍 Getting Jupiter quote: {amount_tokens:,.0f} tokens → SOL")
            
            # Get quote
            quote = await self._get_quote(
                input_mint=token_mint,
                output_mint=SOL_MINT,
                amount=amount_raw,
                slippage_bps=slippage_bps
            )
            
            if not quote:
                logger.error("Failed to get quote from Jupiter")
                return None
            
            out_amount_lamports = int(quote.get('outAmount', 0))
            out_amount_sol = out_amount_lamports / 1e9
            logger.info(f"Quote: {amount_tokens:,.0f} tokens → {out_amount_sol:.4f} SOL")
            
            # Get swap transaction
            signature = await self._execute_swap(quote, slippage_bps)
            
            if signature:
                logger.info(f"✅ Sell executed: {signature}")
            
            return signature
            
        except Exception as e:
            logger.error(f"Sell error: {e}")
            return None
    
    async def _get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int
    ) -> Optional[dict]:
        """Get quote from Jupiter"""
        try:
            params = {
                'inputMint': input_mint,
                'outputMint': output_mint,
                'amount': str(amount),
                'slippageBps': str(slippage_bps)
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(self.quote_url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"Quote API error ({resp.status}): {error_text}")
                        return None
                    
                    quote = await resp.json()
                    logger.debug(f"Got quote: {quote}")
                    return quote
                    
        except Exception as e:
            logger.error(f"Failed to get quote: {e}")
            return None
    
    async def _execute_swap(self, quote: dict, slippage_bps: int) -> Optional[str]:
        """Execute swap using quote"""
        try:
            wallet_pubkey = str(self.wallet.pubkey)
            
            payload = {
                'quoteResponse': quote,
                'userPublicKey': wallet_pubkey,
                'wrapAndUnwrapSol': True,
                'dynamicComputeUnitLimit': True,
                'prioritizationFeeLamports': 'auto'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.swap_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"Swap API error ({resp.status}): {error_text}")
                        return None
                    
                    swap_response = await resp.json()
                    swap_transaction = swap_response.get('swapTransaction')
                    
                    if not swap_transaction:
                        logger.error("No swapTransaction in response")
                        return None
                    
                    # Decode and sign transaction
                    tx_bytes = base64.b64decode(swap_transaction)
                    
                    # Check if it's a versioned transaction
                    is_versioned = (tx_bytes[0] & 0x80) != 0
                    
                    if is_versioned:
                        from solders.transaction import VersionedTransaction
                        
                        unsigned_tx = VersionedTransaction.from_bytes(tx_bytes)
                        message = unsigned_tx.message
                        signed_tx = VersionedTransaction(message, [self.wallet.keypair])
                        signed_tx_bytes = bytes(signed_tx)
                    else:
                        # Legacy transaction
                        from solana.transaction import Transaction
                        tx = Transaction.deserialize(tx_bytes)
                        tx.sign_partial([self.wallet.keypair])
                        signed_tx_bytes = tx.serialize()
                    
                    # Send transaction
                    opts = TxOpts(skip_preflight=True, preflight_commitment="processed")
                    response = self.client.send_raw_transaction(signed_tx_bytes, opts)
                    sig = str(response.value)
                    
                    if sig.startswith("1111111"):
                        logger.error("Transaction failed - invalid signature")
                        return None
                    
                    logger.info(f"Transaction sent: {sig}")
                    return sig
                    
        except Exception as e:
            logger.error(f"Failed to execute swap: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
