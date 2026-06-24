from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core.observability import TraceRecorder, detect_basic_drift, record_trace_events, verify_run
from core.report import build_stock_report
from myinveststock.db import connect, init_db


OBSERVABILITY_INPUT = {
    "stock_code": "600519.SH",
    "stock_name": "贵州茅台",
    "source_report_id": "leader_review_2026-06-24",
    "task_type": "financial",
    "research_date": "2026-06-24",
    "financial_rows": [
        {"revenue": 100.0, "net_profit": 10.0, "equity": 50.0, "gross_margin": 0.45, "debt": 20.0, "free_cash_flow": 8.0, "market_cap": 200.0},
        {"revenue": 120.0, "net_profit": 13.0, "equity": 60.0, "gross_margin": 0.47, "debt": 22.0, "free_cash_flow": 10.0, "market_cap": 220.0},
        {"revenue": 145.0, "net_profit": 17.0, "equity": 70.0, "gross_margin": 0.48, "debt": 25.0, "free_cash_flow": 13.0, "market_cap": 260.0},
        {"revenue": 175.0, "net_profit": 22.0, "equity": 84.0, "gross_margin": 0.49, "debt": 28.0, "free_cash_flow": 16.0, "market_cap": 300.0},
    ],
    "valuation_inputs": {
        "current_price": 90.0,
        "stock_pe": 20.0,
        "pe": 20.0,
        "pb": 2.0,
        "eps": 5.0,
        "book_value_per_share": 30.0,
        "industry_pb": 2.0,
        "fcf_per_share": 4.0,
    },
    "peers": [
        {"stock_code": "000858.SZ", "pe": 18.0, "roe": 0.16},
        {"stock_code": "000568.SZ", "pe": 22.0, "roe": 0.20},
        {"stock_code": "600809.SH", "pe": 28.0, "roe": 0.14},
    ],
}


class ObservabilityTests(unittest.TestCase):
    def test_trace_recorder_covers_full_report_pipeline_without_changing_output(self) -> None:
        baseline = build_stock_report(OBSERVABILITY_INPUT)
        recorder = TraceRecorder()
        traced = build_stock_report(OBSERVABILITY_INPUT, trace_recorder=recorder)

        self.assertEqual(baseline.model_dump(mode="json"), traced.model_dump(mode="json"))
        self.assertEqual([event.stage for event in recorder.events], ["feature", "valuation", "signal", "report"])
        for event in recorder.events:
            self.assertEqual(event.run_id, traced.run_id)
            self.assertEqual(len(event.input_hash), 64)
            self.assertEqual(len(event.output_hash), 64)
            self.assertTrue(event.diff_metrics)

    def test_audit_store_verify_run_and_drift_detection(self) -> None:
        recorder = TraceRecorder()
        report = build_stock_report(OBSERVABILITY_INPUT, trace_recorder=recorder)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "audit.sqlite"
            init_db(db_path)
            with closing(connect(db_path)) as conn:
                inserted = record_trace_events(conn, recorder.events)
                conn.commit()

                rows = conn.execute("SELECT stage, input_hash, output_hash FROM audit_log WHERE run_id = ?", (report.run_id,)).fetchall()
                verified = verify_run(conn, report.run_id or "", expected_report_hash=report.report_hash)
                drift = detect_basic_drift(conn, report.run_id or "")

        self.assertEqual(inserted, 4)
        self.assertEqual({row["stage"] for row in rows}, {"feature", "valuation", "signal", "report"})
        self.assertTrue(verified)
        self.assertEqual(drift.flags, [])
        self.assertIn("pe", drift.checked_metrics)


if __name__ == "__main__":
    unittest.main()
