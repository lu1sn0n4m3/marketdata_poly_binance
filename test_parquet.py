import pyarrow.parquet as pq
import pandas as pd

import pyarrow as pa
import pyarrow.parquet as pq
import pyarrow.compute as pc

FORCE_STRING_COLS = {"venue", "stream_id", "event_type", "side"}

def safe_read_table(path: str) -> pa.Table:
    pf = pq.ParquetFile(path)
    tables = []
    for i in range(pf.num_row_groups):
        t = pf.read_row_group(i)

        # decode any dictionary columns
        arrays = []
        fields = []
        for field, col in zip(t.schema, t.columns):
            name = field.name
            arr = col
            if pa.types.is_dictionary(arr.type):
                arr = pc.dictionary_decode(arr)
            if name in FORCE_STRING_COLS:
                arr = arr.cast(pa.string())
                out_type = pa.string()
            else:
                out_type = arr.type
            arrays.append(arr)
            fields.append(pa.field(name, out_type, nullable=True))

        t = pa.table(arrays, schema=pa.schema(fields))
        tables.append(t)

    schema = pa.unify_schemas([t.schema for t in tables])
    tables = [t.cast(schema, safe=False) for t in tables]
    return pa.concat_tables(tables, promote=True)

df = safe_read_table("data/final/venue=binance/stream_id=BTCUSDT/date=2026-01-18/hour=05/data.parquet").to_pandas()
bbo = df[df["event_type"]=="bbo"].copy()

bbo["spread"] = bbo["ask_px"] - bbo["bid_px"]
print(bbo["spread"].describe(percentiles=[.5,.9,.99]))

# sanity checks
print("negative spreads:", (bbo["spread"] < 0).sum())
print("crossed books:", (bbo["ask_px"] < bbo["bid_px"]).sum())
print("zero spreads:", (bbo["spread"] == 0).sum())

# how sparse in time?
bbo = bbo.sort_values("ts_recv")
dt = bbo["ts_recv"].diff()
print(dt.describe(percentiles=[.5,.9,.99]))