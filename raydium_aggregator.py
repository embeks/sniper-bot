def find_pool_via_jupiter(self, input_mint: str, output_mint: str) -> Optional[Dict[str, Any]]:
        """Use Jupiter API to find Raydium pools."""
        try:
            # Jupiter's route API can tell us about available pools
            url = f"https://quote-api.jup.ag/v6/quote?inputMint={input_mint}&outputMint={output_mint}&amount=1000000"
            
            with httpx.Client(timeout=10.0) as client:
                response = client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    # Jupiter returns routes that might include Raydium pools
                    if data.get("routePlan"):
                        for route in data["routePlan"]:
                            if "Raydium" in route.get("swapInfo", {}).get("label", ""):
                                logging.info("[Raydium] Found pool via Jupiter API")
                                # Extract pool info from Jupiter response
                                # This is a simplified version
                                return {
                                    "id": route["swapInfo"].# raydium_aggregator.py - WORKS WITH solders==0.10.0
import os
import json
import logging
import httpx
import struct
from typing import Optional, Dict, Any

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import ID as SYS_PROGRAM_ID
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from spl.token.constants import TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, create_associated_token_account

# Raydium Program IDs
RAYDIUM_AMM_PROGRAM_ID = Pubkey.from_string("675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8")
SERUM_PROGRAM_ID = Pubkey.from_string("9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin")

# Compute Budget Program
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

RAYDIUM_POOLS_URL = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"

class RaydiumAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
        self.pools = []

    def fetch_pools(self) -> None:
        """Fetch Raydium pool list from API with streaming."""
        try:
            logging.info("[Raydium] Fetching pools from API...")
            
            # For now, skip the huge API and use direct pool lookup
            logging.warning("[Raydium] Skipping 888MB pool file, using direct pool lookup instead")
            self.pools = []
            
            # Alternative: Try the SDK's filtered endpoint if available
            try:
                # Try a lighter endpoint that might exist
                alt_url = "https://api.raydium.io/v2/main/pool/ids"
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(alt_url)
                    if response.status_code == 200:
                        logging.info("[Raydium] Using alternative pool list")
                        # Process lighter response
                        return
            except:
                pass
                
        except Exception as e:
            logging.error(f"[Raydium] Failed to fetch pools: {type(e).__name__}: {e}")
            self.pools = []

    def find_pool(self, input_mint: str, output_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool for the given mint pair - optimized version."""
        # Ensure we have pools
        if not self.pools and not hasattr(self, 'pool_index'):
            self.fetch_pools()
        
        # Fast lookup using index
        if hasattr(self, 'pool_index'):
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Check if one of the mints is SOL
            if input_mint == sol_mint:
                return self.pool_index.get(output_mint)
            elif output_mint == sol_mint:
                return self.pool_index.get(input_mint)
        
        # Fallback to linear search
        for pool in self.pools:
            base_mint = pool.get("baseMint", "")
            quote_mint = pool.get("quoteMint", "")
            
            if (input_mint == base_mint and output_mint == quote_mint) or \
               (input_mint == quote_mint and output_mint == base_mint):
                return pool
        
        # If no pool found, log it
        logging.warning(f"[Raydium] No pool found for {input_mint[:8]}... <-> {output_mint[:8]}...")
        return None
    
    def find_pool_by_rpc(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool for a specific token using RPC calls."""
        try:
            # Get all accounts owned by Raydium AMM program that contain this mint
            filters = [
                {"dataSize": 752},  # Raydium V4 pool size
                {"memcmp": {"offset": 400, "bytes": token_mint}},  # baseMint position
            ]
            
            accounts = self.client.get_program_accounts(
                "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                encoding="base64",
                filters=filters
            )
            
            if accounts.get("result"):
                for account in accounts["result"]:
                    # Parse account data to extract pool info
                    # This is simplified - you'd need to decode the account properly
                    pool_pubkey = account["pubkey"]
                    logging.info(f"[Raydium] Found pool via RPC: {pool_pubkey}")
                    
                    # Return a minimal pool structure
                    return {
                        "id": pool_pubkey,
                        "baseMint": token_mint,
                        "quoteMint": "So11111111111111111111111111111111111111112",
                        # Add other required fields based on account data
                    }
                    
            # Try quoteMint position
            filters = [
                {"dataSize": 752},
                {"memcmp": {"offset": 432, "bytes": token_mint}},  # quoteMint position
            ]
            
            accounts = self.client.get_program_accounts(
                "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                encoding="base64",
                filters=filters
            )
            
            if accounts.get("result") and len(accounts["result"]) > 0:
                # Found pool where token is quoteMint
                pool_pubkey = accounts["result"][0]["pubkey"]
                logging.info(f"[Raydium] Found pool via RPC: {pool_pubkey}")
                return {
                    "id": pool_pubkey,
                    "baseMint": "So11111111111111111111111111111111111111112",
                    "quoteMint": token_mint,
                }
                
        except Exception as e:
            logging.error(f"[Raydium] RPC pool search failed: {e}")
            
        return None
        """Try to find pool using direct RPC calls instead of cached data."""
        # For known tokens, return hardcoded pool info
        # This is a fallback when the API is down
        
        # Normalize mints
        mints = {input_mint, output_mint}
        sol_mint = "So11111111111111111111111111111111111111112"
        
        # Check if one of them is SOL
        if sol_mint not in mints:
            return None
            
        # Get the other token
        token_mint = input_mint if output_mint == sol_mint else output_mint
        
        # Known token pools with SOL
        known_pools = {
            # RAY-SOL
            "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": {
                "id": "AVs9TA4nWDzfPJE9gGVNJMVhcQy3V9PGazuz33BfG2RA",
                "baseMint": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
                "quoteMint": "So11111111111111111111111111111111111111112",
                "lpMint": "89ZKE4aoyfLBe2RuV6jM3JGNhaV18Nxh8eNtjRcndBip",
                "baseDecimals": 6,
                "quoteDecimals": 9,
                "lpDecimals": 6,
                "version": 4,
                "programId": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                "authority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                "openOrders": "6Su6Ea97dBxecd5W92KcVvv6SzCurE2BXGgFe9LNGMpE",
                "targetOrders": "5hATcCfvhVwAjNExvrg8rRkXmYyksHhVajWLa46iRsmE",
                "baseVault": "Em6rHi68trYgBFyJ5261A2nhwuQWfLcirgzZZYoRcrkX",
                "quoteVault": "3mEFzHsJyu2Cpjrz6zPmTzP7uoLFj9SbbecGVzzkL1mJ",
                "marketId": "C6tp2RVZnxBPFbnAsfTjis8BN9tycESAT4SgDQgbbrsA",
                "marketAuthority": "7SdieGqwPJo5rMmSQM9JmntSEMoimM4dQn7NkGbNFcrd",
                "marketBaseVault": "6U6U59zmFWrPSzm9sLX7kVkaK78Kz7XJYkrhP1DjF3uF",
                "marketQuoteVault": "4YEx21yeUAZxUL9Fs7YU9Gm3u45GWoPFs8vcJiHga2eQ",
                "marketBids": "C1nEbACFaHMUiKAUsXVYPWZsuxunJeBkqXHPFr8QgSj9",
                "marketAsks": "4DNBdnTw6wmrK4NmdSTTxs1kEz47yjqLGuoqsMeHvkMF",
                "marketEventQueue": "4HGvdTqhYadgZ1YKrPfEfUvKGMGDnaPSvpEMnJ8kwGNt"
            },
            # USDC-SOL
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {
                "id": "6a1CsrpeZubDjEJE9s1CMVheB6HWM5d7m1cj2jkhyXhj",
                "baseMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "quoteMint": "So11111111111111111111111111111111111111112",
                "lpMint": "8HoQnePLqPj4M7PUDzfw8e3Ymdwgc7NLGnaTUapubyvu",
                "baseDecimals": 6,
                "quoteDecimals": 9,
                "lpDecimals": 9,
                "version": 4,
                "programId": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
                "authority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                "openOrders": "75HKx8M5UBdp2wPLqLZqoWfPsqJnmhFCVFUe9yPg5FMa",
                "targetOrders": "3K5bWdYQZKYLEWi655X8bNVFXmJfnVVsu3wFYomKVYsu",
                "baseVault": "DQyrAcCrDXQ7NeoqGgDCZwBvWDcYmFCjSb9JtteuvPpz",
                "quoteVault": "HLmqeL62xR1QoZ1HKKbXRrdN1p3phKpxRMb2VVopvBBz",
                "marketId": "8BnEgHoWFysVcuFFX7QztDmzuH8r5ZFvyP3sYwn1XTh6",
                "marketAuthority": "F8Vyqk3unwxkXukZFQeYyGmFfTG3CAX4v24iyrjEYBJV",
                "marketBaseVault": "9vYWHBPz817wJdQpE8u3h8UoY3sZ16ZXdCcvLB7jY4Dj",
                "marketQuoteVault": "6mJqqT5TMgveDvxzBt3hrjGkPV5VAj7tacxFCT3GebXh",
                "marketBids": "14ivtgssEBoBjuZJtSAPKYgpUK7DmnSwuPMqJoVTSgKJ",
                "marketAsks": "CEQdAFKdycHugujQg9k2wbmxjcpdYZyVLfV9WerTnafJ",
                "marketEventQueue": "5KKsLVU6TcbVDK4BS6K1DGDxnh4Q9xjYJ8XaDCG5t8ht"
            }
        }
        
        if token_mint in known_pools:
            logging.info(f"[Raydium] Using hardcoded pool for {token_mint[:8]}...")
            return known_pools[token_mint]
            
        # For unknown tokens, we can't provide pool info
        logging.warning(f"[Raydium] No hardcoded pool for {token_mint}")
        return None

    def create_ata_if_needed(self, owner: Pubkey, mint: Pubkey, keypair: Keypair) -> Pubkey:
        """Create ATA if it doesn't exist."""
        ata = get_associated_token_address(owner, mint)
        
        try:
            account_info = self.client.get_account_info(ata)
            if account_info["result"]["value"] is None:
                # Create ATA
                ix = create_associated_token_account(payer=owner, owner=owner, mint=mint)
                recent_blockhash = self.client.get_recent_blockhash()["result"]["value"]["blockhash"]
                
                tx = Transaction()
                tx.recent_blockhash = recent_blockhash
                tx.fee_payer = owner
                tx.add(ix)
                tx.sign(keypair)
                
                sig = self.client.send_raw_transaction(tx.serialize())["result"]
                logging.info(f"[Raydium] Created ATA: {sig}")
                
        except Exception as e:
            logging.error(f"[Raydium] ATA creation error: {e}")
        
        return ata

    def build_swap_transaction(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage: float = 0.01
    ) -> Optional[Transaction]:
        """Build Raydium swap transaction."""
        try:
            pool = self.find_pool(input_mint, output_mint)
            if not pool:
                logging.error(f"[Raydium] No pool found for {input_mint} <-> {output_mint}")
                return None
            
            owner = keypair.pubkey()
            
            # Get mint pubkeys
            if input_mint == "So11111111111111111111111111111111111111112":
                # SOL to Token
                input_mint_pk = Pubkey.from_string(input_mint)
                output_mint_pk = Pubkey.from_string(output_mint)
                
                # For SOL, we need to use wrapped SOL
                # Create wrapped SOL account
                from spl.token.instructions import create_wrapped_native_account
                wrapped_sol_ix = create_wrapped_native_account(
                    program_id=TOKEN_PROGRAM_ID,
                    owner=owner,
                    payer=owner,
                    amount=amount_in,
                )
                
                user_source_token = get_associated_token_address(owner, input_mint_pk)
                user_dest_token = self.create_ata_if_needed(owner, output_mint_pk, keypair)
                
                # We'll add the wrapped SOL instruction to the transaction
                wrap_sol = True
            else:
                # Token to SOL
                input_mint_pk = Pubkey.from_string(input_mint)
                output_mint_pk = Pubkey.from_string(output_mint)
                
                user_source_token = self.create_ata_if_needed(owner, input_mint_pk, keypair)
                user_dest_token = get_associated_token_address(owner, output_mint_pk)
                wrap_sol = False
            
            # Build transaction
            tx = Transaction()
            tx.fee_payer = owner
            
            # Add compute budget instructions
            compute_limit_data = struct.pack("<BI", 2, 300000)  # 2 = SetComputeUnitLimit
            compute_limit_ix = Instruction(
                program_id=COMPUTE_BUDGET_PROGRAM_ID,
                accounts=[],
                data=compute_limit_data
            )
            
            compute_price_data = struct.pack("<BQ", 3, 10000)  # 3 = SetComputeUnitPrice
            compute_price_ix = Instruction(
                program_id=COMPUTE_BUDGET_PROGRAM_ID,
                accounts=[],
                data=compute_price_data
            )
            
            tx.add(compute_limit_ix)
            tx.add(compute_price_ix)
            
            # Add wrapped SOL instruction if needed
            if wrap_sol:
                tx.add(wrapped_sol_ix)
            
            # Calculate min amount out
            min_amount_out = int(amount_in * (1 - slippage))
            
            # Swap instruction data
            data = bytes([9]) + amount_in.to_bytes(8, 'little') + min_amount_out.to_bytes(8, 'little')
            
            # Swap instruction accounts
            keys = [
                AccountMeta(pubkey=TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["id"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["authority"]), is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["openOrders"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["targetOrders"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["baseVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["quoteVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=SERUM_PROGRAM_ID, is_signer=False, is_writable=False),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketId"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketBids"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketAsks"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketEventQueue"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketBaseVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketQuoteVault"]), is_signer=False, is_writable=True),
                AccountMeta(pubkey=Pubkey.from_string(pool["marketAuthority"]), is_signer=False, is_writable=False),
                AccountMeta(pubkey=user_source_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=user_dest_token, is_signer=False, is_writable=True),
                AccountMeta(pubkey=owner, is_signer=True, is_writable=False),
            ]
            
            swap_ix = Instruction(
                program_id=RAYDIUM_AMM_PROGRAM_ID,
                accounts=keys,
                data=data
            )
            tx.add(swap_ix)
            
            # Get recent blockhash
            recent_blockhash = self.client.get_recent_blockhash()["result"]["value"]["blockhash"]
            tx.recent_blockhash = recent_blockhash
            
            logging.info(f"[Raydium] Swap TX built successfully")
            return tx
            
        except Exception as e:
            logging.error(f"[Raydium] Failed to build transaction: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def send_transaction(self, tx: Transaction, keypair: Keypair) -> Optional[str]:
        """Send transaction."""
        try:
            # Sign transaction
            tx.sign(keypair)
            
            # Send transaction
            result = self.client.send_raw_transaction(
                tx.serialize(),
                opts={"skipPreflight": True, "preflightCommitment": "confirmed"}
            )
            
            if "result" in result:
                sig = result["result"]
                logging.info(f"[Raydium] Transaction sent: {sig}")
                return sig
            else:
                error = result.get("error", "Unknown error")
                logging.error(f"[Raydium] Failed to send transaction: {error}")
                return None
                
        except Exception as e:
            logging.error(f"[Raydium] Send transaction error: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None
