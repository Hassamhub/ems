import os
import sys
import asyncio

# ensure project root on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.do_worker import _execute_command

async def main():
    cmd = {
        "CommandID": 99999,
        "AnalyzerID": 3,
        "CoilAddress": 0,
        "Command": "ON",
        "RequestedBy": 1,
        "MaxRetries": 3,
        "RetryCount": 0,
        "IPAddress": "192.168.10.2",
        "ModbusID": 1,
        "Notes": "source=test;reg=60008",
    }
    await _execute_command(cmd)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
