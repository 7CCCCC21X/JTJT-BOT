"""JSON file persistence for watch list.

On Railway attach a Volume and point DATA_DIR at its mount path so the
watch list survives redeploys; otherwise it lives on the ephemeral disk.
"""

import json
import os
import threading

DATA_DIR = os.environ.get("DATA_DIR", "data")
DATA_FILE = os.path.join(DATA_DIR, "watches.json")

_lock = threading.Lock()


class Watch:
    """One monitored target for one chat."""

    def __init__(self, chat_id: int, chain: str, address: str, kind: str,
                 label: str = "", last_block: int = 0, seen: list[str] | None = None):
        self.chat_id = chat_id
        self.chain = chain
        self.address = address.lower()
        self.kind = kind  # "address" (txlist + tokentx) | "token" (all transfers of a token contract)
        self.label = label
        self.last_block = last_block
        self.seen = seen or []  # recent tx keys for dedupe

    @property
    def key(self) -> tuple:
        return (self.chat_id, self.chain, self.address, self.kind)

    def remember(self, tx_key: str):
        self.seen.append(tx_key)
        if len(self.seen) > 300:
            self.seen = self.seen[-300:]

    def to_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "chain": self.chain,
            "address": self.address,
            "kind": self.kind,
            "label": self.label,
            "last_block": self.last_block,
            "seen": self.seen[-300:],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Watch":
        return cls(d["chat_id"], d["chain"], d["address"], d["kind"],
                   d.get("label", ""), d.get("last_block", 0), d.get("seen"))


class Store:
    def __init__(self):
        self.watches: dict[tuple, Watch] = {}
        self.load()

    def load(self):
        with _lock:
            if not os.path.exists(DATA_FILE):
                return
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for d in raw.get("watches", []):
                    w = Watch.from_dict(d)
                    self.watches[w.key] = w
            except (json.JSONDecodeError, KeyError, OSError):
                pass

    def save(self):
        with _lock:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"watches": [w.to_dict() for w in self.watches.values()]},
                          f, ensure_ascii=False)
            os.replace(tmp, DATA_FILE)

    def add(self, watch: Watch) -> bool:
        if watch.key in self.watches:
            return False
        self.watches[watch.key] = watch
        self.save()
        return True

    def remove(self, chat_id: int, chain: str, address: str) -> int:
        address = address.lower()
        keys = [k for k, w in self.watches.items()
                if w.chat_id == chat_id and w.chain == chain and w.address == address]
        for k in keys:
            del self.watches[k]
        if keys:
            self.save()
        return len(keys)

    def for_chat(self, chat_id: int) -> list[Watch]:
        return [w for w in self.watches.values() if w.chat_id == chat_id]
