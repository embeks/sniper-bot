# raydium_aggregator.py (FULL LIVE RAYDIUM SWAP, PRODUCTION)
import os
import json
import httpx
import logging

from solana.rpc.api import Client
from solana.publickey import PublicKey
from solana.transaction import Transaction, TransactionInstruction, AccountMeta
from solana.rpc.types import TxOpts
from solders.keypair import Keypair
from spl.token.instructions import get_associated_token_address, create_associated_token_account
from solana.system_program import SYS_PROGRAM_ID

RAYDIUM_AMM_PROGRAM_ID = PublicKey("RVKd61ztZW9jqhDXnTBu6UBFygcBPzjcZijMdtaiPqK")
RAYDIUM_POOLS_URL = "https://api.raydium.io/v2/sdk/liquidity/mainnet.json"

class RaydiumAggregatorClient:
    def __init__(self, rpc_url):
        self.rpc_url = rpc_url
        self.client = Client(rpc_url)
        self.pools = None

    def fetch_pools(self):
        """Download and cache Raydium pool list from Raydium API."""
        try:
            r = httpx.get(RAYDIUM_POOLS_URL, timeout=10)
            r.raise_for_status()
            pools_data = r.json()
            # Raydium mainnet API may provide "official" or "unOfficial"
            self.pools = (pools_data.get("official") or []) + (pools_data.get("unOfficial") or [])
            logging.info(f"[Raydium] Pools loaded: {len(self.pools)}")
        except Exception as e:
            logging.error(f"[Raydium] Failed to fetch pools: {e}")
            self.pools = []

    def find_pool(self, input_mint, output_mint):
        if self.pools is None or not self.pools:
            self.fetch_pools()
        # Retry if not found
        for _ in range(2):
            for pool in self.pools:
                coins = (pool["baseMint"], pool["quoteMint"])
                if (input_mint in coins) and (output_mint in coins):
                    return pool
            # Refresh and retry
            self.fetch_pools()
        return None

    def create_ata_if_missing(self, owner, mint, keypair):
        ata = get_associated_token_address(owner, mint)
        res = self.client.get_account_info(ata)
        if res["result"]["value"] is None:
            logging.info(f"[Raydium] Creating ATA for {str(mint)}")
            tx = Transaction()
            tx.add(create_associated_token_account(owner, owner, mint))
            tx.recent_blockhash = self.client.get_latest_blockhash()["result"]["value"]["blockhash"]
            tx.fee_payer = owner
            tx.sign([keypair])
            self.client.send_raw_transaction(bytes(tx), opts=TxOpts(skip_preflight=True))

    def build_swap_transaction(
        self,
        keypair: Keypair,
        input_mint: str,
        output_mint: str,
        amount_in: int,
        slippage: float = 0.10
    ):
        pool = self.find_pool(input_mint, output_mint)
        if not pool:
            logging.warning(f"[Raydium] No pool found for {input_mint} -> {output_mint}")
            return None

        owner = keypair.pubkey()
        # Find correct in/out for pool
        if input_mint == pool["baseMint"]:
            in_token_account = get_associated_token_address(owner, PublicKey(pool["baseMint"]))
            out_token_account = get_associated_token_address(owner, PublicKey(pool["quoteMint"]))
            market_side = 0  # base to quote
        else:
            in_token_account = get_associated_token_address(owner, PublicKey(pool["quoteMint"]))
            out_token_account = get_associated_token_address(owner, PublicKey(pool["baseMint"]))
            market_side = 1  # quote to base

        # Create ATA if missing for output
        self.create_ata_if_missing(owner, PublicKey(output_mint), keypair)

        # Prepare swap instruction (Raydium AMM V3 layout)
        keys = [
            AccountMeta(pubkey=owner, is_signer=True, is_writable=True),
            AccountMeta(pubkey=in_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=out_token_account, is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["baseVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["quoteVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["id"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["openOrders"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["market"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["marketBids"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["marketAsks"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["marketEventQueue"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["marketBaseVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["marketQuoteVault"]), is_signer=False, is_writable=True),
            AccountMeta(pubkey=PublicKey(pool["marketAuthority"]), is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
            AccountMeta(pubkey=PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"), is_signer=False, is_writable=False),
            AccountMeta(pubkey=PublicKey("11111111111111111111111111111111"), is_signer=False, is_writable=False),  # Rent sysvar
        ]

        # Raydium swap instruction layout (V3): tag + amount_in + min_amount_out + side
        tag = 9  # Swap
        min_amount_out = int(amount_in * (1 - slippage))
        data = (
            tag.to_bytes(1, "little")
            + amount_in.to_bytes(8, "little")
            + min_amount_out.to_bytes(8, "little")
            + market_side.to_bytes(1, "little")
        )

        ix = TransactionInstruction(
            program_id=RAYDIUM_AMM_PROGRAM_ID,
            keys=keys,
            data=data
        )
        tx = Transaction()
        tx.add(ix)
        return tx

    def send_transaction(self, tx: Transaction, keypair: Keypair):
        try:
            tx.recent_blockhash = self.client.get_latest_blockhash()["result"]["value"]["blockhash"]
            tx.fee_payer = keypair.pubkey()
            tx.sign([keypair])
            sig = self.client.send_raw_transaction(bytes(tx), opts=TxOpts(skip_preflight=True))
            logging.info(f"[Raydium] TX sent: {sig}")
            return sig
        except Exception as e:
            logging.error(f"[Raydium] TX failed: {e}")
            return None
