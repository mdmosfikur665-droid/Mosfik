import asyncio
import multiprocessing as mp
from multiprocessing.connection import Connection
import random
import string
import time
import msgspec
import websockets
from typing import List

# ডেটা মডেল
class TickData(msgspec.Struct):
    s: str   # Symbol
    p: float # Price
    v: float # Volume
    ts: int  # Timestamp (ns)

# নেটওয়ার্ক ব্রিজ (WebSocket কানেকশন)
class TVNetworkBridge(mp.Process):
    def __init__(self, symbols: List[str], pipe_conn: Connection):
        super().__init__(name="IngressBridge", daemon=True)
        self.symbols = symbols
        self.pipe = pipe_conn
        self.url = "wss://data.tradingview.com/socket.io/websocket"

    def _pack(self, m: str, p: list) -> str:
        payload = msgspec.json.encode({"m": m, "p": p}).decode('utf-8')
        return f"~m~{len(payload)}~m~{payload}"

    async def _socket_handler(self):
        headers = {"Origin": "https://www.tradingview.com"}
        session_id = "qs_" + "".join(random.choices(string.ascii_lowercase, k=10))

        async for ws in websockets.connect(self.url, additional_headers=headers, ping_interval=20):
            try:
                # সেশন সেটআপ
                await ws.send(self._pack("set_auth_token", ["unauthorized_user_token"]))
                await ws.send(self._pack("quote_create_session", [session_id]))
                await ws.send(self._pack("quote_set_fields", [session_id, "lp", "v", "ch", "chp"]))
                await ws.send(self._pack("quote_add_symbols", [session_id] + self.symbols))
                
                async for message in ws:
                    t_ingress = time.perf_counter_ns()
                    if "~h~" in message:
                        await ws.send(message)
                        continue
                    
                    # ডেটা পাইপে পাঠিয়ে দেওয়া
                    self.pipe.send((message, t_ingress))
            except Exception:
                await asyncio.sleep(2)

    def run(self):
        asyncio.run(self._socket_handler())

# মেইন ইঞ্জিন
class TVOverdriveEngine:
    def __init__(self, symbols: List[str]):
        self.symbols = symbols
        self.parent_conn, self.child_conn = mp.Pipe(duplex=False)
        self.bridge = TVNetworkBridge(symbols, self.child_conn)
        self._price_cache = {}
        self._running = False

    async def _worker(self, queue: asyncio.Queue, callback):
        while self._running:
            item = await queue.get()
            try:
                await callback(item)
            except:
                pass
            finally:
                queue.task_done()

    async def start(self, callback):
        self._running = True
        self.bridge.start()
        
        # ওয়ার্কার সেটআপ
        processing_queue = asyncio.Queue(maxsize=1000)
        [asyncio.create_task(self._worker(processing_queue, callback)) for _ in range(4)]
        
        loop = asyncio.get_event_loop()
        while self._running:
            if self.parent_conn.poll():
                raw_msg, t_ingress = await loop.run_in_executor(None, self.parent_conn.recv)
                try:
                    payloads = raw_msg.split("~m~")[2::2]
                    for p_str in payloads:
                        data = msgspec.json.decode(p_str)
                        if data.get("m") == "qsd":
                            p = data["p"][1]
                            v = p.get("v", {})
                            symbol = p["n"]
                            price = v.get("lp")

                            if price is not None and self._price_cache.get(symbol) != price:
                                self._price_cache[symbol] = price
                                tick = TickData(s=symbol, p=price, v=v.get("v", 0.0), ts=t_ingress)
                                if not processing_queue.full():
                                    processing_queue.put_nowait(tick)
                except:
                    continue
            else:
                await asyncio.sleep(0.0001)
