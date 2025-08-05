# raydium_aggregator.py - OPTIMIZED VERSION
import os
import json
import logging
import httpx
import struct
from typing import Optional, Dict, Any
import base64

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
RAYDIUM_AUTHORITY = Pubkey.from_string("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1")

# Compute Budget Program
COMPUTE_BUDGET_PROGRAM_ID = Pubkey.from_string("ComputeBudget111111111111111111111111111111")

class RaydiumAggregatorClient:
    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
        # Don't load pools - use on-demand lookup

    def find_pool_onchain(self, token_mint: str) -> Optional[Dict[str, Any]]:
        """Find Raydium pool directly on-chain without downloading the huge file."""
        try:
            sol_mint = "So11111111111111111111111111111111111111112"
            
            # Get all Raydium AMM accounts (no filters for older solana-py)
            accounts = self.client.get_program_accounts(
                str(RAYDIUM_AMM_PROGRAM_ID),
                encoding="base64"
            )
            
            if not accounts.get("result"):
                logging.warning("[Raydium] No program accounts found")
                return None
            
            logging.info(f"[Raydium] Searching through {len(accounts['result'])} Raydium accounts...")
            
            # Look through accounts to find one with our token
            for account_info in accounts["result"]:
                try:
                    # Check account size first
                    account_data = base64.b64decode(account_info["account"]["data"][0])
                    if len(account_data) != 752:  # Not a V4 pool
                        continue
                    
                    # Raydium V4 pool layout (simplified)
                    # Now we're at the pubkeys section (208 bytes in)
                    offset = 208
                    
                    # Read pool accounts
                    pool_id = account_info["pubkey"]
                    base_mint_bytes = account_data[offset:offset+32]
                    quote_mint_bytes = account_data[offset+32:offset+64]
                    
                    base_mint_str = str(Pubkey(base_mint_bytes))
                    quote_mint_str = str(Pubkey(quote_mint_bytes))
                    
                    # Check if this pool contains our token
                    if token_mint in [base_mint_str, quote_mint_str] and \
                       sol_mint in [base_mint_str, quote_mint_str]:
                        
                        # Extract other important accounts
                        base_vault = str(Pubkey(account_data[offset+64:offset+96]))
                        quote_vault = str(Pubkey(account_data[offset+96:offset+128]))
                        open_orders = str(Pubkey(account_data[offset+128:offset+160]))
                        market_id = str(Pubkey(account_data[offset+160:offset+192]))
                        market_program = str(Pubkey(account_data[offset+192:offset+224]))
                        target_orders = str(Pubkey(account_data[offset+224:offset+256]))
                        
                        logging.info(f"[Raydium] Found pool on-chain: {pool_id}")
                        
                        # Get market info
                        market_info = self._get_market_info(market_id)
                        
                        return {
                            "id": pool_id,
                            "baseMint": base_mint_str,
                            "quoteMint": quote_mint_str,
                            "baseVault": base_vault,
                            "quoteVault": quote_vault,
                            "openOrders": open_orders,
                            "targetOrders": target_orders,
                            "marketId": market_id,
                            "marketProgramId": market_program,
                            "authority": str(RAYDIUM_AUTHORITY),
                            "version": 4,
                            "programId": str(RAYDIUM_AMM_PROGRAM_ID),
                            # Market accounts from lookup
                            "marketAuthority": market_info.get("authority", "11111111111111111111111111111111"),
                            "marketBaseVault": market_info.get("baseVault", "11111111111111111111111111111111"),
                            "marketQuoteVault": market_info.get("quoteVault", "11111111111111111111111111111111"),
                            "marketBids": market_info.get("bids", "11111111111111111111111111111111"),
                            "marketAsks": market_info.get("asks", "11111111111111111111111111111111"),
                            "marketEventQueue": market_info.get("eventQueue", "11111111111111111111111111111111"),
                        }
                        
                except Exception as e:
                    continue
                    
            logging.warning(f"[Raydium] No pool found containing {token_mint[:8]}...")
            return None
            
        except Exception as e:
            logging.error(f"[Raydium] On-chain pool search failed: {e}")
            import traceback
            logging.error(traceback.format_exc())
            return None

    def _get_market_info(self, market_id: str) -> Dict[str, Any]:
        """Get Serum market info."""
        try:
            market_account = self.client.get_account_info(Pubkey.from_string(market_id))
            if market_account["result"]["value"]:
                data = base64.b64decode(market_account["result"]["value"]["data"][0])
                
                # Serum market layout (simplified)
                # Skip padding (5 + 8 + 8 + 8 + 8 = 37 bytes)
                offset = 37
                
                # Read market accounts
                base_vault = str(Pubkey(data[offset+16:offset+48]))
                quote_vault = str(Pubkey(data[offset+48:offset+80]))
                bids = str(Pubkey(data[offset+96:offset+128]))
                asks = str(Pubkey(data[offset+128:offset+160]))
                event_queue = str(Pubkey(data[offset+160:offset+192]))
                
                return {
                    "baseVault": base_vault,
                    "quoteVault": quote_vault,
                    "bids": bids,
                    "asks": asks,
                    "eventQueue": event_queue,
                    "authority": str(SERUM_PROGRAM_ID),  # Simplified
                }
        except:
            pass
        
        return {}

    def find_pool(self, input_mint: str, output_mint: str) -> Optional[Dict[str, Any]]:
        """Find pool using on-chain lookup."""
        sol_mint = "So11111111111111111111111111111111111111112"
        
        # Determine which is the token
        if input_mint == sol_mint:
            token_mint = output_mint
        elif output_mint == sol_mint:
            token_mint = input_mint
        else:
            logging.warning("[Raydium] Neither mint is SOL")
            return None
        
        # Try on-chain lookup
        pool = self.find_pool_onchain(token_mint)
        if pool:
            return pool
            
        # For well-known tokens, use hardcoded pools as fallback
        known_pools = {
            # RAY-SOL
            "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R": {
                "id": "AVs9TA4nWDzfPJE9gGVNJMVhcQy3V9PGazuz33BfG2RA",
                "baseMint": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
                "quoteMint": "So11111111111111111111111111111111111111112",
                "baseVault": "Em6rHi68trYgBFyJ5261A2nhwuQWfLcirgzZZYoRcrkX",
                "quoteVault": "3mEFzHsJyu2Cpjrz6zPmTzP7uoLFj9SbbecGVzzkL1mJ",
                "openOrders": "6Su6Ea97dBxecd5W92KcVvv6SzCurE2BXGgFe9LNGMpE",
                "targetOrders": "5hATcCfvhVwAjNExvrg8rRkXmYyksHhVajWLa46iRsmE",
                "marketId": "C6tp2RVZnxBPFbnAsfTjis8BN9tycESAT4SgDQgbbrsA",
                "marketAuthority": "7SdieGqwPJo5rMmSQM9JmntSEMoimM4dQn7NkGbNFcrd",
                "marketBaseVault": "6U6U59zmFWrPSzm9sLX7kVkaK78Kz7XJYkrhP1DjF3uF",
                "marketQuoteVault": "4YEx21yeUAZxUL9Fs7YU9Gm3u45GWoPFs8vcJiHga2eQ",
                "marketBids": "C1nEbACFaHMUiKAUsXVYPWZsuxunJeBkqXHPFr8QgSj9",
                "marketAsks": "4DNBdnTw6wmrK4NmdSTTxs1kEz47yjqLGuoqsMeHvkMF",
                "marketEventQueue": "4HGvdTqhYadgZ1YKrPfEfUvKGMGDnaPSvpEMnJ8kwGNt",
                "authority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                "version": 4,
                "programId": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
            },
            # USDC-SOL
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": {
                "id": "6a1CsrpeZubDjEJE9s1CMVheB6HWM5d7m1cj2jkhyXhj",
                "baseMint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "quoteMint": "So11111111111111111111111111111111111111112",
                "baseVault": "DQyrAcCrDXQ7NeoqGgDCZwBvWDcYmFCjSb9JtteuvPpz",
                "quoteVault": "HLmqeL62xR1QoZ1HKKbXRrdN1p3phKpxRMb2VVopvBBz",
                "openOrders": "75HKx8M5UBdp2wPLqLZqoWfPsqJnmhFCVFUe9yPg5FMa",
                "targetOrders": "3K5bWdYQZKYLEWi655X8bNVFXmJfnVVsu3wFYomKVYsu",
                "marketId": "8BnEgHoWFysVcuFFX7QztDmzuH8r5ZFvyP3sYwn1XTh6",
                "marketAuthority": "F8Vyqk3unwxkXukZFQeYyGmFfTG3CAX4v24iyrjEYBJV",
                "marketBaseVault": "9vYWHBPz817wJdQpE8u3h8UoY3sZ16ZXdCcvLB7jY4Dj",
                "marketQuoteVault": "6mJqqT5TMgveDvxzBt3hrjGkPV5VAj7tacxFCT3GebXh",
                "marketBids": "14ivtgssEBoBjuZJtSAPKYgpUK7DmnSwuPMqJoVTSgKJ",
                "marketAsks": "CEQdAFKdycHugujQg9k2wbmxjcpdYZyVLfV9WerTnafJ",
                "marketEventQueue": "5KKsLVU6TcbVDK4BS6K1DGDxnh4Q9xjYJ8XaDCG5t8ht",
                "authority": "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",
                "version": 4,
                "programId": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
            }
        }
        
        if token_mint in known_pools:
            logging.info(f"[Raydium] Using hardcoded pool for {token_mint[:8]}...")
            return known_pools[token_mint]
        
        logging.warning(f"[Raydium] No pool found for {token_mint[:8]}...")
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
                AccountMeta(pubkey=Pubkey.from_string(pool.get("authority", str(RAYDIUM_AUTHORITY))), is_signer=False, is_writable=False),
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
                AccountMeta(pubkey=Pubkey.from_string(pool.get("marketAuthority", str(SERUM_PROGRAM_ID))), is_signer=False, is_writable=False),
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
