# SpreadsheetBench Dataset

This directory stores the lightweight task split files used by the Xarena leaderboard benchmark. The workbook files are intentionally not committed.

Committed files:

```text
train_split/
  train/items.json
  val/items.json
  test/items.json
```

Prepare the workbook data before building:

```bash
bash prepare_data_root.sh
```

By default the script downloads `https://xskill.wiki/zip/xskill-compete.zip` and extracts its `data_root` into this directory. If you already have a local data root, use:

```bash
SOURCE_DATA_ROOT=/path/to/data_root bash prepare_data_root.sh
```

The algorithm image copies the prepared `data_root` to `/data` during the Docker build.
