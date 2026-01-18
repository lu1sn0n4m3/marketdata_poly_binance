# Quick script to log a book event
import asyncio
import websockets
import orjson

async def sample_book():
    async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/event/bitcoin-up-or-down-january-18-10am-et") as ws:
        # Subscribe (use a current token_id)
        await ws.send(orjson.dumps({"type": "market", "assets_ids": ["YOUR_TOKEN_ID"]}))
        
        async for msg in ws:
            data = orjson.loads(msg)
            if isinstance(data, dict) and data.get("event_type") == "book":
                print(orjson.dumps(data, option=orjson.OPT_INDENT_2).decode())
                break

asyncio.run(sample_book())