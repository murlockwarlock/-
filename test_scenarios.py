from datetime import datetime, timedelta
import unittest

import test_detector


def synthetic_shift(speed, box_size, changing, defect):
    velocity = speed / 60
    faster = velocity * 1.25
    change_at = 0.9 / velocity
    stop_at = change_at + 0.9 / faster
    restart_at = stop_at + 1.7

    def reach(position):
        if not changing or position <= 0.9:
            return position / velocity
        if position <= 1.8:
            return change_at + (position - 0.9) / faster
        return restart_at + (position - 1.8) / faster

    def stamp(seconds):
        return (datetime(2026, 1, 2, 21) + timedelta(seconds=seconds)).strftime(
            "%Y-%m-%d %H:%M:%S.%f"
        )[:-3]

    controls = [(0.0, True, speed)]
    station = [(0.0, f"Пуск ленты, {speed} м/мин")]
    if changing:
        controls.extend([(change_at, True, speed * 1.25),
                         (stop_at, False, speed * 1.25),
                         (restart_at, True, speed * 1.25)])
        station.extend([(change_at, f"Скорость ленты: {speed * 1.25} м/мин"),
                        (stop_at, "Стоп ленты"),
                        (restart_at, f"Пуск ленты, {speed * 1.25} м/мин")])
    bad_numbers = {2, 3} if defect else set()
    count = box_size * 3 + len(bad_numbers)
    inputs, pushes, outputs, laser, boxes = [], [], [], [], []
    open_codes = []
    failed = 2 if defect == "failed" else None
    expected_box = None
    for number in range(1, count + 1):
        position = 0.6 + (number - 1) * 0.38
        code = f"CODE{number}"
        marked = reach(position)
        inputs.append((reach(position + 0.4), number))
        laser.extend([(marked - 0.02, f"→ SendContent {code}"),
                      (marked + 0.3, "← MarkEnd")])
        if number in bad_numbers:
            station.append((reach(position + 0.42) + 0.03, f"№{number} брак NoRead"))
            pushes.append((reach(position + 1.0), number))
            if number == failed:
                outputs.append((reach(position + 1.1), number))
            continue
        station.append((reach(position + 0.42) + 0.03, f"№{number} годен {code}"))
        outputs.append((reach(position + 1.1), number))
        open_codes.append(code)
        if len(open_codes) == box_size:
            label = reach(position + 1.1) + 0.3
            sscc = str(100 + len(boxes))
            if expected_box is None and number > 2:
                expected_box = sscc
            boxes.append(f"{sscc};{stamp(label)};авто;{box_size};{' '.join(open_codes)}\n")
            station.append((label, f"Этикетка {sscc}, дата 02.01.2026"))
            open_codes = []
    end = max(t for t, _ in station) + 1
    plc = []
    for tick in range(int(end / 0.05) + 2):
        now = tick * 0.05
        n_in = sum(t <= now for t, _ in inputs)
        pushed = [number for t, number in pushes if t <= now]
        n_out = sum(t <= now for t, _ in outputs)
        _, running, current_speed = [c for c in controls if c[0] <= now][-1]
        plc.append(f"{stamp(now)};{n_in};{len(pushed)};{pushed[-1] if pushed else 0};"
                   f"{n_out};{int(running)};{current_speed}\n")
    station_text = ''.join(f"{stamp(t)} {message}\n" for t, message in sorted(station))
    laser_text = ''.join(f"{stamp(t)} {message}\n" for t, message in sorted(laser))
    expected = [("Б", "2", "CODE2", expected_box)] if failed else []
    return ''.join(plc), station_text, ''.join(boxes), laser_text, expected


class PhysicalScenarios(unittest.TestCase):
    def test_independent_physical_shifts(self):
        for speed in [18, 26, 36]:
            for size in [2, 3, 7]:
                for changing in [False, True]:
                    for defect in [None, "rejected", "failed"]:
                        with self.subTest(speed=speed, size=size, changing=changing, defect=defect):
                            plc, station, boxes, laser, expected = synthetic_shift(
                                speed, size, changing, defect
                            )
                            rows = test_detector.EvidenceTests().run_shift(plc, station, boxes, laser)
                            self.assertEqual(
                                [(r["вид"], r["туба"], r["код"], r["короб"]) for r in rows],
                                expected,
                            )


if __name__ == "__main__":
    unittest.main()
