from typing import Optional
from pymodbus.client import ModbusTcpClient

REG_DO_COMMAND = 60008
REG_DO_STATUS_BIT = 400
REG_DO_TYPE = 50035

def encode_do_value(output_id: int, action: int) -> int:
    return ((int(output_id) & 0xFF) << 8) | (int(action) & 0xFF)

def read_do_type(host: str, port: int = 502, unit_id: int = 1, reg_do_type: int = REG_DO_TYPE) -> Optional[int]:
    client = ModbusTcpClient(host, port=port)
    if not client.connect():
        return None
    try:
        resp = client.read_holding_registers(reg_do_type, 2, slave=unit_id)
    finally:
        client.close()
    if not resp or resp.isError() or not getattr(resp, "registers", None):
        return None
    hi = int(resp.registers[0])
    lo = int(resp.registers[1])
    return (hi << 16) | lo

def write_do(host: str, output_id: int, action: int, port: int = 502, unit_id: int = 1, reg_do_command: int = REG_DO_COMMAND, check_type: bool = False, reg_do_type: int = REG_DO_TYPE) -> bool:
    if check_type:
        t = read_do_type(host, port, unit_id, reg_do_type)
        if t != 2:
            return False
    value = encode_do_value(output_id, action)
    client = ModbusTcpClient(host, port=port)
    if not client.connect():
        return False
    try:
        resp = client.write_register(reg_do_command, value, slave=unit_id)
    finally:
        client.close()
    return bool(resp and not resp.isError())

def write_do_0(host: str, action: int, port: int = 502, unit_id: int = 1, reg_do_command: int = REG_DO_COMMAND, check_type: bool = False, reg_do_type: int = REG_DO_TYPE, output_id: int = 0) -> bool:
    return write_do(host, output_id, action, port, unit_id, reg_do_command, check_type, reg_do_type)

def read_do_0(host: str, port: int = 502, unit_id: int = 1, reg_do_status_bit: int = REG_DO_STATUS_BIT) -> Optional[int]:
    client = ModbusTcpClient(host, port=port)
    if not client.connect():
        return None
    try:
        resp = client.read_discrete_inputs(reg_do_status_bit, 1, slave=unit_id)
    finally:
        client.close()
    if not resp or resp.isError() or not getattr(resp, "bits", None):
        return None
    return 1 if bool(resp.bits[0]) else 0
