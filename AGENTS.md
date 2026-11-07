# Agent notes for 1541ultimate-tools

This file is for working on this repository. The guide for contributing to the 1541ultimate firmware with these tools
is a separate file, `checkout/AGENTS.md` (with `checkout/CLAUDE.md` and the procedures in `docs/agents/`), which
`install.sh` copies into a checkout. It is read on every agent turn, so keep it near 2,000 words, and put anything an
agent needs only sometimes into `docs/agents/`. Both must stay free of addresses, host names, home paths, private
repositories and personal tooling: `tests/test_guide.py` checks this, the word budget and the file references.

The agent skills in `skills/NAME/SKILL.md` follow the same rules. Each body stays at or under 500 words, names tools and
procedures only through `tooling/...`, `tooling/docs/agents/...` or the pinned `{{TOOLS_REF}}` document URL (an installed
skill cannot read `docs/`), and carries no date, incident story or local tool name. `tests/test_skills.py` checks this.

## Install

`install.sh` never overwrites a guide file that holds local work; see its header and `tests/test_install.py` before
changing the rules. It never creates, changes, moves or deletes `AGENTS.local.md` in the checkout.

```sh
python3 -m unittest discover -s tests -p 'test_install.py'
python3 -m unittest discover -s tests -p 'test_guide.py'
python3 -m unittest discover -s tests -p 'test_skills.py'
```

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
- Every command that touches the board holds the c64u device lock (`$DEVICE_LOCK_DIR/c64u.lock`, default
  `/tmp/1541ultimate-device-locks/c64u.lock`, the lock that `u64ii_jtag.py` and `tooling/with_device_locks.py` use) for
  its whole run. When someone else holds it, the command refuses, or waits when called with `--wait <seconds>`. A lock
  held by an ancestor process, for example `python3 tooling/with_device_locks.py c64u -- c64u-mode ...`, covers the
  command. Never remove or bypass the lock to run a command.
- A power cycle needs `--allow-power-cycle`. The outlet must be listed by `powerctl` under exactly the configured
  alias and id, and must not be in the `deny_outlets` list of the configuration (the code has no built-in names). The tool then powers off through REST first. Pass the flag only when
  the current task authorizes a restart of the board.
- `reload`, `flash:<name>` and `snapshot` wait up to 60 s (`--reload-wait S` or `[board] reload_wait`, 0 turns it off)
  for the board's REST and exit 1 when it stays silent. With `--allow-power-cycle` they then power-cycle the board
  under the rules above.
- Never test the power-cycle guards by actually cycling power. Use `--dry-run` and the unit tests, which replace the
  board with a fake.
- Run the host tests after any change to `tooling/c64u_mode.py` or the plugin interface:

  ```sh
  python3 -m unittest discover -s tooling -p 'test_c64u_mode.py'
  ```
