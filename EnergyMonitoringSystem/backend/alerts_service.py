import os
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List

from backend.dal.database import db_helper
from backend.utils.email_client import send_email

CHECK_INTERVAL_SECONDS = int(os.getenv("ALERTS_CHECK_INTERVAL", "60"))

async def _check_low_balance_and_notify() -> None:
    try:
        # Threshold can be absolute kWh or %; here treat as absolute kWh
        thr_env = os.getenv("LOW_BALANCE_THRESHOLD_KWH", "5")
        try:
            threshold = float(thr_env)
        except Exception:
            threshold = 5.0

        # Find users below threshold (RemainingKWh assumed computed or stored)
        rows = db_helper.execute_query(
            """
            SELECT UserID, Username, Email, RemainingKWh
            FROM app.Users
            WHERE ISNULL(RemainingKWh, 0) < ? AND ISNULL(IsActive,1) = 1
            """,
            (threshold,)
        ) or []

        for r in rows:
            email = r.get("Email")
            if not email:
                continue
            subject = "Low Balance Alert"
            body = f"Dear {r.get('Username')}, your RemainingKWh is {r.get('RemainingKWh')} kWh, which is below {threshold} kWh."
            send_email(subject, body, [email], html=False)
    except Exception:
        # Avoid crashing scheduler
        pass

async def _check_offline_devices_and_notify() -> None:
    try:
        # Determine poll interval to gauge offline threshold
        poll = 60
        try:
            cfg = db_helper.execute_query("SELECT ConfigValue FROM ops.Configuration WHERE ConfigKey = 'system.poller_interval'")
            if cfg and cfg[0].get("ConfigValue"):
                poll = int(cfg[0]["ConfigValue"])
        except Exception:
            pass
        # Offline if LastSeen older than 3x poll interval
        minutes = max(1, (poll * 3) // 60 if poll >= 60 else 1)
        rows = db_helper.execute_query(
            f"""
            SELECT a.AnalyzerID, a.SerialNumber, a.LastSeen, a.ConnectionStatus,
                   u.Email, u.Username
            FROM app.Analyzers a
            LEFT JOIN app.Users u ON a.UserID = u.UserID
            WHERE a.IsActive = 1
              AND (a.LastSeen IS NULL OR a.LastSeen < DATEADD(MINUTE, -{minutes}, GETUTCDATE()))
            """
        ) or []
        for r in rows:
            email = r.get("Email")
            if not email:
                continue
            subject = "Device Offline Alert"
            body = (
                f"Analyzer {r.get('SerialNumber') or r.get('AnalyzerID')} appears OFFLINE. "
                f"LastSeen: {r.get('LastSeen')}"
            )
            send_email(subject, body, [email], html=False)
    except Exception:
        pass

async def _check_usage_threshold_and_notify() -> None:
    try:
        rows = db_helper.execute_query(
            """
            SELECT UserID, Username, FullName, Email, AllocatedKWh, UsedKWh,
                   CASE WHEN COL_LENGTH('app.Users','Sent80PercentWarning') IS NULL THEN NULL ELSE Sent80PercentWarning END AS Sent80PercentWarning,
                   CASE WHEN COL_LENGTH('app.Users','DoAutoOnTriggered') IS NULL THEN NULL ELSE DoAutoOnTriggered END AS DoAutoOnTriggered
            FROM app.Users
            WHERE ISNULL(IsActive,1) = 1 AND ISNULL(AllocatedKWh,0) > 0
            """
        ) or []
        for r in rows:
            uid = int(r.get("UserID"))
            alloc = float(r.get("AllocatedKWh") or 0.0)
            used = float(r.get("UsedKWh") or 0.0)
            if alloc <= 0:
                continue
            pct = (used / alloc) if alloc > 0 else 0.0
            reset_row = db_helper.execute_query(
                "SELECT TOP 1 Timestamp FROM ops.Events WHERE EventType = 'usage_flags_reset' AND UserID = ? ORDER BY Timestamp DESC",
                (uid,)
            ) or []
            reset_ts = reset_row[0].get("Timestamp") if reset_row else None
            if pct >= 0.8 and pct < 1.0:
                already_flagged = (r.get("Sent80PercentWarning") == 1)
                sent_row = db_helper.execute_query(
                    "SELECT TOP 1 EventID FROM ops.Events WHERE EventType = 'usage_80_sent' AND UserID = ? AND (? IS NULL OR Timestamp > ?) ORDER BY Timestamp DESC",
                    (uid, reset_ts, reset_ts)
                ) or []
                if not already_flagged and not sent_row:
                    email = r.get("Email")
                    if email:
                        percent = round(pct * 100.0)
                        subject = "Usage Notice — 80% of your energy allocation used"
                        body = (
                            f"Dear {r.get('FullName') or r.get('Username')},\n\n"
                            f"This is an automated notice from the Energy Monitoring System.\n\n"
                            f"You have used {used:.2f} kWh out of your allocated {alloc:.2f} kWh ({percent}%). This is a friendly reminder that you are approaching your allocation limit.\n\n"
                            f"Recommended next steps:\n"
                            f"• Review your usage in your dashboard.\n"
                            f"• Consider recharging your allocation to avoid service interruption.\n\n"
                            f"If you need help, please contact support@example.com.\n\n"
                            f"Warm regards,\nEnergy Monitoring System\n"
                        )
                        ok = send_email(subject, body, [email], html=False)
                        try:
                            db_helper.execute_query(
                                "INSERT INTO ops.Events (UserID, Level, EventType, Message, Source, MetaData, Timestamp) VALUES (?, ?, 'usage_80_sent', ?, 'alerts', ?, GETUTCDATE())",
                                (
                                    uid,
                                    'INFO' if ok else 'ERROR',
                                    '80% usage warning sent',
                                    f'{'{'}"user_id": {uid}, "email_sent": {str(ok).lower()}{'}'}'
                                )
                            )
                        except Exception:
                            pass
                        try:
                            # Set flag if column exists or via stored proc
                            db_helper.execute_query(
                                "IF COL_LENGTH('app.Users','Sent80PercentWarning') IS NOT NULL UPDATE app.Users SET Sent80PercentWarning = 1 WHERE UserID = ?",
                                (uid,)
                            )
                        except Exception:
                            try:
                                db_helper.execute_stored_procedure(
                                    "sp_SetUserAlertFlags",
                                    {"@UserID": uid, "@Sent80": 1, "@AutoOn": r.get("DoAutoOnTriggered") or 0}
                                )
                            except Exception:
                                pass
            if pct >= 1.0:
                already_auto = (r.get("DoAutoOnTriggered") == 1)
                trig_row = db_helper.execute_query(
                    "SELECT TOP 1 EventID FROM ops.Events WHERE EventType = 'auto_on_exhausted' AND UserID = ? AND (? IS NULL OR Timestamp > ?) ORDER BY Timestamp DESC",
                    (uid, reset_ts, reset_ts)
                ) or []
                if not already_auto and not trig_row:
                    an_rows = db_helper.execute_query(
                        "SELECT AnalyzerID, ISNULL(BreakerCoilAddress, 0) as Coil FROM app.Analyzers WHERE UserID = ? AND IsActive = 1",
                        (uid,)
                    ) or []
                    for a in an_rows:
                        coil = int(a.get("Coil") or 0)
                        try:
                            db_helper.execute_stored_procedure(
                                "app.sp_ControlDigitalOutput",
                                {
                                    "@AnalyzerID": int(a.get("AnalyzerID")),
                                    "@CoilAddress": coil,
                                    "@Command": "ON",
                                    "@RequestedBy": 1,
                                    "@MaxRetries": 3,
                                    "@Notes": "source=auto_exhausted"
                                }
                            )
                        except Exception:
                            pass
                    email = r.get("Email")
                    ok2 = False
                    if email:
                        percent2 = 100
                        subject2 = "Important: Your energy allocation is exhausted — action required"
                        body2 = (
                            f"Dear {r.get('FullName') or r.get('Username')},\n\n"
                            f"This automated message is to inform you that you have consumed 100% of your allocated energy ({used:.2f} kWh of {alloc:.2f} kWh).\n\n"
                            f"To continue uninterrupted service, please recharge your allocation as soon as possible.\n\n"
                            f"If this seems incorrect, please contact support@example.com.\n\n"
                            f"Regards,\nEnergy Monitoring System\n"
                        )
                        ok2 = send_email(subject2, body2, [email], html=False)
                    try:
                        db_helper.execute_query(
                            "INSERT INTO ops.Events (UserID, Level, EventType, Message, Source, MetaData, Timestamp) VALUES (?, ?, 'auto_on_exhausted', ?, 'alerts', ?, GETUTCDATE())",
                            (
                                uid,
                                'INFO' if ok2 else 'ERROR',
                                '100% allocation exhausted',
                                f'{'{'}"user_id": {uid}, "email_sent": {str(ok2).lower()}{'}'}'
                            )
                        )
                    except Exception:
                        pass
                    try:
                        db_helper.execute_query(
                            "IF COL_LENGTH('app.Users','DoAutoOnTriggered') IS NOT NULL UPDATE app.Users SET DoAutoOnTriggered = 1 WHERE UserID = ?",
                            (uid,)
                        )
                    except Exception:
                        try:
                            db_helper.execute_stored_procedure(
                                "sp_SetUserAlertFlags",
                                {"@UserID": uid, "@Sent80": r.get("Sent80PercentWarning") or 0, "@AutoOn": 1}
                            )
                        except Exception:
                            pass
    except Exception:
        pass

async def start_alerts_scheduler():
    # Run forever on app startup if enabled
    async def loop():
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            await _check_usage_threshold_and_notify()
            await _check_low_balance_and_notify()
            await _check_offline_devices_and_notify()
    # Start background loop
    asyncio.create_task(loop())
