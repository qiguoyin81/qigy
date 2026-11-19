# short_drama_tracker

Weekly snapshots of ReelShort (TOP + New Release shelves) and DramaBox (Trending, 4 pages),
then a report of which titles and genre tags are growing, based on the change in each title's
cumulative view count between two snapshots.

```bash
python3 track.py snapshot   # run weekly; saves data/snapshot_<utc>.json
python3 track.py report     # compares the two latest snapshots
python3 track.py list
```

Notes
- Standard library only. Uses HTTPS_PROXY and a CA bundle from SSL_CERT_FILE / REQUESTS_CA_BUNDLE
  (falls back to /root/.ccr/ca-bundle.crt) if present.
- The two platforms report views in different units. They are reported separately and never mixed.
- Use snapshots at least a few days apart; the report warns under 3 days.
- If a page layout changes the script stops with an error instead of saving partial data.
