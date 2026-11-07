# Agent notes for 1541ultimate-tools

## c64u-mode

`tooling/c64u_mode.py` (installed as `c64u-mode`) switches one C64 Ultimate or Ultimate 64 Elite II between
firmware setups with a single command: `status`, `list`, `<mode>`, `ensure <mode>`, `snapshot <name>`, `check-power`.
The built-in modes are `flash:<name>` (swap the whole flash to a stored image over JTAG), `jtag` (run a bitstream and
application from RAM) and `reload` (reconfigure the FPGA from flash). The header comment of the script is the
reference; `tooling/c64u_modes/README.md` describes plugins.

- Configuration is `~/.config/c64u-mode/config.ini` (board host, smart outlet, short mode names, hotkeys). Stored
  flash images and `state.json` are in `~/.local/share/c64u-mode/`. Read-only commands: `status`, `list`, `--dry-run`.
- Other firmware setups are added as plugins, not by editing this repository. Every `*.py` in `tooling/c64u_modes/`
  and in the directories listed in `$C64U_MODE_PLUGINS` (colon-separated) that defines `register(registry, config)` is
  loaded. A plugin reaches the board only through `ctx.board`, so `--dry-run` and the tests can replace it.
- Every command that touches the board holds the c64u device lock (`/tmp/1541ultimate-device-locks/c64u.lock`, the
  lock that `u64ii_jtag.py` and `with-device-locks` use) for its whole run. When someone else holds it, the command
  refuses, or waits when called with `--wait <seconds>`. Never remove or bypass the lock to run a command.
- A power cycle needs `--allow-power-cycle`. The outlet must be listed by `powerctl` under exactly the configured
  alias and id, and must not be in the deny list. The tool then powers off through REST first. Pass the flag only when
  the current task authorizes a restart of the board.
- Never test the power-cycle guards by actually cycling power. Use `--dry-run` and the unit tests, which replace the
  board with a fake.
- Run the host tests after any change to `tooling/c64u_mode.py` or the plugin interface:

  ```sh
  python3 -m unittest discover -s tooling -p 'test_c64u_mode.py'
  ```
