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


def clean_text(text: str) -> str:
    """Normalize whitespace; collapse spaced-out text like 'Ф   и  л  о ...' → 'Фило...'"""
    if not text:
        return ""
    text = str(text).strip()
    # Spaced-out text detection: char + 2+ spaces + char pattern
    if re.match(r"^\S\s{2,}\S", text):
        text = re.sub(r"\s+", " ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"  +", " ", text)
    return text.strip()


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

    Returns list of dicts: {subject, teacher, room, week}
    week: '' | 'чет' | 'нечет'
    """
    text = clean_text(cell_text)
    if not text:
        return []

    # Group lines into logical entries.
    # A new entry starts when a line begins with * or ** (week markers).
    raw_lines = text.split("\n")
    entries_raw: list[str] = []
    current: list[str] = []

    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("*") and current:
            # New week-entry begins — flush previous
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

        # Normalize internal spaces left after joining lines
        entry = re.sub(r"  +", " ", entry).strip()

        # Try to parse: "Subject, Teacher [лк|пр|лб|лекция] Room"
        m = re.match(
            r"^(.+?),\s*(.+?)\s+(лк|пр|лб|лекция)\s+(.+)$",
            entry,
            re.IGNORECASE,
        )
        if m:
            results.append({
                "subject": m.group(1).strip(),
                "teacher": m.group(2).strip(),
                "room": m.group(4).strip(),
                "week": week,
            })
        else:
            # Can't parse details — store whole entry as subject
            results.append({"subject": entry, "teacher": "", "room": "", "week": week})

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
                    # Prefix subject with week marker if needed
                    if entry["week"]:
                        subject = f"[{entry['week']}] {subject}"
                    records.append((
                        d["course"],
                        d["direction"],
                        current_day,
                        time_str,
                        subject,
                        entry["teacher"],
                        entry["room"],
                    ))

    print(f"\nParsed {len(records)} schedule records")

    # Write to DB
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM schedule")
    old_count = cursor.fetchone()[0]
    print(f"Old records in DB: {old_count}")

    cursor.execute("DELETE FROM schedule")
    cursor.executemany(
        "INSERT INTO schedule (course, direction, day, time, subject, teacher, room) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
