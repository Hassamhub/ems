import os
import sys

# ensure project root on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.utils.modbus_client import ModbusClient

async def main():
    client = ModbusClient(host="192.168.10.2", port=502, unit_id=1)
    ok = await client.connect()
    if not ok:
        print("readback_connect_failed")
        return
    try:
        val = await client.read_register_value(207)
        print(f"readback_207={val}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
