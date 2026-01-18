import pyarrow.parquet as pq

path = "data/tmp/venue=binance/stream_id=BTCUSDT/date=2026-01-18/hour=12/checkpoint-2026011812-2221-5c9eff8b.parquet"
pf = pq.ParquetFile(path)

for i in range(pf.num_row_groups):
    t = pf.read_row_group(i, columns=["venue"])
    print(i, t.schema.field("venue").type)