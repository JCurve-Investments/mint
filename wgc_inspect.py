"""
World Gold Council file inspector (a helper, not a Scout).

WHY: the World Gold Council (WGC) publishes central bank gold buying as an Excel file once a month.
Its terms of use do not allow programs to scrape the website, so MINT does NOT download the file
by itself. YOU download it by hand once a month (normal personal, non-commercial use), and put it in
the folder  wgc_data/ . This helper then shows what is inside the file, so we can write the reader
for the real layout instead of guessing.

HOW TO GET THE FILE:
  1. Open https://www.gold.org/goldhub/data/monthly-central-bank-statistics in your browser.
  2. Click "Download xlsx" next to "Changes in World Official Gold Reserves".
  3. Move the downloaded .xlsx file into the  wgc_data  folder inside your mint folder.
  4. Run:  py wgc_inspect.py

It prints the sheet names and the first rows of each sheet. Take a screenshot of that output.
"""

import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl is not installed. Run this once:  py -m pip install -r requirements.txt")

FOLDER = Path(__file__).parent / "wgc_data"
ROWS_TO_SHOW = 15
COLUMNS_TO_SHOW = 10


def show_file(path):
    print(f"\nFILE: {path.name}")
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for sheet in book.worksheets:
        print(f"\n  SHEET: {sheet.title}  (about {sheet.max_row} rows x {sheet.max_column} columns)")
        for i, row in enumerate(sheet.iter_rows(values_only=True)):
            if i >= ROWS_TO_SHOW:
                print("  ...")
                break
            cells = ["" if c is None else str(c)[:18] for c in row[:COLUMNS_TO_SHOW]]
            if any(cells):
                print("  | " + " | ".join(cells))


def main():
    FOLDER.mkdir(exist_ok=True)
    files = sorted(FOLDER.glob("*.xlsx"))
    if not files:
        print("No .xlsx file found in the wgc_data folder.")
        print("Download it by hand (see the top of this file for the steps), put it in wgc_data, and run again.")
        return
    for path in files:
        show_file(path)


if __name__ == "__main__":
    main()
