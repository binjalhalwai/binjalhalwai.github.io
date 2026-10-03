import pandas as pd
import json
from datetime import datetime
import sys

def convert(excel_file):
    # Load Excel with header at row 11 (0-indexed 11)
    # The header is actually at row 11 in the provided data
    try:
        df = pd.read_excel(excel_file, header=11)
        df = df.dropna(how='all')
        
        # Clean numeric data
        for col in df.columns:
            try:
                df[col] = pd.to_numeric(df[col])
            except:
                pass
        
        # Prepare for JSON
        rows = df.where(pd.notnull(df), None).values.tolist()
        data = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "cols": list(df.columns),
            "rows": rows
        }
        
        with open("screener_public.json", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print("Successfully generated screener_public.json")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convert_screener.py <path_to_excel_file>")
    else:
        convert(sys.argv[1])
