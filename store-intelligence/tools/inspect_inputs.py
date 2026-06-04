"""One-off: inspect the REAL challenge inputs (store layout xlsx + POS csv) so we can
build store_layout.json and the POS mapping from ground truth rather than guesses.
Run with a python that has pandas + openpyxl (the Anaconda base does)."""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(r"D:\purple")
XLSX = ROOT / "Brigade Road - Store layoutc5f5d56.xlsx"
CSV = ROOT / "Brigade_Bangalore_10_April_26 (1)bc6219c.csv"

print("=" * 70)
print("STORE LAYOUT XLSX")
print("=" * 70)
xl = pd.ExcelFile(XLSX)
print("sheets:", xl.sheet_names)
for sh in xl.sheet_names:
    df = xl.parse(sh, header=None)
    print(f"\n--- sheet '{sh}'  shape={df.shape} ---")
    # print non-empty cells compactly
    with pd.option_context("display.max_rows", 200, "display.max_columns", 40, "display.width", 200):
        print(df.fillna("").to_string(max_rows=120))

print("\n" + "=" * 70)
print("POS CSV")
print("=" * 70)
pos = pd.read_csv(CSV, dtype=str)
print("rows:", len(pos))
print("columns:", list(pos.columns))
print("store_id values:", pos["store_id"].dropna().unique()[:10] if "store_id" in pos else "n/a")
print("store_name:", pos["store_name"].dropna().unique()[:5] if "store_name" in pos else "n/a")
print("unique order_id:", pos["order_id"].nunique())
print("unique invoice_number:", pos["invoice_number"].nunique())
print("order_date range:", pos["order_date"].min(), "->", pos["order_date"].max())
print("order_time range:", pos["order_time"].min(), "->", pos["order_time"].max())
print("invoice_type values:", pos["invoice_type"].dropna().unique()[:10])
# basket value per order (sum total_amount over line items)
pos["total_amount_f"] = pd.to_numeric(pos["total_amount"], errors="coerce")
per_order = pos.groupby("order_id").agg(value=("total_amount_f", "sum"),
                                        time=("order_time", "first"),
                                        date=("order_date", "first")).reset_index()
print("\nper-order baskets: count=", len(per_order),
      "min=", round(per_order.value.min(), 2), "max=", round(per_order.value.max(), 2),
      "median=", round(per_order.value.median(), 2))
print("earliest order_time:", per_order.time.min(), "latest:", per_order.time.max())
print("\nsample per-order rows:")
print(per_order.sort_values("time").head(12).to_string())
