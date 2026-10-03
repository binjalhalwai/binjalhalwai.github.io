r"""Convert the daily NEPSE stock-screener Excel export into screener_public.json.

Usage (Windows):  python convert_screener.py "C:\path\to\screener.xlsx"
Save the output next to nepse_news_hub.py; the hub picks it up automatically.
"""
import json
import math
import sys
from datetime import datetime

import pandas as pd


def find_header_row(path):
    """The export has a title block above the table; find the row that contains 'Company'."""
    raw = pd.read_excel(path, header=None, nrows=40)
    for i in range(len(raw)):
        if raw.iloc[i].astype(str).str.strip().eq("Company").any():
            return i
    raise ValueError("Could not find a header row containing 'Company'")


def clean(v):
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if hasattr(v, "item"):                      # numpy scalar -> python
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def convert(excel_file, out_file="screener_public.json"):
    try:
        df = pd.read_excel(excel_file, header=find_header_row(excel_file))
        df = df.dropna(how="all")
        for col in df.columns:                  # numeric where possible, text columns stay text
            try:
                df[col] = pd.to_numeric(df[col])
            except (ValueError, TypeError):
                pass
        rows = [[clean(v) for v in row] for row in df.astype(object).values.tolist()]
        # drop the 'Average' row and the 'Printed/Exported by <email>' footer so no personal data is published
        ci = list(df.columns).index("Company")
        rows = [r for r in rows
                if isinstance(r[ci], str) and r[ci].strip()
                and r[ci].strip().lower() != "average"
                and not r[ci].lower().startswith(("printed", "exported"))]
        data = {"date": datetime.now().strftime("%Y-%m-%d"),
                "cols": [str(c) for c in df.columns], "rows": rows}
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, allow_nan=False)
        print(f"Successfully generated {out_file} ({len(rows)} companies, {len(df.columns)} columns)")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python convert_screener.py "<path_to_excel_file>"')
    else:
        convert(sys.argv[1])
