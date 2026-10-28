import csv
import io
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("найти.py")


class EvidenceTests(unittest.TestCase):
    def run_shift(self, plc, station, boxes):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "plc.csv").write_text(
                "время;n_in;n_push;push_no;n_out;лента;скорость\n" + plc,
                encoding="utf-8",
            )
            (folder / "station.log").write_text(station, encoding="utf-8")
            (folder / "boxes.csv").write_text(
                "sscc;этикетка;вид;кодов;коды\n" + boxes, encoding="utf-8"
            )
            (folder / "laser.log").write_text("", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), directory],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return list(csv.DictReader(io.StringIO(result.stdout), delimiter=";"))

    def test_sparse_pushes_do_not_invent_rejection_of_middle_tube(self):
        rows = self.run_shift(
            "2026-10-21 21:00:00.000;0;0;0;0;1;18\n"
            "2026-10-21 21:00:05.000;3;2;3;1;1;18\n",
            "2026-10-21 21:00:02.000 №2 годен CODE2\n"
            "2026-10-21 21:00:03.000 №3 годен CODE3\n",
            "BOX;2026-10-21 21:00:06.000;авто;2;CODE2 CODE3\n",
        )
        self.assertEqual([(r["вид"], r["туба"], r["код"]) for r in rows],
                         [("А", "3", "CODE3")])

    def test_stop_and_repeated_reads_do_not_prove_manual_removal(self):
        rows = self.run_shift(
            "2026-10-21 21:00:00.000;0;0;0;0;1;18\n"
            "2026-10-21 21:00:01.000;1;0;0;0;1;18\n"
            "2026-10-21 21:02:01.000;1;0;0;1;1;18\n",
            "2026-10-21 21:00:01.050 №1 годен CODE1\n"
            "2026-10-21 21:00:01.100 Стоп ленты\n"
            "2026-10-21 21:00:02.000 повтор CODE1\n"
            "2026-10-21 21:00:03.000 повтор CODE1\n"
            "2026-10-21 21:00:04.000 повтор CODE1\n"
            "2026-10-21 21:02:00.000 Пуск ленты, 18 м/мин\n",
            "BOX;2026-10-21 21:02:02.000;авто;1;CODE1\n",
        )
        self.assertEqual(rows, [])


    def test_manual_duplicate_without_camera_history(self):
        rows = self.run_shift(
            "2026-10-21 21:00:00.000;0;0;0;0;0;18\n",
            "",
            "B1;2026-10-21 21:01:00.000;ручной;1;CODE\n"
            "B2;2026-10-21 21:02:00.000;ручной;1;CODE\n",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["вид"], rows[0]["код"], rows[0]["короб"]),
                         ("Г", "CODE", "B1 B2"))
        self.assertEqual((rows[0]["время"], rows[0]["туба"]), ("", ""))

    def test_duplicate_assignment_is_not_also_missing(self):
        rows = self.run_shift(
            "2026-10-21 21:00:00.000;0;0;0;0;1;18\n"
            "2026-10-21 21:00:01.000;1;0;0;0;1;18\n"
            "2026-10-21 21:00:03.000;1;1;1;0;1;18\n",
            "2026-10-21 21:00:01.050 №1 годен CODE\n",
            "B1;2026-10-21 21:01:00.000;авто;1;CODE\n"
            "B2;2026-10-21 21:02:00.000;ручной;1;CODE\n",
        )
        self.assertEqual([r["вид"] for r in rows], ["Г"])

    def test_sensor_bounce_uses_spacing_not_repeated_camera_message(self):
        for speed, expected in [(18, ["А"]), (60, [])]:
            with self.subTest(speed=speed):
                rows = self.run_shift(
                    f"2026-10-21 21:00:00.000;0;0;0;0;1;{speed}\n"
                    f"2026-10-21 21:00:00.300;2;0;0;0;1;{speed}\n"
                    f"2026-10-21 21:00:03.000;2;1;1;0;1;{speed}\n",
                    "2026-10-21 21:00:00.350 №2 годен CODE\n",
                    "BOX;2026-10-21 21:00:04.000;авто;1;CODE\n",
                )
                self.assertEqual([r["вид"] for r in rows], expected)
                if rows:
                    self.assertEqual(rows[0]["туба"], "1")

    def test_clock_epoch_does_not_depend_on_last_poll_time(self):
        from datetime import datetime
        spec = importlib.util.spec_from_file_location("detector_clock_test", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        first, second = module.MonotonicClock(), module.MonotonicClock()
        first.convert(datetime(2026, 10, 21, 23, 31, 0, 100000))
        second.convert(datetime(2026, 10, 21, 23, 31, 0, 900000))
        after = datetime(2026, 10, 21, 23, 30, 1)
        self.assertEqual(first.convert(after), second.convert(after))

    def test_extra_tube_is_found_after_clock_rollback(self):
        rows = self.run_shift(
            "2026-10-21 23:30:59.000;0;0;0;0;1;26\n"
            "2026-10-21 23:31:00.900;2;0;0;0;1;26\n"
            "2026-10-21 23:30:00.050;2;0;0;0;1;26\n"
            "2026-10-21 23:30:03.000;2;0;0;1;1;26\n"
            "2026-10-21 23:30:03.500;2;0;0;2;1;26\n",
            "2026-10-21 23:31:00.000 №1 годен CODE\n"
            "2026-10-21 23:31:00.100 №2 брак NoRead\n"
            "2026-10-21 23:30:00.500 повтор CODE\n"
            "2026-10-21 23:30:03.600 Этикетка 123, дата 21.10.2026\n",
            "123;2026-10-21 23:30:03.600;авто;1;CODE\n",
        )
        self.assertEqual([(r["вид"], r["туба"]) for r in rows], [("Б", "2")])


if __name__ == "__main__":
    unittest.main()
