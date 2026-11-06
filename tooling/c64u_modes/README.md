# c64u-mode plugins

`tooling/c64u_mode.py` loads every `*.py` here and in the directories of `$C64U_MODE_PLUGINS` (colon-separated)
that defines `register(registry, config)`. A plugin adds modes and, optionally, a detector for `c64u-mode status`:

```python
def register(reg, cfg):
    def run(ctx, args):
        ctx.mode("flash:myfirmware")          # build on the built-in modes
        ctx.board.http("POST", "/v1/...")      # the board: REST, run(), ofl(), power_cycle(), notify()
    reg.add("myfirmware", "what it does", run)

    @reg.detector
    def detect(board):
        status, data = board.http("GET", "/my/status")
        return "my firmware" if status == 200 else None
```

Everything that touches the board goes through `ctx.board`, so `--dry-run` and the tests replace it.
Files whose name starts with `_` or `test_` are not loaded.
