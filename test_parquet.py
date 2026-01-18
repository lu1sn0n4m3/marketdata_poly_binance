import pyarrow.parquet as pq

path = "data/final/venue=binance/stream_id=BTCUSDT/date=2026-01-18/hour=11/data.parquet"
pf = pq.ParquetFile(path)

for i in range(pf.num_row_groups):
    t = pf.read_row_group(i, columns=["venue"])
    print(i, t.schema.field("venue").type)