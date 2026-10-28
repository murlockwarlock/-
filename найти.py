#!/usr/bin/env python3
"""Консервативный сторож расхождений линии 2.

Запуск: python3 найти.py <папка>
В папке ожидаются station.log, plc.csv, laser.log, boxes.csv; допустим также
вариант, когда они лежат во вложенной папке data.
"""

from __future__ import annotations

import argparse
import bisect
import json
import hashlib
import math
import csv
import re
import sys
import shutil
import tempfile
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


STAMP = "%Y-%m-%d %H:%M:%S.%f"
_deadline = None


def check_budget():
    if _deadline is not None and time.monotonic() >= _deadline:
        raise TimeoutError("превышен лимит анализа 110 секунд")


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, STAMP)


def show_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class MonotonicClock:
    """Общий ключ порядка эпох; разность ключей через перевод не есть длительность."""

    def __init__(self) -> None:
        self.previous: datetime | None = None
        self.offset = timedelta()

    def convert(self, raw: datetime) -> datetime:
        if self.previous is not None and raw < self.previous:
            self.offset += timedelta(days=1)
        self.previous = raw
        return raw + self.offset


@dataclass
class Tube:
    number: int
    physical_number: int | None = None
    s1_raw: datetime | None = None
    s1_mono: datetime | None = None
    s1_lower: datetime | None = None
    s1_upper: datetime | None = None
    s1_lower_raw: datetime | None = None
    s1_upper_raw: datetime | None = None
    input_group: int = -1
    speed: float = 0.0
    duplicate_input: bool = False
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
        required = {"время", "n_in", "n_push", "push_no", "n_out", "лента", "скорость"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("неверный заголовок plc.csv")
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise ValueError("неполная или лишняя колонка plc.csv")
            check_budget()
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
            if (any(item[k] < 0 for k in ("n_in", "n_push", "push_no", "n_out"))
                    or item["n_out"] > item["n_in"]
                    or item["belt"] not in (0, 1) or not math.isfinite(item["speed"])
                    or item["speed"] < 0 or (item["belt"] and item["speed"] == 0)):
                raise ValueError("недопустимое значение в plc.csv")
            rows.append(item)
            samples.append((mono, item["n_out"]))

    if not rows:
        return tubes, rows, samples, out_changes

    previous = rows[0]
    group = 0
    for row in rows[1:]:
        check_budget()
        if any(row[key] < previous[key] for key in ("n_in", "n_push", "n_out")):
            raise ValueError("сброс счётчиков ПЛК внутри смены не поддерживается")
        added = row["n_in"] - previous["n_in"]
        if row["n_in"] - rows[0]["n_in"] > 200_000:
            raise ValueError("смена превышает предел 200000 наблюдаемых входов")
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
                    s1_lower=previous["mono"],
                    s1_upper=row["mono"],
                    s1_lower_raw=previous["raw"],
                    s1_upper_raw=row["raw"],
                    input_group=group,
                    speed=row["speed"],
                    duplicate_input=(
                        added == 2
                        and row["belt"] == previous.get("belt") == 1
                        and row["speed"] == previous.get("speed")
                        and 0 < mono_span.total_seconds() * row["speed"] / 60 < 0.25
                    ),
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
            check_budget()
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
        reader = csv.DictReader(stream, delimiter=";")
        if not {"sscc", "этикетка", "вид", "кодов", "коды"}.issubset(reader.fieldnames or []):
            raise ValueError("неверный заголовок boxes.csv")
        seen = set()
        for line_number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError("неполная или лишняя колонка boxes.csv")
            if (not row["sscc"] or row["sscc"] in seen
                    or row["вид"] not in {"авто", "ручной", "неполный"}
                    or int(row["кодов"]) != len(row["коды"].split())):
                raise ValueError(f"некорректный короб в строке {line_number}")
            seen.add(row["sscc"])
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
    counter_bounds = {}
    request_at = None
    current_code: str | None = None
    clock = MonotonicClock()

    with (folder / "laser.log").open(encoding="utf-8-sig") as stream:
        for line in stream:
            check_budget()
            line = line.rstrip("\r\n")
            if len(line) < 24:
                continue
            raw = parse_time(line[:23])
            mono = clock.convert(raw)
            message = line[24:]
            if message.endswith("GetMarkCount"):
                request_at = mono
            match = re.search(r"SendContent (\S+)", message)
            if match:
                current_code = match.group(1)
                sends[current_code].append((raw, mono))
            elif message.endswith("MarkEnd"):
                completed.append((raw, mono, current_code or ""))
                current_code = None
            else:
                match = re.search(r"MarkCount (\d+)", message)
                if match:
                    mark_counts.append((mono, int(match.group(1))))
                    counter_bounds[mono] = request_at if request_at is not None else mono
                    request_at = None
    return sends, completed, mark_counts, counter_bounds


class BeltMotion:
    def __init__(self, folder: Path, plc_rows: list[dict]) -> None:
        self.changes: dict[datetime, float | None] = {}
        if not plc_rows:
            self.times = []
            return
        first = plc_rows[0]
        speed = first["speed"]
        running = bool(first["belt"])
        self.changes[first["mono"]] = speed / 60 if running else 0.0
        clock = MonotonicClock()
        previous = None
        gaps = []
        with (folder / "station.log").open(encoding="utf-8-sig") as stream:
            for line in stream:
                if len(line) < 24:
                    continue
                raw = parse_time(line[:23])
                offset = clock.offset
                mono = clock.convert(raw)
                if clock.offset != offset and previous is not None:
                    gaps.append((previous, mono))
                    self.changes[mono] = speed / 60 if running else 0.0
                previous = mono
                message = line[24:].strip()
                match = re.match(r"(?:Пуск ленты, |Скорость ленты: )([\d.,]+) м/мин", message)
                if match:
                    speed = float(match[1].replace(",", "."))
                    if message.startswith("Пуск"):
                        running = True
                    self.changes[mono] = speed / 60 if running else 0.0
                elif message == "Стоп ленты":
                    running = False
                    self.changes[mono] = 0.0
        for before, after in zip(plc_rows, plc_rows[1:]):
            if before["mono"] - before["raw"] != after["mono"] - after["raw"]:
                gaps.append((before["mono"], after["mono"]))
        combined = []
        for low, high in sorted(gaps):
            if combined and low <= combined[-1][1]:
                combined[-1] = (max(combined[-1][0], low), min(combined[-1][1], high))
            else:
                combined.append((low, high))
        for low, high in combined:
            times = sorted(self.changes)
            index = bisect.bisect_right(times, high) - 1
            state = self.changes[times[index]] if index >= 0 else None
            self.changes[low] = None
            self.changes[high] = state
        self.times = sorted(self.changes)

    def arrival(self, start: datetime, distance: float) -> datetime | None:
        index = bisect.bisect_right(self.times, start) - 1
        if index < 0:
            return None
        current = start
        while index < len(self.times):
            speed = self.changes[self.times[index]]
            if speed is None:
                return None
            end = self.times[index + 1] if index + 1 < len(self.times) else None
            if speed > 0:
                duration = distance / speed
                if end is None or duration <= (end - current).total_seconds():
                    return current + timedelta(seconds=duration)
                distance -= (end - current).total_seconds() * speed
            if end is None:
                return None
            current = end
            index += 1
        return None

    def departure(self, end, distance):
        current = end
        index = bisect.bisect_left(self.times, current) - 1
        while index >= 0:
            speed = self.changes[self.times[index]]
            if speed is None:
                return None
            start = self.times[index]
            if speed > 0:
                duration = distance / speed
                if duration <= (current - start).total_seconds():
                    return current - timedelta(seconds=duration)
                distance -= (current - start).total_seconds() * speed
            current = start
            index -= 1
        return None

    def distance_between(self, start, end):
        index = bisect.bisect_right(self.times, start) - 1
        if index < 0 or end < start:
            return None
        current, distance = start, 0.0
        while current < end:
            speed = self.changes[self.times[index]]
            if speed is None:
                return None
            boundary = min(end, self.times[index + 1]) if index + 1 < len(self.times) else end
            distance += (boundary - current).total_seconds() * speed
            current = boundary
            index += 1
        return distance

    def arrival_bounds(self, start, distance):
        index = bisect.bisect_right(self.times, start) - 1
        if index < 0:
            return None, None
        current = start
        earliest = None
        while index < len(self.times):
            speed = self.changes[self.times[index]]
            end = self.times[index + 1] if index + 1 < len(self.times) else None
            if speed is None:
                earliest = current if earliest is None else earliest
            elif speed > 0:
                duration = distance / speed
                if end is None or duration <= (end - current).total_seconds():
                    arrival = current + timedelta(seconds=duration)
                    return earliest or arrival, arrival
                distance -= (end - current).total_seconds() * speed
            if end is None:
                return earliest, None
            current = end
            index += 1
        return earliest, None


def normalize_inputs(tubes, motion):
    for number, tube in sorted(tubes.items()):
        tube.physical_number = number
        previous = tubes.get(number - 1)
        if previous is None or previous.physical_number not in tubes:
            continue
        first = tubes[previous.physical_number]
        if first.s1_lower is None or tube.s1_upper is None:
            continue
        distance = motion.distance_between(first.s1_lower, tube.s1_upper)
        if distance is not None and 0 < distance < 0.25:
            tube.physical_number = first.number
    sizes = Counter(t.physical_number for t in tubes.values())
    for tube in tubes.values():
        tube.duplicate_input = sizes[tube.physical_number] > 1


def passage_evidence(tubes, plc_rows, motion, print_times, numbered):
    windows = {}
    forced = set()
    events = defaultdict(list)
    for event in numbered:
        events[event.number].append(event)
    for number, tube in tubes.items():
        if tube.physical_number != number or tube.s1_lower is None or tube.s1_upper is None:
            continue
        lower, upper = tube.s1_lower, tube.s1_upper
        if number in print_times:
            lower = max(lower, print_times[number] - timedelta(milliseconds=1))
            upper = min(upper, print_times[number] + timedelta(milliseconds=1))
        low = motion.arrival_bounds(lower, 0.7)[0]
        high = motion.arrival_bounds(upper, 0.7)[1]
        if low is None or high is None:
            continue
        windows[number] = (low, high)
        push_low = motion.arrival_bounds(lower, 0.6)[0]
        push_high = motion.arrival_bounds(upper, 0.6)[1]
        if push_low is None or push_high is None:
            continue
        deadline_low, deadline_high = push_low - timedelta(seconds=0.2), push_high - timedelta(seconds=0.2)
        early = sorted((e for e in events[number] if e.mono <= deadline_low), key=lambda e: e.mono)
        uncertain_bad = any(deadline_low < e.mono <= deadline_high and e.verdict == "брак" for e in events[number])
        if early and early[-1].verdict == "годен" and not uncertain_bad:
            forced.add(number)
    ordered = sorted((low, high, n) for n, (low, high) in windows.items())
    starts = [w[0] for w in ordered]
    widest = max((high - low for low, high, _ in ordered), default=timedelta())
    edges = defaultdict(set)
    observations = []
    old_inputs_until = motion.arrival_bounds(plc_rows[0]["mono"], 0.7)[1] if plc_rows and plc_rows[0]["n_in"] else None
    has_old_inputs = bool(plc_rows and plc_rows[0]["n_in"])
    for before, after in zip(plc_rows, plc_rows[1:]):
        check_budget()
        count = after["n_out"] - before["n_out"]
        if count <= 0:
            continue
        left = bisect.bisect_left(starts, before["mono"] - widest)
        right = bisect.bisect_right(starts, after["mono"])
        candidates = {n for low, high, n in ordered[left:right]
                      if high > before["mono"] and low < after["mono"]}
        index = len(observations)
        unknown = has_old_inputs and (old_inputs_until is None or before["mono"] < old_inputs_until)
        observations.append([before["mono"], after["mono"], count, candidates, unknown])
        for number in candidates:
            edges[number].add(index)
    queue = deque(("output", index) for index in range(len(observations)))
    queue.extend(("input", n) for n in forced)
    passed = {}

    def assign(number, index):
        low, high = windows[number]
        observation = observations[index]
        passed[number] = (max(low, observation[0]), min(high, observation[1]))
        for other in list(edges[number]):
            observations[other][3].discard(number)
            queue.append(("output", other))
        edges[number].clear()
        observation[2] -= 1
        if observation[2] == 0:
            for other_number in list(observation[3]):
                edges[other_number].discard(index)
                if other_number in forced:
                    queue.append(("input", other_number))
            observation[3].clear()

    while queue:
        check_budget()
        kind, value = queue.popleft()
        if kind == "input":
            if value not in passed and len(edges[value]) == 1:
                index = next(iter(edges[value]))
                if observations[index][2] > 0:
                    assign(value, index)
        else:
            observation = observations[value]
            if not observation[4] and observation[2] > 0 and len(observation[3]) == observation[2]:
                for number in sorted(observation[3]):
                    assign(number, value)
    covered = {n for n, (low, high) in windows.items()
               if plc_rows and plc_rows[0]["mono"] <= low <= high <= plc_rows[-1]["mono"]}
    absent = {n for n in covered if n not in passed and not edges[n]}
    contradictions = absent & forced
    absent -= contradictions
    return passed, absent, contradictions


def print_ownership(tubes, marks, motion):
    epochs = defaultdict(list)
    for number, tube in tubes.items():
        if tube.physical_number != number:
            continue
        if None in (tube.s1_lower, tube.s1_upper, tube.s1_lower_raw, tube.s1_upper_raw):
            continue
        epoch = tube.s1_upper - tube.s1_upper_raw
        if tube.s1_lower - tube.s1_lower_raw != epoch:
            continue
        epochs[epoch].append((tube.s1_lower, tube.s1_upper, number))
    indices = {}
    for epoch, windows in epochs.items():
        windows.sort()
        indices[epoch] = (windows, [w[0] for w in windows], max(w[1] - w[0] for w in windows))
    candidates = defaultdict(list)
    anchors = {}
    ambiguous = set()
    grouped_marks = defaultdict(list)
    for raw, mono, code in marks:
        check_budget()
        index = indices.get(mono - raw)
        if index is None:
            continue
        arrival = motion.arrival(mono - timedelta(seconds=0.3), 0.4)
        if arrival is None:
            continue
        windows, starts, widest = index
        left = bisect.bisect_left(starts, arrival - widest)
        right = bisect.bisect_left(starts, arrival)
        owners = [number for low, high, number in windows[left:right] if arrival <= high]
        if len(owners) == 1:
            candidates[owners[0]].append(code)
            anchors[owners[0]] = arrival
        else:
            ambiguous.update(owners)
            groups = {tubes[number].input_group for number in owners}
            if len(groups) == 1:
                grouped_marks[next(iter(groups))].append((arrival, code, tuple(owners)))
    for group, observations in grouped_marks.items():
        numbers = sorted(number for number, tube in tubes.items() if tube.input_group == group and tube.physical_number == number)
        if len(observations) != len(numbers) or any(set(owners) != set(numbers) for _, _, owners in observations):
            continue
        observations.sort()
        distances = [motion.distance_between(a[0], b[0]) for a, b in zip(observations, observations[1:])]
        if any(distance is None or distance < 0.25 - 0.001 for distance in distances):
            continue
        for number, (arrival, code, _owners) in zip(numbers, observations):
            candidates[number] = [code]
            anchors[number] = arrival
            ambiguous.discard(number)
    codes = {number: values[0] for number, values in candidates.items()
             if number not in ambiguous and len(values) == 1 and values[0]}
    return codes, {number: anchors[number] for number in codes}


def autonomous_result(folder, tubes, plc_rows, numbered, boxes, labels, marks, sends,
                      counts, motion, passages, legacy_rows, resolved_codes, counter_bounds, print_times, absent, contradictions):
    issues = []
    group_sizes = Counter(tube.input_group for tube in tubes.values())
    results = []
    keys = set()

    def issue(code, entity, detail):
        item = {"code": code, "entity": str(entity), "detail": detail}
        if item not in issues:
            issues.append(item)

    def emit(kind, code, box, numbers, reason):
        identity = (kind, code) if kind == "В" else (kind, code, box, tuple(numbers))
        if identity in keys:
            return
        keys.add(identity)
        available = [tubes[number] for number in numbers if number in tubes]
        results.append({
            "вид": kind,
            "время": " ".join(show_time(print_times[n] - (tubes[n].s1_upper - tubes[n].s1_upper_raw))
                               for n in numbers) if numbers and all(n in print_times for n in numbers) else "",
            "туба": " ".join(str(number) for number in numbers),
            "код": code, "короб": box, "причина": reason,
            "_sort": available[0].s1_mono if available else datetime.min,
        })

    if not plc_rows:
        issue("NO_PLC_BASELINE", "plc.csv", "Нет исходного состояния ПЛК")
    epochs = {}
    for name in ("station.log", "laser.log", "plc.csv"):
        clock = MonotonicClock()
        with (folder / name).open(encoding="utf-8-sig") as stream:
            for line in stream:
                if len(line) >= 23 and line[0].isdigit():
                    clock.convert(parse_time(line[:23]))
        epochs[name] = clock.offset.days
    if len(set(epochs.values())) > 1:
        issue("CLOCK_EPOCH_MISMATCH", "journals", "Перевод часов виден не во всех журналах")
    if any(epochs.values()):
        issue("CLOCK_CORRECTION_INTERVAL", "journals", "Длительность через перевод часов неизвестна")

    motion_consistent = True
    for row in plc_rows:
        index = bisect.bisect_right(motion.times, row["mono"]) - 1
        if index < 0:
            continue
        velocity = motion.changes[motion.times[index]]
        expected = row["speed"] / 60 if row["belt"] else 0.0
        if velocity is not None and abs(velocity - expected) > 1e-9:
            issue("MOTION_LOG_MISMATCH", show_time(row["raw"]), "Состояние ленты ПЛК расходится с журналом управления")
            motion_consistent = False
            break

    assignments = defaultdict(list)
    for event in numbered:
        if event.verdict == "годен":
            assignments[event.code].append(event.number)
    duplicates = {row["код"] for row in legacy_rows if row["вид"] == "Г"}
    for row in legacy_rows:
        if row["вид"] == "Г":
            copy = dict(row)
            number = int(copy["туба"]) if copy["туба"].isdigit() else None
            copy["время"] = (show_time(print_times[number] - (tubes[number].s1_upper - tubes[number].s1_upper_raw))
                             if number in print_times else "")
            results.append(copy)
    auto = sorted((b for b in boxes if b.kind == "авто" and b.sscc in labels), key=lambda b: b.mono)
    label_times = [box.mono for box in auto]
    samples = [r["mono"] for r in plc_rows]
    absent = set(absent)
    if contradictions:
        issue("PHYSICAL_CONTRADICTION", "plc.csv", "Вердикт годен и наблюдаемые проходы S2 несовместимы")
    actual_box = {}
    actual_code = {}
    proven_unprinted = set()
    proven_prints = defaultdict(list)
    mark_times_for_coverage = [mark[1] for mark in marks]
    stable_counts = [sample for sample in counts
                     if bisect.bisect_right(mark_times_for_coverage, sample[0])
                     == bisect.bisect_right(mark_times_for_coverage, counter_bounds[sample[0]])]
    complete_print_intervals = [(before[0], counter_bounds[after[0]])
                                for before, after in zip(stable_counts, stable_counts[1:])
                                if after[1] - before[1] == bisect.bisect_right(mark_times_for_coverage, after[0])
                                - bisect.bisect_right(mark_times_for_coverage, before[0])]
    print_arrivals = sorted(arrival for _raw, mono, _code in marks
                            if (arrival := motion.arrival(mono - timedelta(seconds=0.3), 0.4)) is not None)

    ghosts = {n for n, tube in tubes.items() if tube.physical_number != n}
    for number, tube in sorted(tubes.items()):
        check_budget()
        if number in ghosts:
            continue
        if tube.s1_lower is None or tube.s1_upper is None:
            issue("UNKNOWN_INPUT", number, "Нет интервала S1")
            continue
        code = resolved_codes.get(number, "NoRead")
        if code != "NoRead":
            proven_prints[code].append(number)
        else:
            observed = {event.code for event in numbered
                        if event.number == number and event.verdict == "годен"}
            if tube.duplicate_input:
                observed |= {event.code for event in numbered
                             if event.number in tubes and tubes[event.number].physical_number == number and event.verdict == "годен"}
            if len(observed) == 1:
                code = next(iter(observed))
        actual_code[number] = code
        if code == "NoRead":
            earliest = motion.departure(tube.s1_lower, 0.4)
            latest = motion.departure(tube.s1_upper, 0.4)
            possible_marks = (bisect.bisect_right(print_arrivals, tube.s1_upper)
                              - bisect.bisect_right(print_arrivals, tube.s1_lower))
            no_print = (earliest is not None and latest is not None and possible_marks == 0
                        and any(start <= earliest + timedelta(seconds=0.3)
                                and latest + timedelta(seconds=0.3) <= end
                                for start, end in complete_print_intervals))
            if no_print:
                proven_unprinted.add(number)
            else:
                issue("UNKNOWN_PRINT_IDENTITY", number, "Не доказано, какой код нанесён или что туба немаркированная")
        if number in passages:
            start, end = passages[number]
            index = bisect.bisect_right(label_times, end)
            if index < len(auto) and (index == 0 or start >= auto[index - 1].mono):
                actual_box[number] = auto[index].sscc
            else:
                issue("BOX_BOUNDARY_UNCERTAIN", number, "S2 пересекает границу этикетки или короб неполный")
        elif number not in absent:
            issue("PASSAGE_UNCERTAIN", number, "Недостаточно данных для прохода или отсутствия S2")

    for code, numbers in proven_prints.items():
        if len(numbers) > 1:
            target = " ".join(b.sscc for b in boxes if code in b.codes)
            emit("В", code, target, numbers, "подтверждены отдельные нанесения одного кода")

    unresolved_outputs = bool(plc_rows and plc_rows[-1]["n_out"] - plc_rows[0]["n_out"] != len(passages))
    unresolved_passages = any(n not in passages and n not in absent for n in tubes if n not in ghosts)
    for box in auto:
        check_budget()
        contents = {code for number, code in actual_code.items() if actual_box.get(number) == box.sscc}
        unknown_contents = "NoRead" in contents
        for code in box.codes:
            if code in duplicates or code in contents:
                continue
            numbers = set(assignments.get(code, []))
            physical = {tubes[n].physical_number if n in tubes else n for n in numbers}
            if physical and all(n in absent or (n in actual_box and actual_box[n] != box.sscc) for n in physical):
                if unknown_contents or unresolved_outputs or unresolved_passages:
                    issue("MISSING_CODE_UNCERTAIN", code, "Не исключён несопоставленный проход или неизвестный код в коробе")
                else:
                    emit("А", code, box.sscc, sorted(physical), "записанная туба отсутствует в физическом коробе")
            else:
                issue("BOX_CODE_UNRESOLVED", code, "Запись короба не сопоставлена с физической тубой")
        for number, destination in actual_box.items():
            if destination != box.sscc:
                continue
            code = actual_code[number]
            if code not in box.codes and (code != "NoRead" or number in proven_unprinted):
                emit("Б", code, box.sscc, [number], "туба прошла S2 в этот короб без соответствующей записи")

    if plc_rows:
        observed_out = plc_rows[-1]["n_out"] - plc_rows[0]["n_out"]
        if observed_out != len(passages):
            issue("UNASSIGNED_OUTPUTS", "plc.csv", "Не все приросты S2 однозначно сопоставлены тубам")
    for box in boxes:
        if box.kind == "ручной":
            issue("MANUAL_CONTENT_UNOBSERVED", box.sscc, "Проверена запись кодов, но физическое наполнение ручного короба не наблюдается")
        if box.kind == "неполный" or (box.kind == "авто" and box.sscc not in labels):
            issue("UNSEALED_BOX", box.sscc, "Нет подтверждённой границы физического короба")

    send_events = sorted((mono, code) for code, values in sends.items() for _raw, mono in values)
    send_times = [event[0] for event in send_events]
    mark_times = [mark[1] for mark in marks]
    def offset_range(sample):
        return (sample[1] - bisect.bisect_right(mark_times, sample[0]),
                sample[1] - bisect.bisect_right(mark_times, counter_bounds[sample[0]]))

    offset = offset_range(counts[0]) if counts else (0, 0)
    for before, after in zip(counts, counts[1:]):
        current = offset_range(after)
        intersection = (max(offset[0], current[0]), min(offset[1], current[1]))
        if intersection[0] <= intersection[1]:
            offset = intersection
            continue
        missing_low, missing_high = current[0] - offset[1], current[1] - offset[0]
        offset = current
        if missing_high < 0:
            issue("MARK_COUNTER_DISCONTINUITY", str(after[0]), "Счётчик нанесений сброшен или сообщения противоречат ему")
            continue
        if missing_low != missing_high or missing_low <= 0:
            issue("COUNTER_WINDOW_AMBIGUOUS", str(after[0]), "Нанесение попало в интервал запроса счётчика")
            continue
        missing = missing_low
        begin = max(0, bisect.bisect_right(send_times, before[0]) - 1)
        end = bisect.bisect_right(send_times, after[0])
        pending = []
        for index in range(begin, end):
            sent_at, code = send_events[index]
            limit = min(send_times[index + 1] if index + 1 < len(send_times) else after[0], after[0])
            completed = bisect.bisect_right(mark_times, sent_at)
            if completed >= len(marks) or mark_times[completed] > limit:
                pending.append((sent_at, code))
        if len(pending) != missing or any(t <= before[0] for t, _ in pending):
            issue("LOST_PRINT_AMBIGUOUS", str(after[0]), "Число потерянных нанесений не определяет их коды")
            continue
        for sent_at, code in pending:
            resent = [mono for _raw, mono in sends.get(code, []) if mono > after[0]]
            later = [mark for mark in marks if mark[2] == code
                     and any(after[0] < sent_at < mark[1] for sent_at in resent)]
            if not later:
                issue("LOST_PRINT_IDENTITY", code, "Нанесение доказано счётчиком, но номер тубы неизвестен")
                continue
            prior = next((row for row in legacy_rows if row["вид"] == "В" and row["код"] == code), None)
            prior_numbers = [int(n) for n in prior["туба"].split()] if prior else []
            if prior and all(n in tubes and group_sizes[tubes[n].input_group] == 1 for n in prior_numbers):
                emit("В", code, prior["короб"], prior_numbers, prior["причина"])
            elif len(proven_prints[code]) < 2:
                emit("В", code, " ".join(b.sscc for b in boxes if code in b.codes), [],
                     "повторное нанесение доказано; номера туб не определены")
                issue("DUPLICATE_IDENTITY_UNCERTAIN", code, "Дубль кода доказан, физические номера не восстановлены")
    if len(counts) < 2 and any("возвращён в очередь" in line for line in (folder / "station.log").read_text(encoding="utf-8-sig").splitlines()):
        issue("MARK_COUNTER_COVERAGE", "laser.log", "Не хватает замеров для проверки возврата кодов после аварии")
    if not auto and any(number not in absent for number in tubes if number not in ghosts):
        issue("NO_BOX_BOUNDARIES", "boxes.csv", "Физическое наполнение коробов не проверено")
    if len(set(epochs.values())) > 1 or not motion_consistent or contradictions:
        results = [row for row in results if row["вид"] == "Г"]
    results.sort(key=lambda row: (row["_sort"], row["вид"], row["туба"]))
    referenced = {int(n) for row in results for n in row["туба"].split() if n.isdigit()}
    referenced |= {int(item["entity"]) for item in issues if item["entity"].isdigit()}
    intervals = {str(n): {"lower": show_time(t.s1_lower_raw),
                         "upper": show_time(t.s1_upper_raw),
                         "clock_discontinuity": t.s1_upper - t.s1_upper_raw != t.s1_lower - t.s1_lower_raw}
                 for n, t in tubes.items() if n in referenced and t.s1_lower_raw and t.s1_upper_raw}
    status = "INCOMPLETE" if issues else "ANOMALIES" if results else "CLEAR"
    return results, {"schema_version": 1, "status": status, "issues": issues,
                     "findings": [{k: v for k, v in row.items() if not k.startswith("_")} for row in results],
                     "s1_intervals": intervals,
                     "known_print_owners": {code: proven_prints[code] for code in {row["код"] for row in results if row["вид"] == "В"}},
                     "time_basis": "mark_end_and_belt_motion; otherwise blank with PLC interval",
                     "scope": "digital_logs_only", "manual_removals_observable": False}


def input_fingerprint(folder):
    result = {}
    for name in ("station.log", "plc.csv", "laser.log", "boxes.csv"):
        path = folder / name
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError(f"{name} превышает предел 64 МБ")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result[name] = digest.hexdigest()
        if name.endswith(".log"):
            with path.open(encoding="utf-8-sig") as stream:
                for line in stream:
                    check_budget()
                    if line.strip() and len(line.rstrip("\r\n")) < 25:
                        raise ValueError(f"оборванная строка в {name}")
    return result


def strict_failure(error):
    print(json.dumps({"schema_version": 1, "status": "INPUT_ERROR", "issues": [
        {"code": "INPUT_VALIDATION", "detail": str(error)}]}, ensure_ascii=False), file=sys.stderr)
    return 2


def analyze(args, folder, source_folder=None, fingerprint=None) -> int:
    try:
        folder = locate_data(folder)
        tubes, plc_rows, plc_samples, out_changes = read_plc(folder)
        numbered, repeats, labels, stops, starts, requeues = read_station(folder)
        boxes = read_boxes(folder, labels)
        sends, completed_marks, mark_counts, counter_bounds = read_laser(folder)
        motion = BeltMotion(folder, plc_rows)
        normalize_inputs(tubes, motion)
        resolved_codes, print_times = print_ownership(tubes, completed_marks, motion)
        passed_numbers, absent_numbers, contradictions = passage_evidence(tubes, plc_rows, motion, print_times, numbered)
    except (OSError, ValueError, KeyError, csv.Error, UnicodeError, OverflowError) as error:
        if args.strict:
            return strict_failure(error)
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
        if not tube or not tube.pushed or number in passed_numbers or tube.s1_raw is None or tube.s1_mono is None:
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

    # А: два номера на участке короче минимального расстояния между тубами.
    for number, tube in sorted(tubes.items()):
        check_budget()
        following = tubes.get(number + 1)
        if (
            not tube.pushed
            or number in passed_numbers
            or tube.s1_raw is None
            or tube.s1_mono is None
            or following is None
            or following.physical_number != tube.physical_number
            or tube.physical_number != number
            or not tube.duplicate_input
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
    send_events = sorted((mono, raw, code) for code, values in sends.items() for raw, mono in values)
    send_times = [event[0] for event in send_events]
    completed_times = [event[1] for event in completed_marks]
    used_duplicates = set()
    for _raw, requeue_mono, code, _line in requeues:
        check_budget()
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
        if count_after[1] - count_before[1] - observed != 1:
            continue
        pending = []
        begin = max(0, bisect.bisect_right(send_times, count_before[0]) - 1)
        end = bisect.bisect_right(send_times, count_after[0])
        for index in range(begin, end):
            sent_at, _sent_raw, sent_code = send_events[index]
            next_send = send_times[index + 1] if index + 1 < len(send_times) else count_after[0]
            limit = min(next_send, count_after[0])
            completion = bisect.bisect_right(completed_times, sent_at)
            if completion >= len(completed_marks) or completed_times[completion] > limit:
                pending.append((sent_at, sent_code))
        if pending != [(first_send[1], code)]:
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
        chain = [mark] + [item for item in completed_marks if mark[1] < item[1] < first_send[1]]
        uninterrupted = True
        for completed in chain:
            index = bisect.bisect_right(send_times, completed[1])
            earliest_next_tube = motion.arrival(completed[1] - timedelta(seconds=0.3), 0.25)
            if (index >= len(send_times) or earliest_next_tube is None
                    or send_times[index] >= earliest_next_tube):
                uninterrupted = False
                break
        if not uninterrupted:
            continue
        first_number = base_event.number + completed_between + 1
        if first_number >= second_event.number:
            continue
        identity = (code, first_number, second_event.number)
        if identity in used_duplicates:
            continue
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
            or resolved_codes.get(second_tube.number, "NoRead") != code
        ):
            continue
        used_duplicates.add(identity)
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
        check_budget()
        cumulative += len(box.codes)
        missing += missing_by_box[box.sscc]
        expected = cumulative - missing + extra
        index = bisect.bisect_right(sample_times, box.mono) - 1
        baseline = plc_rows[0]["n_out"] if plc_rows else 0
        lower = plc_samples[index][1] - baseline if index >= 0 else 0
        surplus = lower - expected
        if surplus > 0:
            candidates = [
                e
                for e in unread
                if e.number not in used_unread
                and (previous_label is None or e.mono > previous_label)
                and e.mono < box.mono
            ]
            candidates = list({event.number: event for event in candidates}.values())
            if len(candidates) > surplus:
                candidates = [event for event in candidates if event.number in passed_numbers]
            if len(candidates) > surplus:
                candidates = []
            for event in candidates:
                tube = tubes.get(event.number)
                if tube is None or tube.s1_raw is None or tube.s1_mono is None:
                    continue
                code = resolved_codes.get(tube.number, "NoRead")
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
        tube = tubes.get(event.number) if event else None
        reason = (
            "ручной скан не проверил глобальный дубль"
            if any(box.kind == "ручной" for box in ordered)
            else "код записан в два короба"
        )
        add_row(
            "Г",
            show_time(tube.s1_raw) if tube and tube.s1_raw else "",
            str(event.number) if event else "",
            code,
            " ".join(box.sscc for box in ordered),
            reason,
            tube.s1_mono if tube and tube.s1_mono else ordered[0].mono,
        )

    duplicated_codes = {row["код"] for row in rows if row["вид"] == "Г"}
    rows = [row for row in rows if row["вид"] != "А" or row["код"] not in duplicated_codes]
    report = None
    if args.strict:
        try:
            rows, report = autonomous_result(folder, tubes, plc_rows, numbered, boxes, labels,
                                             completed_marks, sends, mark_counts, motion, passed_numbers, rows, resolved_codes, counter_bounds, print_times, absent_numbers, contradictions)
            if input_fingerprint(source_folder or folder) != fingerprint:
                raise ValueError("входные файлы изменились во время анализа; нужен согласованный снимок")
            report["inputs_sha256"] = fingerprint
        except (OSError, ValueError, KeyError, csv.Error, UnicodeError, OverflowError) as error:
            return strict_failure(error)
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
    if report is not None:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return {"CLEAR": 0, "ANOMALIES": 10, "INCOMPLETE": 20}[report["status"]]
    return 0


class CommandLineParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def run_analysis():
    parser = CommandLineParser()
    parser.add_argument("folder", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--strict", dest="strict", action="store_true")
    mode.add_argument("--compat", dest="strict", action="store_false")
    parser.set_defaults(strict=True)
    try:
        args = parser.parse_args()
    except ValueError as error:
        return strict_failure(error)
    if not args.strict:
        return analyze(args, args.folder)
    try:
        source = locate_data(args.folder)
        baseline = input_fingerprint(source)
        with tempfile.TemporaryDirectory(prefix="smena-snapshot-") as directory:
            snapshot = Path(directory)
            for name in baseline:
                shutil.copyfile(source / name, snapshot / name)
            if input_fingerprint(snapshot) != baseline:
                raise ValueError("входные файлы изменились при создании снимка")
            return analyze(args, snapshot, source, baseline)
    except (OSError, ValueError, KeyError, csv.Error, UnicodeError, OverflowError) as error:
        return strict_failure(error)


def main() -> int:
    global _deadline
    _deadline = time.monotonic() + 110
    try:
        return run_analysis()
    except TimeoutError as error:
        if "--compat" not in sys.argv:
            return strict_failure(error)
        print(f"Ошибка анализа: {error}", file=sys.stderr)
        return 1
    finally:
        _deadline = None


if __name__ == "__main__":
    raise SystemExit(main())

