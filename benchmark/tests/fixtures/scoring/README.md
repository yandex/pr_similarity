# Scoring golden fixtures

`golden-code.json` contains ten independently recorded nontrivial results,
one for each supported CodeBLEU language. Inputs are small loop examples;
human changes multiply by two, AI changes multiply by three and rename the
accumulator. All three file states differ, so these exercise actual MLP
inference rather than shortcuts.

The expected scores/pair scores were recorded with the same fixed model,
CodeBLEU commit `b0edb622f6a52fe9d1edc407be5061d3e1462a7f`, the grammar
profile pinned in `requirements.lock`, and `PYTHONHASHSEED=42`.
Original weight source SHA-256:
`51ce752bb196c24fd8ad45d064ef7225b1927b39fcee9f9266fa66667d08bbc2`.
Feature source SHA-256:
`cceb314252f728f326e450a057b7c1d0f85b27d098fa8b36699dcd12604267eb`.
Text normalization/LOC source SHA-256:
`58436870108729e68214845221c9192653bd95f2c19dca8897b4765dabc1b7d6`.

The tests compare the port against these frozen results. Changing expected
values to accommodate an accidental implementation change is not a fix.
Existence-aware shortcuts and PR aggregation have separate behavioural tests.
