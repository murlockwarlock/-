import csv
import io
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


if __name__ == "__main__":
    unittest.main()
