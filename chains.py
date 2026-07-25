"""Supported chains (Etherscan V2 multi-chain API: one key for all)."""

CHAINS = {
    "eth": {
        "chain_id": 1,
        "name": "Ethereum",
        "explorer": "https://etherscan.io",
        "native": "ETH",
    },
    "bsc": {
        "chain_id": 56,
        "name": "BNB Chain",
        "explorer": "https://bscscan.com",
        "native": "BNB",
    },
    "base": {
        "chain_id": 8453,
        "name": "Base",
        "explorer": "https://basescan.org",
        "native": "ETH",
    },
    "arb": {
        "chain_id": 42161,
        "name": "Arbitrum One",
        "explorer": "https://arbiscan.io",
        "native": "ETH",
    },
    "polygon": {
        "chain_id": 137,
        "name": "Polygon",
        "explorer": "https://polygonscan.com",
        "native": "POL",
    },
}

DEFAULT_CHAIN = "bsc"


def resolve_chain(alias: str | None) -> str | None:
    """Return canonical chain key for a user-supplied alias, or None."""
    if not alias:
        return DEFAULT_CHAIN
    alias = alias.lower().strip()
    aliases = {
        "eth": "eth", "ethereum": "eth", "1": "eth",
        "bsc": "bsc", "bnb": "bsc", "bep20": "bsc", "56": "bsc",
        "base": "base", "8453": "base",
        "arb": "arb", "arbitrum": "arb", "42161": "arb",
        "polygon": "polygon", "matic": "polygon", "pol": "polygon", "137": "polygon",
    }
    return aliases.get(alias)
