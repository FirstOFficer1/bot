"""
Import schedule from Excel (rsp2s.xlsx) into s.db.

Usage:
    python import_excel.py                          # default path
    python import_excel.py C:/path/to/file.xlsx     # custom path

The script clears the schedule table and re-imports from Excel.
"""

import re
import sqlite3
import sys

import openpyxl

DEFAULT_EXCEL = r"C:\Users\kiril\Downloads\rsp2s.xlsx"
DB_PATH = "s.db"
HEADER_ROW = 8  # 1-indexed in Excel

DAYS = {
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
}


def _reconstruct_spaced_line(line: str) -> str:
    """
    Восстанавливает слова из разрежённого текста.

    В таких строках символы разделены 2-4 пробелами, а слова — 4+ пробелами:
      'Ф   и  л   о   с   о   ф   и   я,     С   т   е   п   а   н   о  в'
    →  'Философия, Степанов'

    После восстановления добавляет пробел после точки в инициалах:
      'А.Г.' → 'А. Г.'
    """
    if not re.match(r"^\S\s{2,}\S", line):
        return line  # обычный текст, не трогаем

    # 4+ пробела = граница слова; 1-3 пробела = разделитель символов
    line = re.sub(r" {4,}", "\x00", line)   # маркируем границы слов
    line = re.sub(r" {1,3}", "", line)       # убираем разделители символов
    line = line.replace("\x00", " ")         # восстанавливаем пробелы между словами

    # Вставляем пробел после точки в инициалах: 'А.Г.' → 'А. Г.'
    line = re.sub(r"([А-ЯЁ]\.)([А-ЯЁа-яё])", r"\1 \2", line)

    return line.strip()


_JUNK_CHARS = re.compile(
    r"[﻿​‌‍⁠­   ]"
)


def clean_text(text: str) -> str:
    """Normalize whitespace, strip junk Unicode chars, reconstruct spaced-out characters."""
    if not text:
        return ""
    text = str(text).strip()
    text = _JUNK_CHARS.sub(lambda m: " " if m.group() in (" ", " ", " ") else "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_reconstruct_spaced_line(ln) for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"  +", " ", text)
    return text.strip()


# Имя преподавателя: «Фамилия И. О.»
_TEACHER_RE = re.compile(r"^([А-ЯЁ][а-яё]{1,}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.?)")

# Временные метки в строке преподавателя: «до 20.03», «с 23.03», «07.02», «7.02.»
_DATE_RE = re.compile(
    r"((?:до|с|от|по)\s+\d{1,2}\.\d{2}(?:\.\d{2,4})?"
    r"|\d{1,2}\.\d{2}(?:\.\d{2,4})?\.?)",
    re.IGNORECASE,
)

# Тип занятия в хвосте строки преподавателя
_CT_TAIL_RE = re.compile(r"\b(лк|пр|лб|лекция)\b", re.IGNORECASE)


def _parse_teacher(raw: str) -> tuple[str, str]:
    """
    Разделяет строку вида «Иванов И. О. до 20.03» на:
      (чистое_имя, период)  →  ('Иванов И. О.', 'до 20.03')
    Отрезает типы пар и аудитории, если они прилипли к имени.
    """
    raw = raw.strip()
    m = _TEACHER_RE.match(raw)
    if not m:
        return raw, ""
    teacher = m.group(1).strip()
    remainder = raw[m.end():].strip()
    date_m = _DATE_RE.search(remainder)
    return teacher, date_m.group(1).strip() if date_m else ""


def _clean_teacher(raw: str) -> str:
    return _parse_teacher(raw)[0]


def parse_direction_course(header: str) -> tuple[str, int]:
    """
    Parse header like 'Инжиниринг ИС \\n1 курс' → ('Инжиниринг ИС', 1).
    Returns ('', 0) if course number not found.
    """
    text = re.sub(r"\s+", " ", header.replace("\n", " ")).strip()
    m = re.search(r"(\d+)\s*курс", text, re.IGNORECASE)
    if not m:
        return "", 0
    course = int(m.group(1))
    direction = re.sub(r",?\s*\d+\s*курс.*", "", text, flags=re.IGNORECASE).strip()
    return direction, course


def parse_cell(cell_text: str) -> list[dict]:
    """
    Parse one schedule cell into records.

    Cell format examples:
      "Геометрия, Абруков Д. А. пр 408"
      "* Психология ..., Вишневская М. Н. лк 400\\n** Основы ..., Григорьев Ю. В. лб 425"

    A new week-entry starts only when a line begins with * or **.
    Other newlines within the same entry are just line-wrapping.

    Returns list of dicts: {subject, teacher, room, week, class_type, date_range}
    week: '' | 'чёт' | 'нечет'  class_type: '' | 'лк' | 'пр' | 'лб' | 'лекция'
    """
    text = clean_text(cell_text)
    if not text:
        return []

    # Group lines into logical entries.
    # A new entry starts when:
    #   a) a line begins with * or ** (explicit week markers), OR
    #   b) a line starts with an uppercase Cyrillic letter AND the current
    #      accumulated lines already contain a class-type keyword (лк/пр/лб),
    #      which means the previous entry is complete and a new subject begins.
    _CLASS_TYPE_RE = re.compile(r'\b(лк|пр|лб|лекция)\b', re.IGNORECASE)
    raw_lines = text.split("\n")
    entries_raw: list[str] = []
    current: list[str] = []

    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("*") and current:
            entries_raw.append(" ".join(current))
            current = [stripped]
        elif (
            current
            and re.match(r'[А-ЯЁ]', stripped)
            and _CLASS_TYPE_RE.search(" ".join(current))
        ):
            # Current entry already has a class type — new subject starting
            entries_raw.append(" ".join(current))
            current = [stripped]
        else:
            current.append(stripped)

    if current:
        entries_raw.append(" ".join(current))

    results = []
    for entry in entries_raw:
        week = ""
        if entry.startswith("**"):
            week = "чёт"   # ** = чётная неделя
            entry = entry[2:].strip()
        elif entry.startswith("*"):
            week = "нечет"  # *  = нечётная неделя
            entry = entry[1:].strip()
        # Убираем оставшиеся лидирующие * (например из "** *" в ячейке)
        if entry.startswith("*"):
            entry = entry.lstrip("*").strip()

        # Normalize internal spaces left after joining lines
        entry = re.sub(r"  +", " ", entry).strip()

        # Паттерн 1: "Предмет, Преподаватель лк/пр/лб/лекция Аудитория"
        # Ищем запятую перед фамилией (заглавная + строчные), а не перед первым словом,
        # чтобы корректно обрабатывать предметы с запятой в названии:
        #   "Здания, сооружения..., Иванов Л. Н. лк 414" → subject="Здания, сооружения..."
        # Без IGNORECASE: [А-ЯЁ] соответствует только заглавным буквам,
        # поэтому «, сооружения» не матчится, а «, Иванов» — матчится.
        # Типы пар (лк/пр/лб) в Excel всегда строчные, IGNORECASE не нужен.
        m = re.search(
            r",\s*([А-ЯЁ][а-яё]{2,}.*?)\s+(лк|пр|лб|лекция)\s+(.+)$",
            entry,
        )
        if m:
            subject = entry[:m.start()].strip().rstrip(",")
            class_type = m.group(2).lower()
            teacher, date_range = _parse_teacher(m.group(1))
            room_raw = m.group(3).strip()

            # Встроенные маркеры недели (* или **) внутри аудитории:
            # "330 ** пр 409"  →  нечёт: room=330, чёт: room=409
            # "** пр 400"      →  чёт: room=400  (нечёт без аудитории — пропускаем)
            if re.search(r"\*", room_raw):
                segments = re.split(r"\s*(\*{1,2})\s*", room_raw)
                first_room = segments[0].strip()
                first_marker = segments[1] if len(segments) > 1 else ""
                if week:
                    first_week = week
                else:
                    first_week = "нечет" if first_marker == "**" else "чёт" if first_marker == "*" else ""
                if first_room or week:
                    results.append({
                        "subject": subject, "teacher": teacher,
                        "room": first_room, "week": first_week,
                        "class_type": class_type, "date_range": date_range,
                    })
                i = 1
                while i < len(segments) - 1:
                    marker = segments[i]
                    rest   = segments[i + 1].strip() if i + 1 < len(segments) else ""
                    sub_week = "чёт" if marker == "**" else "нечет"
                    mr = re.match(r"(?:лк|пр|лб|лекция)\s*(.*)", rest, re.IGNORECASE)
                    sub_room = mr.group(1).strip() if mr else rest
                    results.append({
                        "subject": subject, "teacher": teacher,
                        "room": sub_room, "week": sub_week,
                        "class_type": class_type, "date_range": date_range,
                    })
                    i += 2
            else:
                results.append({
                    "subject": subject, "teacher": teacher,
                    "room": room_raw, "week": week,
                    "class_type": class_type, "date_range": date_range,
                })
            continue

        # Паттерн 2: "Предмет, Преподаватель (Аудитория)"  — без маркера типа пары
        mp = re.match(
            r"^(.+?),\s*(.+?)\s+\((.+?)\)\s*$",
            entry,
        )
        if mp:
            raw_t = mp.group(2)
            ct_m2 = _CT_TAIL_RE.search(raw_t)
            teacher2, date_range2 = _parse_teacher(raw_t)
            results.append({
                "subject": mp.group(1).strip(),
                "teacher": teacher2,
                "room": mp.group(3).strip(),
                "week": week,
                "class_type": ct_m2.group(1).lower() if ct_m2 else "",
                "date_range": date_range2,
            })
            continue

        # Паттерн 3: деление по ПОСЛЕДНЕЙ запятой → (предмет, преподаватель [аудитория])
        # Применяем только если часть после последней запятой похожа на фамилию (Заглавная+строчная)
        if "," in entry:
            last_idx = entry.rfind(",")
            teacher_room_cand = entry[last_idx + 1:].strip()
            if re.match(r"[А-ЯЁ][а-яё]", teacher_room_cand):
                subj = entry[:last_idx].strip().rstrip(",")
                mr = re.search(r"\s+(\d+\S*)\s*$", teacher_room_cand)
                raw_t3 = teacher_room_cand[:mr.start()].strip() if mr else teacher_room_cand
                room = mr.group(1).strip() if mr else ""
                ct_m3 = _CT_TAIL_RE.search(raw_t3)
                teacher3, date_range3 = _parse_teacher(raw_t3)
                results.append({
                    "subject": subj, "teacher": teacher3, "room": room, "week": week,
                    "class_type": ct_m3.group(1).lower() if ct_m3 else "",
                    "date_range": date_range3,
                })
                continue

        # Паттерн 4: нет запятой перед преподавателем — ищем «Фамилия И. О.» в тексте
        # Пример: «...работы) Фадеев И. В. 5 уч.к 109»
        m4 = re.search(
            r"\s+([А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)\s*(.*?)\s*$",
            entry,
        )
        if m4:
            subj = entry[:m4.start()].strip().rstrip(",")
            teacher4, date_range4 = _parse_teacher(m4.group(1))
            room_part4 = m4.group(2).strip()
            ct_m4 = _CT_TAIL_RE.search(room_part4) if room_part4 else None
            class_type4 = ct_m4.group(1).lower() if ct_m4 else ""
            if room_part4 and not room_part4[0].isdigit():
                mr4 = re.search(r'(\d+\S*)\s*$', room_part4)
                room4 = mr4.group(1) if mr4 else ""
            else:
                room4 = room_part4
            results.append({
                "subject": subj, "teacher": teacher4, "room": room4, "week": week,
                "class_type": class_type4, "date_range": date_range4,
            })
            continue

        # Запасной вариант: сохраняем всё как предмет, убираем висящую запятую
        results.append({
            "subject": entry.rstrip(",").strip(),
            "teacher": "",
            "room": "",
            "week": week,
            "class_type": "",
            "date_range": "",
        })

    return results


def build_column_groups(header_row: list) -> list[dict]:
    """
    Parse header row to identify column groups.
    Each group: {para_col, time_col, directions: [{col, direction, course}]}
    """
    groups = []
    current_group = None
    for i, val in enumerate(header_row):
        if val == "Пара":
            current_group = {"para_col": i, "time_col": i + 1, "directions": []}
            groups.append(current_group)
        elif val and val != "Время" and current_group is not None:
            direction, course = parse_direction_course(str(val))
            if direction and course:
                current_group["directions"].append({"col": i, "direction": direction, "course": course})
    return groups


def import_schedule(excel_path: str = DEFAULT_EXCEL, db_path: str = DB_PATH) -> int:
    print(f"Reading: {excel_path}")
    wb = openpyxl.load_workbook(excel_path)
    ws = wb[wb.sheetnames[0]]
    print(f"Sheet '{wb.sheetnames[0]}': {ws.max_row} rows x {ws.max_column} cols")

    # Step 1: Read header BEFORE expanding merged cells.
    # After expansion the header cells that were originally null (merged extension columns)
    # get filled with the same value as their neighbour — which would create duplicate
    # direction entries. Reading the header first preserves only the original columns.
    header_row = [cell.value for cell in ws[HEADER_ROW]]
    groups = build_column_groups(header_row)

    # Step 2: Expand merged cells in DATA rows only (rows after the header).
    # This ensures that a lesson written once in a merged cell is visible from all
    # direction columns that the merged range spans.
    merged_ranges = list(ws.merged_cells.ranges)
    print(f"Expanding {len(merged_ranges)} merged cell ranges...")
    for merge_range in merged_ranges:
        top_left_value = ws.cell(merge_range.min_row, merge_range.min_col).value
        ws.unmerge_cells(str(merge_range))
        for r in range(merge_range.min_row, merge_range.max_row + 1):
            for c in range(merge_range.min_col, merge_range.max_col + 1):
                ws.cell(r, c).value = top_left_value

    print(f"\nDetected {len(groups)} course groups:")
    for g in groups:
        print(f"  col {g['para_col']}: {len(g['directions'])} directions")
        for d in g["directions"]:
            print(f"    [{d['course']} курс] col {d['col']}: {d['direction'][:55]}")

    # Collect records
    records = []
    current_day = None
    first_para_col = groups[0]["para_col"] if groups else 0

    for row_num in range(HEADER_ROW + 1, ws.max_row + 1):
        row = [cell.value for cell in ws[row_num]]

        first_para = row[first_para_col] if len(row) > first_para_col else None

        # Day header row: Пара column is None, but some cell contains a day name
        if first_para is None:
            for cell_val in row:
                if isinstance(cell_val, str) and cell_val.strip() in DAYS:
                    current_day = cell_val.strip()
                    break
            continue

        # Lesson row: Пара column is a digit
        try:
            int(str(first_para).strip())
        except (ValueError, TypeError):
            continue

        if current_day is None:
            continue

        for g in groups:
            time_val = row[g["time_col"]] if len(row) > g["time_col"] else None
            if not time_val:
                continue
            time_str = clean_text(str(time_val))

            for d in g["directions"]:
                cell_val = row[d["col"]] if len(row) > d["col"] else None
                if not cell_val:
                    continue

                for entry in parse_cell(str(cell_val)):
                    subject = entry["subject"]
                    if not subject or subject.lower() == "none":
                        continue
                    records.append((
                        d["course"],
                        d["direction"],
                        current_day,
                        time_str,
                        subject,
                        entry["teacher"],
                        entry["room"],
                        entry["week"],
                        entry.get("class_type", ""),
                        entry.get("date_range", ""),
                    ))

    print(f"\nParsed {len(records)} schedule records")

    # Write to DB
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course INTEGER, direction TEXT, day TEXT,
            time TEXT, subject TEXT, teacher TEXT, room TEXT,
            week TEXT DEFAULT '',
            class_type TEXT DEFAULT '',
            date_range TEXT DEFAULT ''
        )
    """)
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(schedule)").fetchall()}
    if "week" not in existing_cols:
        cursor.execute("ALTER TABLE schedule ADD COLUMN week TEXT DEFAULT ''")
    if "class_type" not in existing_cols:
        cursor.execute("ALTER TABLE schedule ADD COLUMN class_type TEXT DEFAULT ''")
    if "date_range" not in existing_cols:
        cursor.execute("ALTER TABLE schedule ADD COLUMN date_range TEXT DEFAULT ''")
    conn.commit()
    cursor.execute("SELECT COUNT(*) FROM schedule")
    old_count = cursor.fetchone()[0]
    print(f"Old records in DB: {old_count}")

    cursor.execute("DELETE FROM schedule")
    cursor.executemany(
        "INSERT INTO schedule (course, direction, day, time, subject, teacher, room, week, class_type, date_range) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        records,
    )
    conn.commit()
    cursor.execute("SELECT COUNT(*) FROM schedule")
    new_count = cursor.fetchone()[0]
    conn.close()

    print(f"New records in DB: {new_count}")
    print("Import complete!")
    return new_count


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EXCEL
    import_schedule(path)
