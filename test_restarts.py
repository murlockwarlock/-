from datetime import datetime, timedelta
import unittest

import test_detector


def restart_shift(mode):
    def stamp(seconds):
        return (datetime(2026, 2, 3, 21) + timedelta(seconds=seconds)).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]

    repeated = mode != "no_resend"
    printing = {1: "BASE"}
    if mode != "unprinted":
        printing[2] = "OTHER" if mode in {"unrelated", "multiple"} else "RETRY"
    if mode == "multiple":
        printing[3] = "RETRY"
    if mode == "starvation":
        printing.pop(2, None)
        printing[3] = "RETRY"
    if mode == "empty_buffer":
        printing.pop(2, None)
    positions = {1: 1.0, 2: 3.0, 3: 5.0, 4: 7.0, 5: 9.0}
    if mode == "unprinted":
        positions = {1: 1.0, 2: 9.0}
    if mode == "unrelated":
        positions = {1: 1.0, 2: 3.0, 3: 9.0}
        printing = {1: "OTHER", 2: "BASE"}
    base_number = 2 if mode == "unrelated" else 1
    base_time = positions[base_number]
    final_number = max(positions)
    if repeated:
        printing[final_number] = "RETRY"
    recovered_count = 100 + sum(number != final_number for number in printing)
    if mode == "reset":
        recovered_count = 1
    laser = [(0.0, "← MarkCount 100"),
             (0.1, "→ SendContent BASE"), (1.3, "← MarkEnd")]
    if mode == "unrelated":
        laser = [(0.0, "← MarkCount 100"), (0.1, "→ SendContent OTHER"),
                 (1.31, "→ SendContent BASE"), (3.3, "← MarkEnd"),
                 (3.31, "→ SendContent RETRY")]
    elif mode == "multiple":
        laser.extend([(1.31, "→ SendContent OTHER"), (3.31, "→ SendContent RETRY")])
    elif mode == "starvation":
        laser.append((3.9, "→ SendContent RETRY"))
    elif mode != "empty_buffer":
        laser.append((1.31, "→ SendContent RETRY"))
    if mode != "missing_counter":
        laser.append((8.05, f"← MarkCount {recovered_count}"))
    if repeated:
        laser.extend([(8.1, "→ SendContent RETRY"), (9.3, "← MarkEnd")])
    station = [(0.0, "Пуск ленты, 18 м/мин"),
               (base_time + 1.43, f"№{base_number} годен BASE"),
               (8.0, "Запуск станции — прошлый запуск завершился аварийно"),
               (8.01, "Код RETRY был отдан маркиратору, MarkEnd не пришёл — возвращён в очередь")]
    if mode == "empty_buffer":
        station = [(t, m) for t, m in station if not m.startswith("Код RETRY") ]
    if repeated:
        station.append((10.43, f"№{final_number} годен RETRY"))
    else:
        station.append((10.43, f"№{final_number} брак NoRead"))
    for number, marked_at in positions.items():
        camera_at = marked_at + 1.43
        if number in {base_number, final_number}:
            continue
        if not base_time + 1.5 < camera_at < 8.0:
            station.append((camera_at, f"№{number} брак NoRead"))
    label_time = 9 + 1.1 / 0.3 + 0.3
    station.append((label_time, "Этикетка 123, дата 03.02.2026"))
    codes = "BASE RETRY" if repeated else "BASE"
    boxes = f"123;{stamp(label_time)};авто;{len(codes.split())};{codes}\n"
    plc = []
    for tick in range(151):
        now = tick / 10
        if base_time + 1.5 < now < 8.0:
            continue
        n_in = sum(t + 0.4 / 0.3 <= now for t in positions.values())
        pushed = [number for number, t in positions.items()
                  if number != base_number and (number != final_number or not repeated)
                  and t + 1 / 0.3 <= now]
        n_out = sum(t + 1.1 / 0.3 <= now for number, t in positions.items()
                    if number == base_number or (number == final_number and repeated))
        plc.append(f"{stamp(now)};{n_in};{len(pushed)};{pushed[-1] if pushed else 0};"
                   f"{n_out};1;18\n")
    return (''.join(plc), ''.join(f"{stamp(t)} {m}\n" for t, m in sorted(station)),
            boxes, ''.join(f"{stamp(t)} {m}\n" for t, m in sorted(laser)))


class RestartScenarios(unittest.TestCase):
    def test_lost_confirmation_and_real_duplicate(self):
        rows = test_detector.EvidenceTests().run_shift(*restart_shift("lost_ack"))
        self.assertEqual([(r["вид"], r["туба"], r["код"]) for r in rows],
                         [("В", "2 5", "RETRY")])

    def test_no_duplicate_claim_without_code_specific_evidence(self):
        for mode in ["unprinted", "unrelated", "no_resend", "reset", "empty_buffer", "missing_counter"]:
            with self.subTest(mode=mode):
                rows = test_detector.EvidenceTests().run_shift(*restart_shift(mode))
                self.assertEqual(rows, [])

    def test_ambiguous_loss_is_not_assigned_to_wrong_tube(self):
        for mode in ["multiple", "starvation"]:
            with self.subTest(mode=mode):
                rows = test_detector.EvidenceTests().run_shift(*restart_shift(mode))
                for row in rows:
                    self.assertEqual((row["вид"], row["туба"], row["код"]),
                                     ("В", "3 5", "RETRY"))

    def test_recovered_open_box_does_not_hide_previous_box_assignment(self):
        plc, station, boxes, laser = restart_shift("unprinted")
        station = "\n".join(sorted(station.splitlines() + [
            "2026-02-03 21:00:04.967 Этикетка 122, дата 03.02.2026",
        ])) + "\n"
        boxes = "122;2026-02-03 21:00:04.967;авто;1;BASE\n" + boxes
        rows = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser)
        self.assertEqual([(r["вид"], r["код"], r["короб"]) for r in rows],
                         [("Г", "BASE", "122 123")])

    def test_repeated_recovery_notice_does_not_duplicate_case(self):
        plc, station, boxes, laser = restart_shift("lost_ack")
        notice = "Код RETRY был отдан маркиратору, MarkEnd не пришёл — возвращён в очередь"
        station = station.replace(
            "2026-02-03 21:00:08.010 " + notice,
            "2026-02-03 21:00:08.010 " + notice + "\n"
            "2026-02-03 21:00:08.020 " + notice,
        )
        rows = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser)
        self.assertEqual([(r["вид"], r["туба"]) for r in rows], [("В", "2 5")])


if __name__ == "__main__":
    unittest.main()
