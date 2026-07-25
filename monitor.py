"""Etherscan V2 polling and Telegram message formatting."""

import asyncio
import html
import logging
import os
import time
from decimal import Decimal

import httpx

from chains import CHAINS
from storage import Watch

log = logging.getLogger(__name__)

API_URL = "https://api.etherscan.io/v2/api"
API_KEY = os.environ.get("ETHERSCAN_API_KEY", "")

# free tier is 5 req/s — space calls out a little
REQUEST_GAP = float(os.environ.get("REQUEST_GAP", "0.25"))
MAX_ALERTS_PER_POLL = int(os.environ.get("MAX_ALERTS_PER_POLL", "8"))


class EtherscanError(Exception):
    pass


# Etherscan 免费套餐不覆盖的链自动降级到备用数据源。
# 每条链一组候选 (url, 是否带 apikey),逐个探测,第一个可用的胜出。
# 可用 FALLBACK_API_<链> 环境变量插队自定义,如 FALLBACK_API_BSC=https://xxx/api
FALLBACKS: dict[str, list[tuple[str, bool]]] = {
    "eth": [("https://eth.blockscout.com/api", False)],
    "bsc": [("https://bsc.blockscout.com/api", False),
            ("https://api.bscscan.com/api", True),
            ("https://api.bscscan.com/api", False)],
    "base": [("https://base.blockscout.com/api", False)],
    "arb": [("https://arbitrum.blockscout.com/api", False)],
    "polygon": [("https://polygon.blockscout.com/api", False)],
}
_fallback_chains: set[str] = set()
_fallback_urls: dict[str, tuple[str, bool]] = {}  # 探测成功后缓存


def _candidates(chain: str) -> list[tuple[str, bool]]:
    lst: list[tuple[str, bool]] = []
    env = os.environ.get(f"FALLBACK_API_{chain.upper()}")
    if env:
        lst.append((env.rstrip("/"), False))
    lst.extend(FALLBACKS.get(chain, []))
    return lst


def _plan_blocked(text) -> bool:
    t = str(text).lower()
    return ("not supported for this chain" in t
            or "upgrade your api plan" in t)


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    resp = await client.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _parse_list(data: dict) -> list[dict]:
    if data.get("status") == "1":
        return data.get("result") or []
    message = str(data.get("message", ""))
    result = data.get("result")
    if "No transactions found" in message or result == []:
        return []
    if "Invalid API Key" in str(result) or "Invalid API Key" in message:
        raise EtherscanError(
            "Etherscan API Key 无效。请在 Railway 的 Variables 里把 "
            "ETHERSCAN_API_KEY 换成 etherscan.io/myapikey 申请的 key"
            "(V2 多链通用,BscScan 旧 key 不可用)")
    raise EtherscanError(f"{message}: {result}")


async def _fallback_do(client: httpx.AsyncClient, url: str, with_key: bool,
                       params: dict) -> list[dict]:
    p = dict(params)
    if with_key:
        p["apikey"] = API_KEY
    else:
        p.pop("apikey", None)
    # Blockscout 的 tokentx 需要 address 参数;只有 contractaddress 时
    # (代币合约监控)改走它的 v2 接口查该代币的全部转账
    if ("blockscout" in url and p.get("action") == "tokentx"
            and "contractaddress" in p and "address" not in p):
        return await _blockscout_token_transfers(client, url, p)
    data = await _get_json(client, url, p)
    return _parse_list(data)


async def _fallback_query(client: httpx.AsyncClient, chain: str,
                          params: dict) -> list[dict]:
    if chain in _fallback_urls:
        url, with_key = _fallback_urls[chain]
        return await _fallback_do(client, url, with_key, params)
    errors = []
    for url, with_key in _candidates(chain):
        try:
            rows = await _fallback_do(client, url, with_key, params)
            _fallback_urls[chain] = (url, with_key)
            log.info("chain %s using fallback source %s", chain, url)
            return rows
        except Exception as e:
            errors.append(f"{url}: {str(e)[:80]}")
        await asyncio.sleep(REQUEST_GAP)
    raise EtherscanError(
        f"{CHAINS[chain]['name']} 的备用数据源均不可用: " + " | ".join(errors))


async def _query(client: httpx.AsyncClient, chain: str, params: dict) -> list[dict]:
    if chain not in _fallback_chains:
        data = await _get_json(client, API_URL, {
            "chainid": CHAINS[chain]["chain_id"],
            "apikey": API_KEY,
            **params,
        })
        try:
            return _parse_list(data)
        except EtherscanError as e:
            if _plan_blocked(e) and _candidates(chain):
                _fallback_chains.add(chain)
                log.info("chain %s not in Etherscan free plan, "
                         "switching to fallback sources", chain)
            else:
                raise
    if _candidates(chain):
        return await _fallback_query(client, chain, params)
    raise EtherscanError(
        f"{CHAINS[chain]['name']} 不在 Etherscan 免费套餐内,且暂无备用数据源")


def _adapt_v2_transfer(item: dict) -> dict:
    """把 Blockscout v2 transfer 条目转成 etherscan tokentx 行格式。"""
    total = item.get("total") or {}
    token = item.get("token") or {}
    ts = 0
    raw_ts = item.get("timestamp")
    if raw_ts:
        from datetime import datetime
        try:
            ts = int(datetime.fromisoformat(
                str(raw_ts).replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return {
        "hash": item.get("transaction_hash") or item.get("tx_hash") or "",
        "blockNumber": str(item.get("block_number") or 0),
        "timeStamp": str(ts),
        "from": (item.get("from") or {}).get("hash", ""),
        "to": (item.get("to") or {}).get("hash", ""),
        "value": str(total.get("value") or "0"),
        "tokenSymbol": token.get("symbol") or "",
        "tokenDecimal": str(total.get("decimals")
                            or token.get("decimals") or 18),
        "logIndex": str(item.get("log_index") or ""),
        "transactionIndex": "0",
    }


async def _blockscout_token_transfers(client: httpx.AsyncClient, api_url: str,
                                      params: dict) -> list[dict]:
    base_url = api_url.rsplit("/api", 1)[0]
    url = f"{base_url}/api/v2/tokens/{params['contractaddress']}/transfers"
    data = await _get_json(client, url, {})
    items = data.get("items") or []
    rows = [_adapt_v2_transfer(i) for i in items]
    start = int(params.get("startblock", 0) or 0)
    rows = [r for r in rows if int(r["blockNumber"] or 0) >= start]
    limit = int(params.get("offset", 50) or 50)
    return rows[:limit]  # v2 默认新→旧排序,取最新的一页


async def fetch_new_txs(client: httpx.AsyncClient, watch: Watch) -> list[dict]:
    """Fetch transactions for a watch since its last seen block.

    Returns a list of dicts, each tagged with "_type": "native" | "token".
    """
    start = max(watch.last_block, 0)
    common = {
        "module": "account",
        "startblock": start,
        "endblock": 999999999,
        "page": 1,
        "offset": 50,
        "sort": "asc",
    }
    txs: list[dict] = []

    if watch.kind == "token":
        rows = await _query(client, watch.chain, {
            **common, "action": "tokentx", "contractaddress": watch.address,
        })
        for r in rows:
            r["_type"] = "token"
        txs.extend(rows)
    else:
        rows = await _query(client, watch.chain, {
            **common, "action": "txlist", "address": watch.address,
        })
        for r in rows:
            r["_type"] = "native"
        txs.extend(rows)

        await asyncio.sleep(REQUEST_GAP)
        rows = await _query(client, watch.chain, {
            **common, "action": "tokentx", "address": watch.address,
        })
        for r in rows:
            r["_type"] = "token"
        txs.extend(rows)

    fresh = []
    for tx in txs:
        key = f'{tx.get("hash", "")}:{tx.get("_type")}:{tx.get("logIndex", "")}:{tx.get("from", "")}:{tx.get("to", "")}:{tx.get("value", "")}'
        if key in watch.seen:
            continue
        watch.remember(key)
        fresh.append(tx)
        block = int(tx.get("blockNumber", 0) or 0)
        if block > watch.last_block:
            watch.last_block = block

    fresh.sort(key=lambda t: (int(t.get("blockNumber", 0) or 0),
                              int(t.get("transactionIndex", 0) or 0)))
    return fresh


async def _proxy(client: httpx.AsyncClient, chain: str, action: str,
                 extra: dict | None = None):
    """proxy 模块 (JSON-RPC 透传),返回 result 原文;免费套餐不覆盖时走备用源。"""
    params = {"module": "proxy", "action": action, **(extra or {})}
    if chain not in _fallback_chains:
        data = await _get_json(client, API_URL, {
            "chainid": CHAINS[chain]["chain_id"],
            "apikey": API_KEY,
            **params,
        })
        result = data.get("result")
        if not (_plan_blocked(result) or _plan_blocked(data.get("message", ""))):
            return result
        if _candidates(chain):
            _fallback_chains.add(chain)
            log.info("chain %s not in Etherscan free plan, "
                     "switching to fallback sources", chain)
        else:
            return result
    # 已探测出的源优先,否则依次尝试候选
    tried = ([_fallback_urls[chain]] if chain in _fallback_urls else []) \
        + [c for c in _candidates(chain)
           if c != _fallback_urls.get(chain)]
    for url, with_key in tried:
        p = dict(params)
        if with_key:
            p["apikey"] = API_KEY
        try:
            data = await _get_json(client, url, p)
            res = data.get("result")
            if isinstance(res, str) and res.startswith("0x"):
                return res
        except Exception as e:
            log.debug("proxy fallback %s failed: %s", url, e)
        await asyncio.sleep(REQUEST_GAP)
    return None


def _source_name(chain: str) -> str:
    if chain not in _fallback_chains:
        return "Etherscan"
    if chain in _fallback_urls:
        from urllib.parse import urlparse
        return urlparse(_fallback_urls[chain][0]).netloc
    return "备用源"


async def check_chain(chain: str) -> tuple[int, str]:
    """连通性检测:返回 (最新区块, 数据源名)。失败抛异常。"""
    async with httpx.AsyncClient() as client:
        res = await _proxy(client, chain, "eth_blockNumber")
    block = int(str(res), 16)  # 出错时 res 是错误文本/None,这里会抛异常
    return block, _source_name(chain)


async def detect_address(address: str) -> dict[str, dict]:
    """识别地址在每条链上的类型: eoa(普通地址) / contract(合约) / token(代币合约)。

    有合约代码 → contract;合约且有代币转账记录 → token(带 symbol)。
    """
    result: dict[str, dict] = {}
    async with httpx.AsyncClient() as client:
        for chain in CHAINS:
            info = {"type": "eoa", "symbol": None}
            code = None
            try:
                code = await _proxy(client, chain, "eth_getCode",
                                    {"address": address, "tag": "latest"})
            except Exception as e:
                log.debug("getCode failed on %s: %s", chain, e)
            is_contract = (isinstance(code, str) and code.startswith("0x")
                           and len(code) > 4)
            # getCode 拿不到明确结果(接口不可用/被套餐限制)时也去探测代币转账,
            # 避免像 Base 这种链把代币合约误判成普通地址
            code_unknown = not (isinstance(code, str) and code.startswith("0x"))
            if is_contract or code_unknown:
                await asyncio.sleep(REQUEST_GAP)
                try:
                    rows = await _query(client, chain, {
                        "module": "account", "action": "tokentx",
                        "contractaddress": address,
                        "page": 1, "offset": 1, "sort": "desc",
                        "startblock": 0, "endblock": 999999999,
                    })
                    if rows:
                        info["type"] = "token"
                        info["symbol"] = rows[0].get("tokenSymbol") or None
                    elif is_contract:
                        info["type"] = "contract"
                except Exception as e:
                    log.debug("tokentx probe failed on %s: %s", chain, e)
                    if is_contract:
                        info["type"] = "contract"
            result[chain] = info
            await asyncio.sleep(REQUEST_GAP)
    return result


async def fetch_recent(chain: str, address: str, limit: int = 10) -> list[dict]:
    """Fetch the latest transactions (native + token) for an address, newest first."""
    common = {
        "module": "account",
        "startblock": 0,
        "endblock": 999999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
    }
    async with httpx.AsyncClient() as client:
        native = await _query(client, chain, {**common, "action": "txlist", "address": address})
        for r in native:
            r["_type"] = "native"
        await asyncio.sleep(REQUEST_GAP)
        token = await _query(client, chain, {**common, "action": "tokentx", "address": address})
        for r in token:
            r["_type"] = "token"
    merged = native + token
    merged.sort(key=lambda t: (int(t.get("blockNumber", 0) or 0),
                               int(t.get("timeStamp", 0) or 0)), reverse=True)
    return merged[:limit]


def _age(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}秒前"
    if seconds < 3600:
        return f"{seconds // 60}分钟前"
    if seconds < 86400:
        return f"{seconds // 3600}小时前"
    return f"{seconds // 86400}天前"


def format_recent(chain_key: str, address: str, txs: list[dict]) -> str:
    chain = CHAINS[chain_key]
    explorer = chain["explorer"]
    target = address.lower()
    header = (f"📜 <b>[{chain['name']}]</b> "
              f"<a href=\"{explorer}/address/{address}\">{_short(target)}</a> "
              f"近 {len(txs)} 条交易")
    if not txs:
        return header + "\n\n(没有查到交易)"
    now = time.time()
    lines = []
    for tx in txs:
        sender = (tx.get("from") or "").lower()
        receiver = (tx.get("to") or "").lower()
        if receiver == target and sender != target:
            arrow = "📥"
        elif sender == target and receiver != target:
            arrow = "📤"
        else:
            arrow = "🔁"
        if tx.get("_type") == "token":
            symbol = html.escape(tx.get("tokenSymbol") or "TOKEN")
            amount = f"{_amount(tx.get('value', '0'), int(tx.get('tokenDecimal') or 18))} {symbol}"
        else:
            amount = f"{_amount(tx.get('value', '0'), 18)} {chain['native']}"
            if tx.get("isError") == "1":
                arrow += "⚠️"
        ts = int(tx.get("timeStamp", 0) or 0)
        age = _age(now - ts) if ts else "?"
        lines.append(
            f"{arrow} <b>{amount}</b> · {_short(sender)} → {_short(receiver)}"
            f" · {age} · <a href=\"{explorer}/tx/{tx.get('hash', '')}\">查看</a>")
    return header + "\n\n" + "\n".join(lines)


def _short(addr: str) -> str:
    if not addr:
        return "—"
    return addr[:8] + "…" + addr[-6:] if len(addr) > 16 else addr


def _amount(raw: str, decimals: int) -> str:
    try:
        value = Decimal(raw or "0") / (Decimal(10) ** decimals)
    except Exception:
        return raw or "0"
    if value == 0:
        return "0"
    text = f"{value:,.6f}".rstrip("0").rstrip(".")
    return text or "0"


def format_tx(watch: Watch, tx: dict) -> str:
    chain = CHAINS[watch.chain]
    explorer = chain["explorer"]
    tx_hash = tx.get("hash", "")
    sender = (tx.get("from") or "").lower()
    receiver = (tx.get("to") or "").lower()
    target = watch.address

    if watch.kind == "token":
        direction = "🔄 代币转账"
    elif sender == target and receiver == target:
        direction = "🔁 自转"
    elif sender == target:
        direction = "📤 转出"
    elif receiver == target:
        direction = "📥 转入"
    else:
        direction = "🔔 相关交易"

    if tx.get("_type") == "token":
        symbol = html.escape(tx.get("tokenSymbol") or "TOKEN")
        decimals = int(tx.get("tokenDecimal") or 18)
        amount = f"{_amount(tx.get('value', '0'), decimals)} {symbol}"
    else:
        amount = f"{_amount(tx.get('value', '0'), 18)} {chain['native']}"
        if tx.get("isError") == "1":
            direction += " ⚠️失败"

    label = html.escape(watch.label or _short(target))
    lines = [
        f"{direction} <b>[{chain['name']}] {label}</b>",
        f"金额: <b>{amount}</b>",
        f"From: <a href=\"{explorer}/address/{sender}\">{_short(sender)}</a>",
        f"To: <a href=\"{explorer}/address/{receiver}\">{_short(receiver)}</a>",
        f"区块: {tx.get('blockNumber', '?')}",
        f"<a href=\"{explorer}/tx/{tx_hash}\">🔗 查看交易</a>",
    ]
    return "\n".join(lines)
