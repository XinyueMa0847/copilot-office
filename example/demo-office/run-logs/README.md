# Run logs

`registry.json` is a JSON object keyed by run-directory name. Each entry uses:

```json
{
  "name": "descriptive run name",
  "host": "machine name",
  "rid": "run or job ID",
  "started": "YYYY-MM-DD HH:MM TZ",
  "attempt": 1,
  "setup": "configuration summary",
  "status": "optional current status",
  "superseded": false
}
```

`status` and `superseded` are optional. A run may also have `<name>.md` with an
`## Issues / events` section and `<name>.health.log` containing health blocks.
Record observed times and distinguish submitted, responsive, completed, and
validated states.
