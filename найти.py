#!/usr/bin/env python3
"""Консервативный сторож расхождений линии 2.

Запуск: python3 найти.py <папка>
В папке ожидаются station.log, plc.csv, laser.log, boxes.csv; допустим также
вариант, когда они лежат во вложенной папке data.
"""

from __future__ import annotations

import bisect
import csv
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


STAMP = "%Y-%m-%d %H:%M:%S.%f"


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, STAMP)


def show_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class MonotonicClock:
    """Сохраняет порядок строк, даже если часы станции перевели назад."""

    def __init__(self) -> None:
        self.previous: datetime | None = None
        self.offset = timedelta()

    def convert(self, raw: datetime) -> datetime:
        if self.previous is not None and raw < self.previous - timedelta(seconds=5):
            self.offset += self.previous - raw + timedelta(milliseconds=1)
        self.previous = raw
        return raw + self.offset


@dataclass
class Tube:
    number: int
    s1_raw: datetime | None = None
    s1_mono: datetime | None = None
    input_group: int = -1
    speed: float = 0.0
    pushed: bool = False
    push_raw: datetime | None = None
    push_mono: datetime | None = None


@dataclass
class StationEvent:
    raw: datetime
    mono: datetime
    line: int
    number: int
    verdict: str
    code: str


@dataclass
class Box:
    sscc: str
    raw: datetime
    mono: datetime
    kind: str
    codes: list[str]
    line: int


def locate_data(folder: Path) -> Path:
    if (folder / "station.log").is_file():
        return folder
    if (folder / "data" / "station.log").is_file():
        return folder / "data"
    raise FileNotFoundError("не найдены station.log/plc.csv/laser.log/boxes.csv")


def read_plc(folder: Path):
    rows: list[dict] = []
    tubes: dict[int, Tube] = {}
    samples: list[tuple[datetime, int]] = []
    out_changes: list[tuple[datetime, int]] = []
    clock = MonotonicClock()

    with (folder / "plc.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        for row in reader:
            raw = parse_time(row["время"])
            mono = clock.convert(raw)
            item = {
                "raw": raw,
                "mono": mono,
                "n_in": int(row["n_in"]),
                "n_push": int(row["n_push"]),
                "push_no": int(row["push_no"]),
                "n_out": int(row["n_out"]),
                "belt": int(row["лента"]),
                "speed": float(row["скорость"].replace(",", ".")),
            }
            rows.append(item)
            samples.append((mono, item["n_out"]))

    if not rows:
        return tubes, rows, samples, out_changes

    previous = {
        "raw": rows[0]["raw"],
        "mono": rows[0]["mono"],
        "n_in": 0,
        "n_push": 0,
        "n_out": 0,
        "push_no": 0,
    }
    group = 0
    for row in rows:
        added = row["n_in"] - previous["n_in"]
        if added > 0:
            group += 1
            raw_span = row["raw"] - previous["raw"]
            mono_span = row["mono"] - previous["mono"]
            for index, number in enumerate(
                range(previous["n_in"] + 1, row["n_in"] + 1), 1
            ):
                # При пропуске опросов порядок известен; время равномерно
                # интерполируется и явно описано в отчёте как оценка.
                fraction = index / added
                tubes[number] = Tube(
                    number=number,
                    s1_raw=(
                        row["raw"]
                        if added == 1
                        else previous["raw"] + raw_span * fraction
                    ),
                    s1_mono=(
                        row["mono"]
                        if added == 1
                        else previous["mono"] + mono_span * fraction
                    ),
                    input_group=group,
                    speed=row["speed"],
                )

        pushed = row["n_push"] - previous["n_push"]
        if pushed > 0:
            number = row["push_no"]
            tube = tubes.setdefault(number, Tube(number))
            tube.pushed = True
            tube.push_raw = row["raw"]
            tube.push_mono = row["mono"]

        passed = row["n_out"] - previous["n_out"]
        if passed > 0:
            span = row["mono"] - previous["mono"]
            for index, count in enumerate(
                range(previous["n_out"] + 1, row["n_out"] + 1), 1
            ):
                when = (
                    row["mono"]
                    if passed == 1
                    else previous["mono"] + span * (index / passed)
                )
                out_changes.append((when, count))
        previous = row

    return tubes, rows, samples, out_changes


def read_station(folder: Path):
    numbered: list[StationEvent] = []
    repeats: list[tuple[datetime, datetime, str, int]] = []
    labels: dict[str, datetime] = {}
    stops: list[tuple[datetime, datetime]] = []
    starts: list[tuple[datetime, datetime]] = []
    requeues: list[tuple[datetime, datetime, str, int]] = []
    clock = MonotonicClock()

    with (folder / "station.log").open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            line = line.rstrip("\r\n")
            if len(line) < 24:
                continue
            raw = parse_time(line[:23])
            mono = clock.convert(raw)
            message = line[24:]

            match = re.match(r"№(\d+) (годен|брак) (\S+)", message)
            if match:
                numbered.append(
                    StationEvent(
                        raw,
                        mono,
                        line_number,
                        int(match.group(1)),
                        match.group(2),
                        match.group(3),
                    )
                )
                continue
            match = re.match(r"повтор (\S+)", message)
            if match:
                repeats.append((raw, mono, match.group(1), line_number))
                continue
            match = re.match(r"Этикетка (\d+),", message)
            if match:
                labels[match.group(1)] = mono
                continue
            match = re.match(
                r"Код (\S+) был отдан маркиратору, MarkEnd не пришёл", message
            )
            if match:
                requeues.append((raw, mono, match.group(1), line_number))
                continue
            if message == "Стоп ленты":
                stops.append((raw, mono))
            elif message.startswith("Пуск ленты"):
                starts.append((raw, mono))

    return numbered, repeats, labels, stops, starts, requeues


def read_boxes(folder: Path, label_times: dict[str, datetime]):
    boxes: list[Box] = []
    with (folder / "boxes.csv").open(encoding="utf-8-sig", newline="") as stream:
        for line_number, row in enumerate(csv.DictReader(stream, delimiter=";"), 2):
            raw = parse_time(row["этикетка"])
            boxes.append(
                Box(
                    row["sscc"],
                    raw,
                    label_times.get(row["sscc"], raw),
                    row["вид"],
                    row["коды"].split(),
                    line_number,
                )
            )
    return boxes


def read_laser(folder: Path):
    sends: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    completed: list[tuple[datetime, datetime, str]] = []
    mark_counts: list[tuple[datetime, int]] = []
    current_code: str | None = None
    clock = MonotonicClock()

    with (folder / "laser.log").open(encoding="utf-8-sig") as stream:
        for line in stream:
            line = line.rstrip("\r\n")
            if len(line) < 24:
                continue
            raw = parse_time(line[:23])
            mono = clock.convert(raw)
            message = line[24:]
            match = re.search(r"SendContent (\S+)", message)
            if match:
                current_code = match.group(1)
                sends[current_code].append((raw, mono))
            elif message.endswith("MarkEnd") and current_code:
                completed.append((raw, mono, current_code))
            else:
                match = re.search(r"MarkCount (\d+)", message)
                if match:
                    mark_counts.append((mono, int(match.group(1))))
    return sends, completed, mark_counts


def main() -> int:
    if len(sys.argv) != 2:
        print("Использование: python3 найти.py <папка>", file=sys.stderr)
        return 2

    try:
        folder = locate_data(Path(sys.argv[1]))
        tubes, plc_rows, plc_samples, out_changes = read_plc(folder)
        numbered, repeats, labels, stops, starts, requeues = read_station(folder)
        boxes = read_boxes(folder, labels)
        sends, completed_marks, mark_counts = read_laser(folder)
    except (OSError, ValueError, KeyError) as error:
        print(f"Ошибка входных данных: {error}", file=sys.stderr)
        return 1

    events_by_number: dict[int, list[StationEvent]] = defaultdict(list)
    good_by_code: dict[str, list[StationEvent]] = defaultdict(list)
    for event in numbered:
        events_by_number[event.number].append(event)
        if event.verdict == "годен" and event.code != "NoRead":
            good_by_code[event.code].append(event)

    code_boxes: dict[str, list[Box]] = defaultdict(list)
    for box in boxes:
        for code in box.codes:
            code_boxes[code].append(box)

    rows: list[dict] = []
    used_a_codes: set[str] = set()

    def add_row(
        kind: str,
        times: str,
        tube_numbers: str,
        code: str,
        box_value: str,
        reason: str,
        sort_at: datetime,
        pre_s2: bool = False,
    ) -> None:
        rows.append(
            {
                "вид": kind,
                "время": times,
                "туба": tube_numbers,
                "код": code,
                "короб": box_value,
                "причина": reason,
                "_sort": sort_at,
                "_pre_s2": pre_s2,
            }
        )

    # А: код успел попасть в запись, но последний/опоздавший вердикт привёл к толчку.
    for number in sorted(events_by_number):
        tube = tubes.get(number)
        if not tube or not tube.pushed or tube.s1_raw is None or tube.s1_mono is None:
            continue
        events = events_by_number[number]
        good = next(
            (e for e in events if e.verdict == "годен" and e.code in code_boxes), None
        )
        if good is None or good.code in used_a_codes:
            continue
        target_boxes = [b for b in code_boxes[good.code] if b.kind != "ручной"]
        if not target_boxes:
            continue
        if any(e.verdict == "брак" for e in events if e.mono >= good.mono):
            reason = "после «годен» пришёл NoRead"
        elif tube.push_raw and (tube.push_raw - good.raw).total_seconds() < 0.2:
            reason = "вердикт «годен» опоздал"
        else:
            reason = "ПЛК вытолкнул записанную тубу"
        add_row(
            "А",
            show_time(tube.s1_raw),
            str(number),
            good.code,
            target_boxes[0].sscc,
            reason,
            tube.s1_mono,
            pre_s2=True,
        )
        used_a_codes.add(good.code)

    # А: дребезг S1 дал два номера одному физическому месту. Камера записала код
    # на следующий номер, а повтор того же чтения оставил предыдущий без вердикта.
    repeat_by_code: dict[str, list[tuple[datetime, datetime, str, int]]] = defaultdict(list)
    for repeat in repeats:
        repeat_by_code[repeat[2]].append(repeat)
    for number, tube in sorted(tubes.items()):
        following = tubes.get(number + 1)
        if (
            not tube.pushed
            or tube.s1_raw is None
            or tube.s1_mono is None
            or following is None
            or following.input_group != tube.input_group
        ):
            continue
        next_good = next(
            (
                e
                for e in events_by_number.get(number + 1, [])
                if e.verdict == "годен" and e.code in code_boxes
            ),
            None,
        )
        if next_good is None or next_good.code in used_a_codes:
            continue
        nearby_repeat = any(
            0 <= (repeat[1] - next_good.mono).total_seconds() <= 0.75
            for repeat in repeat_by_code.get(next_good.code, [])
        )
        if not nearby_repeat:
            continue
        target_box = next(
            (b for b in code_boxes[next_good.code] if b.kind != "ручной"), None
        )
        if target_box is None:
            continue
        add_row(
            "А",
            show_time(tube.s1_raw),
            str(number),
            next_good.code,
            target_box.sscc,
            "дребезг S1 оставил тубу без вердикта",
            tube.s1_mono,
            pre_s2=True,
        )
        used_a_codes.add(next_good.code)

    # В: повторная выдача кода после потери MarkEnd считается настоящим дублем
    # только если MarkCount доказывает неувиденное станцией нанесение.
    mark_count_times = [item[0] for item in mark_counts]
    for _raw, requeue_mono, code, _line in requeues:
        sent = sends.get(code, [])
        before_sends = [item for item in sent if item[1] < requeue_mono]
        after_sends = [item for item in sent if item[1] > requeue_mono]
        if not before_sends or not after_sends:
            continue
        first_send = before_sends[-1]
        second_send = after_sends[0]
        right = bisect.bisect_right(mark_count_times, second_send[1]) - 1
        left = bisect.bisect_left(mark_count_times, first_send[1]) - 1
        if left < 0 or right >= len(mark_counts):
            continue
        count_before = mark_counts[left]
        count_after = mark_counts[right]
        observed = sum(
            count_before[0] < item[1] <= count_after[0] for item in completed_marks
        )
        if count_after[1] - count_before[1] <= observed:
            continue

        # Привязываем потерянное нанесение к номеру через последний код, для
        # которого одновременно известны MarkEnd и номер камеры.
        base = None
        for mark in completed_marks:
            if mark[1] >= first_send[1] or mark[2] not in good_by_code:
                continue
            candidate_event = min(good_by_code[mark[2]], key=lambda e: abs((e.mono - mark[1]).total_seconds()))
            base = (mark, candidate_event)
        second_event = next(
            (e for e in good_by_code.get(code, []) if e.mono > requeue_mono), None
        )
        if base is None or second_event is None:
            continue
        mark, base_event = base
        completed_between = sum(
            mark[1] < item[1] < first_send[1] for item in completed_marks
        )
        first_number = base_event.number + completed_between + 1
        first_tube = tubes.get(first_number)
        second_tube = tubes.get(second_event.number)
        target_box = next((b for b in code_boxes.get(code, []) if b.kind != "ручной"), None)
        if (
            first_tube is None
            or second_tube is None
            or first_tube.s1_raw is None
            or first_tube.s1_mono is None
            or second_tube.s1_raw is None
            or target_box is None
        ):
            continue
        add_row(
            "В",
            f"{show_time(first_tube.s1_raw)} {show_time(second_tube.s1_raw)}",
            f"{first_number} {second_event.number}",
            code,
            target_box.sscc,
            "код возвращён в очередь после потерянного MarkEnd",
            first_tube.s1_mono,
        )

    # Б: сравниваем накопительный n_out с записанными кодами. Учтены уже
    # доказанные А до S2. Случай выдаётся лишь когда нижняя граница n_out уже
    # больше ожидаемой и внутри именно этого короба был NoRead.
    auto_boxes = sorted(
        (box for box in boxes if box.kind != "ручной" and box.sscc in labels),
        key=lambda box: box.mono,
    )
    sample_times = [item[0] for item in plc_samples]
    missing_by_box = Counter(
        row["короб"] for row in rows if row["вид"] == "А" and row["_pre_s2"]
    )
    unread = sorted(
        (e for e in numbered if e.verdict == "брак" and e.code == "NoRead"),
        key=lambda e: e.mono,
    )
    used_unread: set[int] = set()
    cumulative = 0
    missing = 0
    extra = 0
    previous_label: datetime | None = None
    for box in auto_boxes:
        cumulative += len(box.codes)
        missing += missing_by_box[box.sscc]
        expected = cumulative - missing + extra
        index = bisect.bisect_right(sample_times, box.mono) - 1
        lower = plc_samples[index][1] if index >= 0 else 0
        surplus = lower - expected
        if surplus > 0:
            candidates = [
                e
                for e in unread
                if e.number not in used_unread
                and (previous_label is None or e.mono > previous_label)
                and e.mono < box.mono
            ]
            for event in candidates[-surplus:]:
                prior_marks = [item for item in completed_marks if item[1] <= event.mono]
                code = prior_marks[-1][2] if prior_marks else "NoRead"
                tube = tubes.get(event.number)
                if tube is None or tube.s1_raw is None or tube.s1_mono is None:
                    continue
                add_row(
                    "Б",
                    show_time(tube.s1_raw),
                    str(event.number),
                    code,
                    box.sscc,
                    "NoRead не был физически вытолкнут",
                    tube.s1_mono,
                )
                used_unread.add(event.number)
                extra += 1
        previous_label = box.mono

    # Г: boxes.csv даёт прямое и самое надёжное доказательство. Порядок SSCC —
    # порядок создания коробов, размер короба нигде не предполагается.
    box_order = {box.sscc: index for index, box in enumerate(boxes)}
    for code, occurrences in code_boxes.items():
        unique = {box.sscc: box for box in occurrences}
        if len(unique) < 2:
            continue
        ordered = sorted(unique.values(), key=lambda box: box_order[box.sscc])
        event = next(iter(good_by_code.get(code, [])), None)
        if event is None:
            continue
        tube = tubes.get(event.number)
        if tube is None or tube.s1_raw is None or tube.s1_mono is None:
            continue
        reason = (
            "ручной скан не проверил глобальный дубль"
            if any(box.kind == "ручной" for box in ordered)
            else "код записан в два короба"
        )
        add_row(
            "Г",
            show_time(tube.s1_raw),
            str(event.number),
            code,
            " ".join(box.sscc for box in ordered),
            reason,
            tube.s1_mono,
        )

    rows.sort(key=lambda row: (row["_sort"], row["вид"], row["туба"]))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="")
    writer = csv.writer(sys.stdout, delimiter=";", lineterminator="\n")
    writer.writerow(["вид", "время", "туба", "код", "короб", "причина"])
    for row in rows:
        writer.writerow(
            [
                row["вид"],
                row["время"],
                row["туба"],
                row["код"],
                row["короб"],
                row["причина"],
            ]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

