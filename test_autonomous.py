import unittest
import importlib.util
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch
import csv
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

import test_detector
import test_restarts
import test_scenarios


class AutonomousScenarios(unittest.TestCase):
    def test_physical_shifts_have_machine_decisions(self):
        for speed in [18, 26, 36]:
            for size in [2, 3, 7]:
                for changing in [False, True]:
                    for defect in [None, "rejected", "failed"]:
                        with self.subTest(speed=speed, size=size, changing=changing, defect=defect):
                            plc, station, boxes, laser, expected = test_scenarios.synthetic_shift(speed, size, changing, defect)
                            rows, report, exit_code = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser, strict=True)
                            self.assertEqual([(r["вид"], r["туба"], r["код"], r["короб"]) for r in rows], expected)
                            self.assertEqual(exit_code, 10 if expected else 0, report["issues"])
                            self.assertEqual(report["status"], "ANOMALIES" if expected else "CLEAR")
                            if expected:
                                velocity = speed / 60
                                seconds = 0.9 / velocity + 0.48 / (velocity * 1.25) if changing else 1.38 / velocity
                                expected_time = datetime(2026, 1, 2, 21) + timedelta(seconds=seconds)
                                actual_time = datetime.strptime(rows[0]["время"], "%Y-%m-%d %H:%M:%S.%f")
                                self.assertLessEqual(abs((actual_time - expected_time).total_seconds()), 0.002)

    def test_missing_and_multiple_confirmations_cannot_return_clear(self):
        for mode in ["lost_ack", "multiple", "starvation", "missing_counter", "reset"]:
            with self.subTest(mode=mode):
                rows, report, exit_code = test_detector.EvidenceTests().run_shift(*test_restarts.restart_shift(mode), strict=True)
                self.assertIn(exit_code, (10, 20))
                self.assertNotEqual(report["status"], "CLEAR")
                if mode in {"lost_ack", "multiple"}:
                    self.assertTrue(any(row["вид"] == "В" and row["код"] == "RETRY" for row in rows), report)

    def test_unknown_print_is_not_reported_as_proven_extra_code(self):
        plc, station, boxes, laser, _ = test_scenarios.synthetic_shift(18, 3, False, "failed")
        lines = laser.splitlines()
        index = next(i for i, line in enumerate(lines) if "SendContent CODE2" in line)
        laser = "\n".join(lines[:index] + lines[index + 2:]) + "\n"
        rows, report, exit_code = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser, strict=True)
        self.assertFalse(any(row["вид"] == "Б" and row["туба"] == "2" for row in rows))
        self.assertEqual(exit_code, 20)
        self.assertTrue(any(i["code"] == "UNKNOWN_PRINT_IDENTITY" and i["entity"] == "2" for i in report["issues"]))

    def test_counter_reply_window_does_not_invent_lost_print(self):
        plc, station, boxes, laser, expected = test_scenarios.synthetic_shift(18, 3, False, None)
        laser = "\n".join(sorted(laser.splitlines() + [
            "2026-01-02 21:00:00.000 → GetMarkCount",
            "2026-01-02 21:00:00.010 ← MarkCount 0",
            "2026-01-02 21:00:02.295 → GetMarkCount",
            "2026-01-02 21:00:02.310 ← MarkCount 0",
            "2026-01-02 21:00:02.700 → GetMarkCount",
            "2026-01-02 21:00:02.710 ← MarkCount 1",
        ])) + "\n"
        rows, report, exit_code = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser, strict=True)
        self.assertEqual((rows, exit_code, report["status"]), ([], 0, "CLEAR"))

    def test_delayed_confirmation_without_resend_is_not_duplicate(self):
        plc, station, boxes, laser, _ = test_scenarios.synthetic_shift(18, 3, False, None)
        laser = laser.replace("21:00:02.300 ← MarkEnd", "21:00:02.320 ← MarkEnd")
        laser = "\n".join(sorted(laser.splitlines() + [
            "2026-01-02 21:00:00.010 ← MarkCount 0",
            "2026-01-02 21:00:02.310 ← MarkCount 1",
        ])) + "\n"
        rows, report, exit_code = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser, strict=True)
        self.assertFalse(any(row["вид"] == "В" for row in rows))
        self.assertNotEqual(report["status"], "CLEAR")
        self.assertEqual(exit_code, 20)

    def test_clock_rollback_reports_uncertainty_and_keeps_proven_extra(self):
        data = test_scenarios.synthetic_shift(18, 3, False, "failed", spacing=1.0)
        cutoff = datetime(2026, 1, 2, 21, 0, 8, 800000)
        def shift(text, column):
            result = []
            for line in text.splitlines():
                parts = line.split(";") if column else None
                value = parts[1] if column else line[:23]
                time = datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
                if time >= cutoff:
                    stamp = (time - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    if column:
                        parts[1] = stamp
                        line = ";".join(parts)
                    else:
                        line = stamp + line[23:]
                result.append(line)
            return "\n".join(result) + "\n"
        plc, station, boxes, laser = [shift(data[i], i == 2) for i in range(4)]
        rows, report, code = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser, strict=True)
        self.assertEqual(code, 20)
        self.assertTrue(any(r["вид"] == "Б" and r["туба"] == "2" for r in rows), report)
        self.assertTrue(any(i["code"] == "CLOCK_CORRECTION_INTERVAL" for i in report["issues"]))

    def test_malformed_input_has_machine_error_without_csv(self):
        for fault in ["counter_reset", "nan_speed", "box_count", "missing_laser", "invalid_utf8", "truncated_csv", "truncated_log", "duplicate_box_code"]:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                plc, station, boxes, laser, _ = test_scenarios.synthetic_shift(18, 3, False, None)
                if fault == "counter_reset":
                    plc += "2026-01-02 22:00:00.000;0;0;0;0;1;18\n"
                if fault == "nan_speed":
                    plc = plc.replace(";1;18", ";1;NaN", 1)
                if fault == "truncated_csv":
                    plc += "2026-01-02 22:00:00.000;9\n"
                if fault == "truncated_log":
                    laser += "2026-01-02 22:\n"
                if fault == "duplicate_box_code":
                    boxes = boxes.replace(";авто;3;CODE1 CODE2 CODE3", ";авто;4;CODE1 CODE1 CODE2 CODE3", 1)
                if fault == "box_count":
                    boxes = boxes.replace(";авто;3;", ";авто;99;", 1)
                for name, text in [("plc.csv", "время;n_in;n_push;push_no;n_out;лента;скорость\n" + plc),
                                   ("station.log", station),
                                   ("boxes.csv", "sscc;этикетка;вид;кодов;коды\n" + boxes),
                                   ("laser.log", laser)]:
                    if name == "laser.log" and fault == "missing_laser":
                        continue
                    (folder / name).write_bytes(b"\xff" if name == "laser.log" and fault == "invalid_utf8" else text.encode())
                result = subprocess.run([sys.executable, str(test_detector.SCRIPT), directory, "--strict"],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr)["status"], "INPUT_ERROR")

    def test_replay_is_deterministic(self):
        data = test_scenarios.synthetic_shift(26, 7, True, "failed")[:4]
        first = test_detector.EvidenceTests().run_shift(*data, strict=True)
        second = test_detector.EvidenceTests().run_shift(*data, strict=True)
        self.assertEqual(first, second)

    def test_snapshot_change_and_deadline_abort_before_output(self):
        for mode in ["mutation", "deadline"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                plc, station, boxes, laser, _ = test_scenarios.synthetic_shift(18, 3, False, None)
                for name, text in [("plc.csv", "время;n_in;n_push;push_no;n_out;лента;скорость\n" + plc),
                                   ("station.log", station), ("laser.log", laser),
                                   ("boxes.csv", "sscc;этикетка;вид;кодов;коды\n" + boxes)]:
                    (folder / name).write_text(text, encoding="utf-8")
                spec = importlib.util.spec_from_file_location("autonomous_runtime", test_detector.SCRIPT)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                fingerprint = module.input_fingerprint
                changed = False
                def mutate(path):
                    nonlocal changed
                    result = fingerprint(path)
                    if not changed:
                        with (folder / "station.log").open("a", encoding="utf-8") as stream:
                            stream.write("2026-01-02 22:00:00.000 Стоп ленты\n")
                        changed = True
                    return result
                target = patch.object(module, "input_fingerprint", side_effect=mutate) if mode == "mutation" else patch.object(module.time, "monotonic", side_effect=[0.0] + [111.0] * 100)
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(sys, "argv", [str(test_detector.SCRIPT), directory, "--strict"]), target, redirect_stdout(output), redirect_stderr(errors):
                    code = module.main()
                self.assertEqual(code, 2)
                self.assertEqual(output.getvalue(), "")
                report = json.loads(errors.getvalue())
                self.assertEqual(report["status"], "INPUT_ERROR")
                detail = report["issues"][0]["detail"]
                self.assertIn("изменились" if mode == "mutation" else "110 секунд", detail)
                if mode == "mutation":
                    self.assertTrue((folder / "station.log").read_text().endswith("Стоп ленты\n"))

    def test_unassigned_output_is_not_a_clean_empty_shift(self):
        rows, report, code = test_detector.EvidenceTests().run_shift(
            "2026-01-02 21:00:00.000;100;0;0;90;1;18\n"
            "2026-01-02 21:00:01.000;100;0;0;91;1;18\n",
            "", "", strict=True,
        )
        self.assertEqual(code, 20)
        self.assertEqual(report["status"], "INCOMPLETE")
        self.assertTrue(any(i["code"] == "UNASSIGNED_OUTPUTS" for i in report["issues"]))

    def test_repeated_camera_code_without_prints_is_not_silent(self):
        plc, station, boxes, _, _ = test_scenarios.synthetic_shift(18, 1, False, None)
        station = station.replace("CODE2", "CODE1")
        boxes = boxes.replace("CODE2", "CODE1")
        rows, report, code = test_detector.EvidenceTests().run_shift(plc, station, boxes, "", strict=True)
        self.assertEqual(code, 20)
        self.assertTrue(any(i["code"] == "REPEATED_CAMERA_CODE" for i in report["issues"]))
        self.assertTrue(any(r["вид"] == "Г" for r in rows))

    def test_station_tail_outside_plc_is_incomplete(self):
        plc, station, boxes, laser, _ = test_scenarios.synthetic_shift(18, 3, False, None)
        station += "2026-01-02 21:01:00.000 №10 брак NoRead\n"
        rows, report, code = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser, strict=True)
        self.assertEqual(code, 20)
        self.assertTrue(any(i["code"] == "STATION_OUTSIDE_PLC" for i in report["issues"]))

    def test_print_tail_outside_plc_is_incomplete(self):
        plc, station, boxes, laser, _ = test_scenarios.synthetic_shift(18, 3, False, None)
        laser += "2026-01-02 21:01:00.000 → SendContent EXTRA\n2026-01-02 21:01:00.300 ← MarkEnd\n"
        rows, report, code = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser, strict=True)
        self.assertEqual(code, 20)
        self.assertTrue(any(i["code"] == "PRINT_OUTSIDE_PLC" for i in report["issues"]))

    def test_manual_box_record_does_not_prove_physical_contents(self):
        rows, report, code = test_detector.EvidenceTests().run_shift(
            "2026-01-02 21:00:00.000;0;0;0;0;0;18\n",
            "", "123;2026-01-02 21:00:01.000;ручной;1;CODE\n", strict=True,
        )
        self.assertEqual((rows, code), ([], 20))
        self.assertTrue(any(i["code"] == "MANUAL_CONTENT_UNOBSERVED" for i in report["issues"]))

    def test_empty_observed_shift_is_clear(self):
        rows, report, code = test_detector.EvidenceTests().run_shift(
            "2026-01-02 21:00:00.000;0;0;0;0;0;18\n"
            "2026-01-02 21:00:01.000;0;0;0;0;0;18\n",
            "", "", strict=True,
        )
        self.assertEqual((rows, report["status"], code), ([], "CLEAR", 0))

    def test_unaligned_clock_epochs_suppress_physical_claims(self):
        plc, station, boxes, laser, _ = test_scenarios.synthetic_shift(18, 3, False, "failed")
        station += "2026-01-02 20:59:00.000 Стоп ленты\n"
        rows, report, code = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser, strict=True)
        self.assertEqual(code, 20)
        self.assertFalse(any(row["вид"] in {"А", "Б", "В"} for row in rows))
        self.assertTrue(any(i["code"] == "CLOCK_EPOCH_MISMATCH" for i in report["issues"]))

    def test_default_cli_is_autonomous_and_preserves_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            plc, station, boxes, laser, expected = test_scenarios.synthetic_shift(26, 3, True, "failed")
            files = {"plc.csv": "время;n_in;n_push;push_no;n_out;лента;скорость\n" + plc,
                     "station.log": station, "laser.log": laser,
                     "boxes.csv": "sscc;этикетка;вид;кодов;коды\n" + boxes}
            for name, text in files.items():
                (folder / name).write_text(text, encoding="utf-8")
            result = subprocess.run([sys.executable, str(test_detector.SCRIPT), directory],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 10, result.stderr)
            report = json.loads(result.stderr)
            self.assertEqual(report["status"], "ANOMALIES")
            rows = list(csv.DictReader(io.StringIO(result.stdout), delimiter=";"))
            self.assertEqual([(r["вид"], r["туба"], r["код"], r["короб"]) for r in rows], expected)
            self.assertEqual({p.name: p.read_text() for p in folder.iterdir()}, files)

    def test_cli_argument_errors_are_machine_readable(self):
        for arguments in [[], ["missing", "--strict", "--compat"], ["missing", "--unknown"]]:
            with self.subTest(arguments=arguments):
                result = subprocess.run([sys.executable, str(test_detector.SCRIPT)] + arguments,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr)["status"], "INPUT_ERROR")

    def test_file_size_limit_is_rejected_before_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with (folder / "station.log").open("wb") as stream:
                stream.truncate(64 * 1024 * 1024 + 1)
            result = subprocess.run([sys.executable, str(test_detector.SCRIPT), directory],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, "")
            self.assertIn("64 МБ", json.loads(result.stderr)["issues"][0]["detail"])

    def test_restart_with_empty_buffer_is_resolved_without_false_alarm(self):
        for mode in ["empty_buffer", "unprinted"]:
            with self.subTest(mode=mode):
                rows, report, code = test_detector.EvidenceTests().run_shift(*test_restarts.restart_shift(mode), strict=True)
                self.assertEqual((rows, code, report["status"]), ([], 0, "CLEAR"), report["issues"])

    def test_bounce_across_two_polls_keeps_physical_number(self):
        rows, report, code = test_detector.EvidenceTests().run_shift(
            "2026-01-02 21:00:00.000;0;0;0;0;1;18\n"
            "2026-01-02 21:00:01.990;0;0;0;0;1;18\n"
            "2026-01-02 21:00:02.010;1;0;0;0;1;18\n"
            "2026-01-02 21:00:02.030;1;0;0;0;1;18\n"
            "2026-01-02 21:00:02.050;2;0;0;0;1;18\n"
            "2026-01-02 21:00:04.010;2;1;1;0;1;18\n"
            "2026-01-02 21:00:05.000;2;1;1;0;1;18\n",
            "2026-01-02 21:00:02.100 №2 годен GOOD\n"
            "2026-01-02 21:00:04.700 Этикетка 123, дата 02.01.2026\n",
            "123;2026-01-02 21:00:04.700;авто;1;GOOD\n",
            "2026-01-02 21:00:00.100 → SendContent GOOD\n"
            "2026-01-02 21:00:00.967 ← MarkEnd\n",
            strict=True,
        )
        self.assertEqual([(r["вид"], r["туба"], r["код"]) for r in rows], [("А", "1", "GOOD")])
        self.assertEqual(code, 10, report["issues"])


if __name__ == "__main__":
    unittest.main()
