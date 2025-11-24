import os
import sys

# ensure project root on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.dal.database import db_helper

def main():
    params = {
        "@AnalyzerID": 3,
        "@CoilAddress": 0,
        "@Command": "ON",
        "@RequestedBy": 1,
        "@MaxRetries": 3,
        "@Notes": "source=test;reg=60008",
    }
    print("Enqueue start")
    try:
        db_helper.execute_stored_procedure("app.sp_ControlDigitalOutput", params)
        print("Enqueue done")
        rows = db_helper.execute_query(
            "SELECT TOP 5 CommandID, AnalyzerID, CoilAddress, Command, ExecutionResult FROM app.DigitalOutputCommands ORDER BY RequestedAt DESC",
            ()
        ) or []
        print("Recent commands:")
        for r in rows:
            print(r)
    except Exception as e:
        print(f"Enqueue failed: {e}")

if __name__ == "__main__":
    main()
