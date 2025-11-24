"""
do_worker.py

Robust DO worker for processing DigitalOutputCommands from the DB and executing Modbus writes
against PAC3220 (or similar) devices.

Features / fixes applied:
- Clear, central configuration constants for easy adjustment (WRITE_REG, READ_REG, BITMASK).
- Proper parsing of `Notes` for `reg=` override.
- Correct logic for ON/OFF -> FC06 register values for PAC3220 (256 for ON, 0 for OFF).
- Idempotence: checks DB state and avoids unnecessary writes.
- Read-back verification (FC03 read of status register + bitmask).
- Retries with delay and clear error reporting.
- Safe DB updates and event recording via helper functions.
- Defensive exception handling and detailed debug logs.
- Async-friendly: uses provided ModbusClient async methods.
"""

from typing import Optional, List, Dict, Any
import asyncio
import os

from backend.dal.database import db_helper
from backend.utils.modbus_client import ModbusClient

# --- Configuration (adjustable) ---
# Default Modbus register to WRITE for PAC3220 DO (zero-based or device-specified as needed)
DEFAULT_WRITE_REGISTER = 60008  # PAC3220 spec for "Switch outputs" (Table in manual)
# Default Modbus register to READ status from (holding register view for DO status)
DEFAULT_READ_REGISTER = 207
# Bitmask within read register that contains Digital Output 0.0 state
DEFAULT_STATUS_BITMASK = 0x0001
# Modbus TCP port
MODBUS_PORT = 502
# Retry/backoff defaults
DEFAULT_MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 1
# Duplicate-suppression window when enqueuing (seconds)
DUPLICATE_WINDOW_SECONDS = 5

# --- DB / Modbus helpers ---


def _get_pending_commands(limit: int = 20) -> List[Dict[str, Any]]:
    q = (
        """
        SELECT TOP (?) c.CommandID, c.AnalyzerID, c.CoilAddress, c.Command, c.Notes,
               c.RequestedBy, c.MaxRetries, ISNULL(c.RetryCount, 0) as RetryCount,
               a.IPAddress, a.ModbusID
        FROM app.DigitalOutputCommands c
        JOIN app.Analyzers a ON c.AnalyzerID = a.AnalyzerID
        WHERE c.ExecutionResult = 'PENDING'
        ORDER BY c.RequestedAt ASC
        """
    )
    return db_helper.execute_query(q, (limit,)) or []


def _update_result(command_id: int, result: str, error_msg: Optional[str] = None) -> None:
    params = {"@CommandID": command_id, "@ExecutionResult": result, "@ErrorMessage": error_msg}
    try:
        db_helper.execute_stored_procedure("app.sp_UpdateDigitalOutputResult", params)
    except Exception:
        # best-effort; do not crash worker loop
        print(f"[WARN] Failed to call sp_UpdateDigitalOutputResult for {command_id}")


def _parse_notes_for_reg(notes: Optional[str]) -> Optional[int]:
    if not notes:
        return None
    try:
        for part in str(notes).split(";"):
            p = part.strip()
            if not p:
                continue
            if p.lower().startswith("reg="):
                raw = p.split("=", 1)[1].strip()
                return int(raw)
    except Exception:
        return None
    return None


def _record_do_event(
    analyzer_id: int,
    coil_address: int,
    old_state: Optional[int],
    new_state: Optional[int],
    control_type: str,
    success: bool,
    source_note: Optional[str] = None,
):
    """
    Update DigitalOutputStatus and insert an event into ops.Events.
    This function is resilient: it swallows exceptions but prints a warn.
    """
    try:
        src = "system"
        src_detail = source_note or ""
        if source_note:
            for part in str(source_note).split(";"):
                if part.strip().startswith("source="):
                    src = part.split("=", 1)[1].strip()
                    break

        # Upsert/update status row (attempt update first)
        db_helper.execute_query(
            "UPDATE app.DigitalOutputStatus SET State = ?, LastUpdated = GETUTCDATE(), UpdateSource = ? WHERE AnalyzerID = ? AND CoilAddress = ?",
            (int(new_state) if new_state is not None else None, src, analyzer_id, coil_address),
        )
        # Insert event log
        meta = f'{{"old_state": {old_state if old_state is not None else "null"}, "new_state": {new_state if new_state is not None else "null"}, "type": "{control_type}", "notes": "{src_detail}"}}'
        db_helper.execute_query(
            """
            INSERT INTO ops.Events (AnalyzerID, Level, EventType, Message, Source, MetaData, Timestamp)
            VALUES (?, ?, ?, ?, ?, ?, GETUTCDATE())
            """,
            (analyzer_id, "INFO" if success else "ERROR", "do_control" if success else "do_control_failed",
             f"DO {'ON' if new_state == 1 else 'OFF' if new_state == 0 else 'UNKNOWN'}", src, meta),
        )
    except Exception as e:
        print(f"[WARN] _record_do_event failed: {e}")


# --- Execution logic ---


async def _execute_command(cmd: Dict[str, Any]) -> None:
    """
    Execute a single pending DO command.
    - Determines write/read register addresses (with override support).
    - Performs idempotence check against app.DigitalOutputStatus.
    - Writes using FC06 (single register) and verifies via read (FC03 or FC04 depending on client).
    """
    command_id = int(cmd["CommandID"])
    analyzer_id = int(cmd.get("AnalyzerID") or cmd.get("AnalyzerId") or 0)
    host = cmd.get("IPAddress")
    unit_id = int(cmd.get("ModbusID") or 1)
    coil_address = int(cmd.get("CoilAddress") or 0)
    notes = str(cmd.get("Notes") or "")
    reg_override = _parse_notes_for_reg(notes)
    write_register_address = int(reg_override if reg_override is not None else DEFAULT_WRITE_REGISTER)
    read_register_address = DEFAULT_READ_REGISTER
    status_bitmask = DEFAULT_STATUS_BITMASK
    command = str(cmd.get("Command") or "").upper()
    max_retries = int(cmd.get("MaxRetries") or DEFAULT_MAX_RETRIES)

    if not host:
        _update_result(command_id, "FAILED", "missing_analyzer_ip")
        print(f"[ERROR] Command {command_id} missing analyzer IP")
        return

    # Prepare Modbus client
    client = ModbusClient(host=host, port=MODBUS_PORT, unit_id=unit_id)
    ok = await client.connect()
    if not ok:
        _update_result(command_id, "FAILED", f"connect_failed:{host}")
        print(f"[ERROR] Command {command_id} failed to connect to {host}:{MODBUS_PORT}")
        return

    # Determine target boolean and register value encoding (PAC3220 specifics)
    target_state_bool: Optional[bool] = None
    if command == "ON":
        target_state_bool = True
    elif command == "OFF":
        target_state_bool = False
    elif command == "TOGGLE":
        # Toggle: derive from DB if present, else default to True
        try:
            cur = db_helper.execute_query(
                "SELECT State FROM app.DigitalOutputStatus WHERE AnalyzerID = ? AND CoilAddress = ?",
                (analyzer_id, coil_address),
            ) or []
            if cur:
                cur_state = int(cur[0].get("State") or 0)
                target_state_bool = not bool(cur_state)
            else:
                target_state_bool = True
        except Exception:
            target_state_bool = True
    else:
        # Unknown command: fail fast
        _update_result(command_id, "FAILED", f"unknown_command:{command}")
        await client.disconnect()
        print(f"[ERROR] Command {command_id} has unknown command '{command}'")
        return

    # PAC3220 encoding: Byte0 = Output ID (0), Byte1 = Action (1 => ON, 0 => OFF)
    # So combined 16-bit value: Byte1 << 8 | Byte0. For ON (1,0) -> 256. For OFF -> 0.
    target_value = 256 if target_state_bool else 0

    # Idempotence: check DB state and skip if already in target
    try:
        current_db = db_helper.execute_query(
            "SELECT State FROM app.DigitalOutputStatus WHERE AnalyzerID = ? AND CoilAddress = ?",
            (analyzer_id, coil_address),
        ) or []
        if current_db:
            cs = int(current_db[0].get("State") or 0)
            desired_int = 1 if target_state_bool else 0
            if cs == desired_int:
                # Already at desired state — mark success, record event, disconnect
                _update_result(command_id, "SUCCESS", None)
                _record_do_event(analyzer_id, coil_address, cs, desired_int, "manual" if command in ("ON", "OFF", "TOGGLE") else "auto", True, source_note=notes)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                print(f"[INFO] Command {command_id} skipped: already in desired state {desired_int}")
                return
    except Exception as e:
        print(f"[WARN] Idempotence DB check failed for command {command_id}: {e}")

    # Attempt write with retries
    success = False
    last_error: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        try:
            print(f"[DO] Command {command_id} attempt {attempt}: FC06 write reg={write_register_address} val={target_value}")
            ok_write = await client.write_register(write_register_address, int(target_value))
            if ok_write:
                success = True
                break
            else:
                last_error = f"attempt_failed:{attempt}"
                print(f"[WARN] Command {command_id} write returned falsy (attempt {attempt})")
        except Exception as e:
            last_error = f"attempt_error:{attempt}:{str(e)}"
            print(f"[ERROR] Command {command_id} write exception (attempt {attempt}): {e}")
        if attempt < max_retries:
            await asyncio.sleep(RETRY_DELAY_SECONDS)

    # After attempts, always attempt to read back (if we connected and write succeeded) to verify state
    read_back_value = None
    read_back_state: Optional[int] = None
    if success:
        try:
            print(f"[DO] Command {command_id}: FC03 read-back reg={read_register_address}")
            read_back_value = await client.read_register_value(read_register_address)
            if read_back_value is not None:
                # parse bitmask
                try:
                    read_back_state = 1 if (int(read_back_value) & int(status_bitmask)) != 0 else 0
                except Exception:
                    read_back_state = None
            print(f"[DO] Command {command_id}: read_back raw={read_back_value} parsed_state={read_back_state}")
        except Exception as e:
            print(f"[WARN] Command {command_id} read-back failed: {e}")
            read_back_value = None
            read_back_state = None

    # Clean disconnect
    try:
        await client.disconnect()
    except Exception:
        pass

    # Finalize result based on verification
    desired_int = 1 if target_state_bool else 0
    if success and read_back_state is not None and read_back_state == desired_int:
        _update_result(command_id, "SUCCESS", None)
        _record_do_event(analyzer_id, coil_address, None, read_back_state, "manual" if command in ("ON", "OFF", "TOGGLE") else "auto", True, source_note=(notes or "") + f";write_reg={write_register_address};read_reg={read_register_address};read_back={read_back_value}")
        print(f"[INFO] Command {command_id} SUCCESS. read_back={read_back_value}")
    elif success and read_back_state is not None and read_back_state != desired_int:
        _update_result(command_id, "FAILED", "readback_mismatch")
        _record_do_event(analyzer_id, coil_address, None, desired_int, "manual" if command in ("ON", "OFF", "TOGGLE") else "auto", False, source_note=(notes or "") + f";write_reg={write_register_address};read_reg={read_register_address};read_back={read_back_value}")
        print(f"[ERROR] Command {command_id} FAILED: readback_mismatch (got={read_back_state} expected={desired_int})")
    elif success and read_back_state is None:
        # Write succeeded on Modbus client but read-back not available -> mark failed (safety)
        _update_result(command_id, "FAILED", "readback_missing")
        _record_do_event(analyzer_id, coil_address, None, desired_int, "manual" if command in ("ON", "OFF", "TOGGLE") else "auto", False, source_note=(notes or "") + f";write_reg={write_register_address};read_reg={read_register_address};read_back=null")
        print(f"[ERROR] Command {command_id} FAILED: readback_missing")
    else:
        # Write did not succeed
        _update_result(command_id, "FAILED", last_error or "unknown_error")
        _record_do_event(analyzer_id, coil_address, None, desired_int, "manual" if command in ("ON", "OFF", "TOGGLE") else "auto", False, source_note=(notes or "") + f";write_reg={write_register_address};error={last_error or 'unknown'}")
        print(f"[ERROR] Command {command_id} FAILED: {last_error or 'unknown'}")


async def process_pending_commands(batch_size: int = 20) -> int:
    cmds = _get_pending_commands(batch_size)
    if not cmds:
        return 0
    processed = 0
    for cmd in cmds:
        try:
            await _execute_command(cmd)
            processed += 1
        except Exception as e:
            print(f"[ERROR] Unexpected error while processing command {cmd.get('CommandID')}: {e}")
            try:
                _update_result(int(cmd.get("CommandID")), "FAILED", f"unexpected:{str(e)}")
            except Exception:
                pass
    return processed


def _should_enqueue(analyzer_id: int, coil_address: int, command: str, source: str) -> bool:
    try:
        q = (
            """
            SELECT TOP 1 CommandID
            FROM app.DigitalOutputCommands
            WHERE AnalyzerID = ? AND CoilAddress = ? AND Command = ? AND ExecutionResult = 'PENDING'
                  AND RequestedAt >= DATEADD(SECOND, -? , GETUTCDATE())
            ORDER BY RequestedAt DESC
            """
        )
        rows = db_helper.execute_query(q, (analyzer_id, coil_address, command, DUPLICATE_WINDOW_SECONDS))
        return not rows
    except Exception:
        # If DB check fails, allow enqueue (best-effort)
        return True


def _enqueue_do(analyzer_id: int, coil_address: int, command: str, source: str, reason: Optional[str], requested_by: Optional[int] = None) -> bool:
    try:
        if not _should_enqueue(analyzer_id, coil_address, command, source):
            return False
        params = {
            "@AnalyzerID": analyzer_id,
            "@CoilAddress": coil_address,
            "@Command": command,
            "@RequestedBy": requested_by or 0,
            "@MaxRetries": DEFAULT_MAX_RETRIES,
            "@Notes": f"source={source};reason={reason or ''}"
        }
        db_helper.execute_stored_procedure("app.sp_ControlDigitalOutput", params)
        return True
    except Exception as e:
        print(f"[WARN] _enqueue_do failed: {e}")
        return False


def _enforce_auto_limit_restore():
    """
    Enqueue ON when used >= allocated (100+%), enqueue OFF when under threshold (after recharge).
    Only applies to analyzers where BreakerEnabled=1. This function is best-effort and errors are swallowed.
    """
    try:
        users = db_helper.execute_query("SELECT UserID, AllocatedKWh, UsedKWh FROM app.Users WHERE ISNULL(IsActive,1)=1") or []
        for u in users:
            try:
                alloc = float(u.get("AllocatedKWh") or 0.0)
                used = float(u.get("UsedKWh") or 0.0)
                pct = (used / alloc * 100.0) if alloc > 0 else (100.0 if used > 0 else 0.0)
                aids = db_helper.execute_query(
                    "SELECT AnalyzerID, ISNULL(BreakerCoilAddress, 0) as Coil, ISNULL(BreakerEnabled, 0) as Enabled FROM app.Analyzers WHERE UserID = ? AND IsActive = 1",
                    (u["UserID"],),
                ) or []
                for a in aids:
                    if int(a.get("Enabled") or 0) != 1:
                        continue
                    coil = int(a.get("Coil") or 0)
                    # Keep DB-range check but allow worker to override write register via default/reg override
                    if coil < 0 or coil > 9999:
                        continue
                    if pct >= 100.0:
                        _enqueue_do(int(a["AnalyzerID"]), coil, "ON", "auto_limit", "Units exceeded 100%", requested_by=1)
                    else:
                        _enqueue_do(int(a["AnalyzerID"]), coil, "OFF", "auto_restore", "Recharge completed", requested_by=1)
            except Exception:
                # per-user failure should not break the loop
                continue
    except Exception:
        pass


async def run_worker_loop(poll_interval_seconds: int = 5):
    dry = os.getenv("DRY_RUN", "false").lower() == "true"
    try:
        connected = db_helper.test_connection()
    except Exception:
        connected = False
    print(f"[WORKER] Connected to DB: {'YES' if connected else 'NO'}")
    print(f"[WORKER] DRY_RUN: {'ENABLED' if dry else 'DISABLED'}")
    print("[WORKER] Waiting for commands...")
    if dry:
        while True:
            await asyncio.sleep(poll_interval_seconds)
    while True:
        try:
            _enforce_auto_limit_restore()
        except Exception:
            pass
        try:
            count = await process_pending_commands(20)
        except Exception:
            count = 0
        await asyncio.sleep(poll_interval_seconds if count == 0 else 1)


if __name__ == "__main__":
    try:
        interval = int(os.getenv("WORKER_POLL_INTERVAL", "5"))
    except Exception:
        interval = 5
    try:
        asyncio.run(run_worker_loop(interval))
    except KeyboardInterrupt:
        pass
