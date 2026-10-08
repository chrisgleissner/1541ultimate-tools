#!/usr/bin/env python3
"""Checks the tools against a u64ii application built from a 1541ultimate checkout.

The gdb server reads FreeRTOS task lists through structure offsets it carries
as constants, the unwinder recognises frame set-up by the instructions GCC
emits, and the JTAG tool loads ultimate.bin at a fixed address. These tests
compare all three with the ELF the build produced: the offsets with its DWARF
debug information, the unwinder with its disassembly, and the load address
with its program headers.

    ULTIMATE_REPO_DIR=/path/to/1541ultimate python3 -m unittest tests/upstream/test_build.py

The checkout must have been built with `build-tool u64ii`. Without
ULTIMATE_REPO_DIR, or without the build, every test is skipped; with
UPSTREAM_REQUIRED=1 either is a failure instead. Disassembly needs a RISC-V
objdump (riscv32-unknown-elf-objdump, or riscv64-unknown-elf-objdump from the
Debian/Ubuntu package binutils-riscv64-unknown-elf); objdump and gdb print
instructions through the same libopcodes, so the text is what the unwinder sees
in gdb.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLING = os.path.join(HERE, "..", "..", "tooling")
sys.path.insert(0, TOOLING)
os.environ.setdefault("U64II_JTAG_LOCK", "off")
import u64ii_gdbstub as gs  # noqa: E402
import u64ii_jtag as jt  # noqa: E402

REPO = os.environ.get("ULTIMATE_REPO_DIR", "")
REQUIRED = os.environ.get("UPSTREAM_REQUIRED") == "1"
RESULT = ("target", "u64ii", "riscv", "ultimate", "result")
ELF = os.path.join(REPO, *RESULT, "ultimate.elf")
BIN = os.path.join(REPO, *RESULT, "ultimate.bin")


def setUpModule():
    if not REPO or not os.path.isfile(ELF) or not os.path.isfile(BIN):
        if REQUIRED:
            raise RuntimeError(f"no u64ii build under ULTIMATE_REPO_DIR={REPO!r}")
        raise unittest.SkipTest("no u64ii build (set ULTIMATE_REPO_DIR and run build-tool u64ii)")


def tool(*names):
    """The first of `names` found on PATH."""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


# ---------------------------------------------------------------------------
# DWARF, from `objdump --dwarf=info`
# ---------------------------------------------------------------------------
DIE = re.compile(r"^\s*<(\d+)><([0-9a-f]+)>: Abbrev Number: \d+ \((DW_TAG_\w+)\)")
ATTR = re.compile(r"^\s*<[0-9a-f]+>\s+(DW_AT_\w+)\s*:\s*(.*)$")


def parse_dies(text):
    """Every DIE as (level, offset, tag, attributes), in file order."""
    dies = []
    for line in text.splitlines():
        m = DIE.match(line)
        if m:
            dies.append((int(m.group(1)), int(m.group(2), 16), m.group(3), {}))
            continue
        m = ATTR.match(line)
        if m and dies:
            value = m.group(2).strip()
            # "(indirect string, offset: 0x3ef): name" -> "name"
            value = re.sub(r"^\(indirect (line )?string, offset: 0x[0-9a-f]+\):\s*", "", value)
            dies[-1][3][m.group(1)] = value
    return dies


def number(value):
    """A constant, or a member location as GCC encodes it for DWARF 2/3:
    "2 byte block: 23 4 (DW_OP_plus_uconst: 4)"."""
    m = re.search(r"DW_OP_plus_uconst: (\d+)", value)
    if m:
        return int(m.group(1))
    m = re.match(r"(0x[0-9a-f]+|\d+)", value)
    return int(m.group(1), 0)


class Dwarf:
    def __init__(self, elf):
        out = subprocess.run(["objdump", "--dwarf=info", elf], capture_output=True,
                             text=True, check=True).stdout
        self.dies = parse_dies(out)
        self.by_offset = {d[1]: i for i, d in enumerate(self.dies)}

    def children(self, index):
        level = self.dies[index][0]
        out = []
        for die in self.dies[index + 1:]:
            if die[0] <= level:
                break
            if die[0] == level + 1:
                out.append(die)
        return out

    def struct(self, name):
        """(byte size, {member: (offset, type reference)}) of the first definition."""
        for i, (_, _, tag, attrs) in enumerate(self.dies):
            if tag == "DW_TAG_structure_type" and attrs.get("DW_AT_name") == name \
                    and "DW_AT_byte_size" in attrs:
                members = {}
                for _, _, ctag, cattrs in self.children(i):
                    if ctag == "DW_TAG_member":
                        ref = re.search(r"<0x([0-9a-f]+)>", cattrs.get("DW_AT_type", ""))
                        members[cattrs["DW_AT_name"]] = (
                            number(cattrs.get("DW_AT_data_member_location", "0")),
                            int(ref.group(1), 16) if ref else None)
                return number(attrs["DW_AT_byte_size"]), members
        raise AssertionError(f"struct {name} not in the DWARF information")

    def array_length(self, type_offset):
        index = self.by_offset[type_offset]
        tag = self.dies[index][2]
        if tag != "DW_TAG_array_type":
            raise AssertionError(f"type at {type_offset:#x} is {tag}, not an array")
        for _, _, ctag, cattrs in self.children(index):
            if ctag == "DW_TAG_subrange_type":
                if "DW_AT_count" in cattrs:
                    return number(cattrs["DW_AT_count"])
                return number(cattrs["DW_AT_upper_bound"]) + 1
        raise AssertionError("array without a subrange")


class FreeRtosLayout(unittest.TestCase):
    """The gdb server's structure offsets against the firmware's DWARF."""

    @classmethod
    def setUpClass(cls):
        cls.dwarf = Dwarf(ELF)

    def test_task_control_block(self):
        _, members = self.dwarf.struct("tskTaskControlBlock")
        self.assertEqual(members["pxTopOfStack"][0], gs.TCB_TOP_OF_STACK)
        self.assertEqual(members["xStateListItem"][0], gs.TCB_STATE_LIST_ITEM)
        self.assertEqual(members["uxPriority"][0], gs.TCB_PRIORITY)
        self.assertEqual(members["pcTaskName"][0], gs.TCB_NAME)
        self.assertEqual(self.dwarf.array_length(members["pcTaskName"][1]), gs.TCB_NAME_LEN)

    def test_list(self):
        size, members = self.dwarf.struct("xLIST")
        self.assertEqual(size, gs.LIST_BYTES)
        self.assertEqual(members["uxNumberOfItems"][0], gs.LIST_ITEMS)
        self.assertEqual(members["xListEnd"][0], gs.LIST_END)

    def test_list_item(self):
        _, members = self.dwarf.struct("xLIST_ITEM")
        self.assertEqual(members["pxNext"][0], gs.ITEM_NEXT)
        self.assertEqual(members["pvOwner"][0], gs.ITEM_OWNER)

    def test_mini_list_item_links_like_a_list_item(self):
        # The server walks xListEnd with the ListItem_t offset of pxNext.
        _, members = self.dwarf.struct("xMINI_LIST_ITEM")
        self.assertEqual(members["pxNext"][0], gs.ITEM_NEXT)


class Symbols(unittest.TestCase):
    """read_symbols() on the real ELF finds everything the server reads."""

    @classmethod
    def setUpClass(cls):
        cls.symbols = gs.read_symbols(ELF)

    def test_task_lists_and_scheduler_symbols(self):
        for name in ("pxCurrentTCB", "__global_pointer$") + tuple(n for n, _ in gs.TASK_LISTS):
            with self.subTest(symbol=name):
                self.assertIn(name, self.symbols)

    def test_list_sizes(self):
        ready, size = self.symbols["pxReadyTasksLists"]
        self.assertGreater(size, 0)
        self.assertEqual(size % gs.LIST_BYTES, 0, "pxReadyTasksLists is not an array of List_t")
        for name, _ in gs.TASK_LISTS[1:]:
            with self.subTest(list=name):
                self.assertEqual(self.symbols[name][1], gs.LIST_BYTES)

    def test_data_lies_where_the_server_may_read(self):
        for name in ("pxCurrentTCB",) + tuple(n for n, _ in gs.TASK_LISTS):
            with self.subTest(symbol=name):
                self.assertLess(self.symbols[name][0], gs.MEMORY_LIMIT)


class Image(unittest.TestCase):
    """The image the JTAG tool loads and starts."""

    def test_application_is_linked_at_the_load_address(self):
        header = subprocess.run(["readelf", "-hlW", ELF], capture_output=True, text=True,
                                check=True).stdout
        entry = int(re.search(r"Entry point address:\s+0x([0-9a-f]+)", header).group(1), 16)
        first_load = int(re.search(r"LOAD\s+0x[0-9a-f]+\s+0x([0-9a-f]+)", header).group(1), 16)
        self.assertEqual(entry, jt.APP_ADDRESS)
        self.assertEqual(first_load, jt.APP_ADDRESS)

    def test_bin_is_the_loadable_image(self):
        objcopy = tool("riscv32-unknown-elf-objcopy", "riscv64-unknown-elf-objcopy", "objcopy")
        with tempfile.NamedTemporaryFile(suffix=".bin") as out:
            subprocess.run([objcopy, "-O", "binary", ELF, out.name], check=True, capture_output=True)
            with open(BIN, "rb") as built:
                self.assertEqual(out.read(), built.read())

    def test_bin_fits_below_the_io_space(self):
        self.assertLess(jt.APP_ADDRESS + os.path.getsize(BIN), gs.MEMORY_LIMIT)

    def test_deploy_script_reads_this_file(self):
        with open(os.path.join(TOOLING, "build_and_deploy_u64ii.sh")) as handle:
            self.assertIn("target/u64ii/riscv/ultimate/result/ultimate.bin", handle.read())


# ---------------------------------------------------------------------------
# Unwinder
# ---------------------------------------------------------------------------
def load_unwinder():
    """u64ii_gdb_unwind with just enough of a gdb module to import it."""
    gdb = types.ModuleType("gdb")
    unwinder = types.ModuleType("gdb.unwinder")
    unwinder.Unwinder = type("Unwinder", (), {"__init__": lambda self, name: None})
    unwinder.register_unwinder = lambda *args, **kwargs: None
    gdb.unwinder = unwinder
    saved = {k: sys.modules.get(k) for k in ("gdb", "gdb.unwinder", "u64ii_gdb_unwind")}
    sys.modules.update({"gdb": gdb, "gdb.unwinder": unwinder})
    sys.modules.pop("u64ii_gdb_unwind", None)
    try:
        import u64ii_gdb_unwind
        return u64ii_gdb_unwind
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class DisassembledArch:
    """gdb.Architecture.disassemble() over objdump's listing of one function."""

    def __init__(self, instructions):
        self.instructions = instructions

    def disassemble(self, start, end, count):
        out = [{"addr": a, "asm": t} for a, t in self.instructions if start <= a <= end]
        return out[:count]


def functions(listing):
    """{name: [(address, instruction text)]} from `objdump -d --no-show-raw-insn`."""
    out, current = {}, None
    for line in listing.splitlines():
        m = re.match(r"^([0-9a-f]+) <(.+)>:$", line)
        if m:
            current = out.setdefault(m.group(2), [])
            continue
        m = re.match(r"^\s+([0-9a-f]+):\s+(\S.*)$", line)
        if m and current is not None:
            current.append((int(m.group(1), 16), m.group(2).split("#")[0].strip()))
    return out


# Hand-written assembly and libgcc helpers that call without a C frame.
NO_C_FRAME = re.compile(r"^(__crt0_|handle_|test_if_|mem(set|cpy|move)$|__(u)?(mod|div)[sd]i3$)")


class Unwinder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        objdump = tool("riscv32-unknown-elf-objdump", "riscv64-unknown-elf-objdump",
                       "riscv64-linux-gnu-objdump")
        if objdump is None:
            if REQUIRED:
                raise AssertionError("no RISC-V objdump found")
            raise unittest.SkipTest("no RISC-V objdump found")
        listing = subprocess.run([objdump, "-d", "--no-show-raw-insn", ELF], capture_output=True,
                                 text=True, check=True).stdout
        cls.functions = functions(listing)
        cls.unwind = load_unwinder()

    def scan(self, name):
        instructions = self.functions[name]
        start, end = instructions[0][0], instructions[-1][0] + 4
        return self.unwind._scan(DisassembledArch(instructions), start, end)

    def calls(self, name):
        return any(re.match(r"^(jal|jalr|call)\s", text) and not text.startswith(("jal\tzero", "jalr\tzero"))
                   for _, text in self.functions[name])

    def test_every_calling_c_function_has_a_frame_the_unwinder_finds(self):
        missed = []
        checked = 0
        for name in self.functions:
            if not self.calls(name) or NO_C_FRAME.match(name):
                continue
            checked += 1
            size, alloc, saves = self.scan(name)
            if alloc is None or size <= 0 or "ra" not in saves:
                missed.append(name)
        self.assertGreater(checked, 1000)
        self.assertEqual(missed, [], f"{len(missed)} of {checked} functions")

    def test_shrink_wrapped_functions(self):
        # Functions that branch before they allocate their frame: the case
        # gdb's own prologue analyser gives up on, and the reason this
        # unwinder exists. The firmware must still contain some, or the test
        # above proves nothing about them.
        wrapped = 0
        for name, instructions in self.functions.items():
            if not self.calls(name) or NO_C_FRAME.match(name):
                continue
            size, alloc, saves = self.scan(name)
            if alloc is None:
                continue
            if any(re.match(r"^(beq|bne|blt|bge|bltu|bgeu|beqz|bnez|blez|bgez|bltz|bgtz)\s", t)
                   for a, t in instructions if a < alloc):
                wrapped += 1
                with self.subTest(function=name):
                    self.assertGreater(size, 0)
                    self.assertIn("ra", saves)
                    self.assertLess(saves["ra"][0], size)
                    self.assertGreater(saves["ra"][1], alloc)
        self.assertGreater(wrapped, 10)

if __name__ == "__main__":
    unittest.main()
