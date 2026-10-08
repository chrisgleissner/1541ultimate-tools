#!/usr/bin/env python3
"""Host tests for u64ii_gdb_unwind.py, the gdb unwinder for the RISC-V application.

The module only runs inside gdb, so these tests put a fake `gdb` package into
sys.modules before importing it. The fake models the parts of the gdb Python
API the unwinder uses: blocks for a pc, an architecture that disassembles a
listing, a pending frame with registers, the selected inferior's memory,
gdb.Value casts and the unwinder registration. The tests check the caller pc,
sp and s0 (fp) the unwinder recovers from a function's prologue, the frame id
it reports, and the cases in which it declines to unwind.

    python3 tooling/test_u64ii_gdb_unwind.py
"""

import os
import struct
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# A fake gdb
# ---------------------------------------------------------------------------
class GdbError(RuntimeError):
    """gdb.error derives from RuntimeError in real gdb."""


class RegType:
    def __init__(self, name):
        self.name = name


class Value:
    def __init__(self, value, type=None):
        self.value, self.type = int(value), type

    def cast(self, type):
        return Value(self.value & 0xFFFFFFFF, type)

    def __int__(self):
        return self.value


class Unwinder:
    def __init__(self, name):
        self.name = name
        self.enabled = True


registered = []
gdb = types.ModuleType("gdb")
gdb.error = GdbError
gdb.Value = Value
gdb.block_for_pc = None            # set per test
gdb.selected_inferior = None       # set per test
gdb_unwinder = types.ModuleType("gdb.unwinder")
gdb_unwinder.Unwinder = Unwinder
gdb_unwinder.register_unwinder = lambda locus, unwinder, replace=False: \
    registered.append((locus, unwinder, replace))
gdb.unwinder = gdb_unwinder
# The fake is installed only while the module imports, so no other test file
# sees a `gdb` or this unwinder in sys.modules; `uw` keeps its own reference.
_SAVED = {name: sys.modules.get(name) for name in ("gdb", "gdb.unwinder", "u64ii_gdb_unwind")}
sys.modules.update({"gdb": gdb, "gdb.unwinder": gdb_unwinder})
sys.modules.pop("u64ii_gdb_unwind", None)
try:
    import u64ii_gdb_unwind as uw  # noqa: E402
finally:
    for _name, _module in _SAVED.items():
        if _module is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _module


class Block:
    def __init__(self, start, end, function=None, superblock=None):
        self.start, self.end, self.function, self.superblock = start, end, function, superblock


class Arch:
    """Disassembles a fixed listing of (address, text)."""

    def __init__(self, listing):
        self.listing = listing
        self.calls = []
        self.consumed = 0

    def disassemble(self, start, end, count):
        self.calls.append((start, end, count))
        for addr, text in self.listing:
            if start <= addr <= end:
                self.consumed += 1
                yield {"addr": addr, "asm": text, "length": 4}


class Inferior:
    def __init__(self, memory):
        self.memory = memory
        self.reads = []

    def read_memory(self, address, length):
        self.reads.append((address, length))
        return memoryview(bytes(self.memory.get(address + i, 0) for i in range(length)))


class UnwindInfo:
    def __init__(self, frame_id):
        self.frame_id = frame_id
        self.saved = {}

    def add_saved_register(self, name, value):
        self.saved[name] = value


class PendingFrame:
    def __init__(self, regs, arch):
        self.regs, self.arch = regs, arch

    def read_register(self, name):
        if name not in self.regs:
            raise GdbError(f"register {name} is not available")
        return Value(self.regs[name], RegType(name))

    def architecture(self):
        return self.arch

    def create_unwind_info(self, frame_id):
        return UnwindInfo(frame_id)


# A shrink-wrapped function: it branches before it allocates its frame.
START, END = 0x1000, 0x1040
LISTING = [
    (0x1000, "beqz\ta0,0x1030"),
    (0x1004, "addi\tsp,sp,-48"),
    (0x1008, "sw\ts1,36(sp)"),
    (0x100C, "sw\tra,44(sp)"),
    (0x1010, "sw\ts0,40(sp)"),
    (0x1014, "jal\tra,0x2000"),
    (0x1018, "lw\tra,44(sp)"),
    (0x101C, "addi\tsp,sp,48"),
    (0x1020, "ret"),
    (0x1030, "ret"),
]
SP = 0x8000


def memory_with(**words):
    """Little-endian words at sp-relative offsets: memory_with(o44=..., o40=...)."""
    mem = {}
    for key, value in words.items():
        for i, b in enumerate(struct.pack("<L", value)):
            mem[SP + int(key[1:]) + i] = b
    return mem


class UnwindTestBase(unittest.TestCase):
    def setUp(self):
        self.blocks = {}
        self.inferior = Inferior(memory_with(o44=0x5550, o40=0x7770))

        def block_for_pc(pc):
            if pc in self.blocks:
                return self.blocks[pc]
            raise RuntimeError("cannot locate object file for block")

        gdb.block_for_pc = block_for_pc
        gdb.selected_inferior = lambda: self.inferior

    def unwind(self, pc, regs=None, listing=LISTING, block=None):
        self.blocks[pc] = block or Block(START, END, function="fn")
        self.arch = Arch(listing)
        r = {"pc": pc, "sp": SP, "ra": 0x6660, "fp": 0x9990}
        r.update(regs or {})
        return uw.U64iiScanUnwinder()(PendingFrame({k: v for k, v in r.items() if v is not None},
                                                   self.arch))

    def saved(self, info):
        return {name: int(v) for name, v in info.saved.items()}


class RegistrationTest(unittest.TestCase):
    def test_registered_globally_replacing_an_older_copy(self):
        self.assertEqual(len(registered), 1)
        locus, unwinder, replace = registered[0]
        self.assertIsNone(locus)
        self.assertIsInstance(unwinder, uw.U64iiScanUnwinder)
        self.assertEqual(unwinder.name, "u64ii-riscv-scan")
        self.assertTrue(replace)


class FrameIdTest(unittest.TestCase):
    def test_values_cast_to_the_register_type(self):
        t = RegType("sp")
        fid = uw._FrameId(0x8030, 0x1000, t)
        self.assertEqual((int(fid.sp), int(fid.pc)), (0x8030, 0x1000))
        self.assertIs(fid.sp.type, t)
        self.assertIs(fid.pc.type, t)


class ScanTest(unittest.TestCase):
    def test_finds_allocation_and_saves(self):
        arch = Arch(LISTING)
        size, alloc, saves = uw._scan(arch, START, END)
        self.assertEqual((size, alloc), (48, 0x1004))
        self.assertEqual(saves, {"ra": (44, 0x100C), "s0": (40, 0x1010)})
        self.assertEqual(arch.calls, [(START, END - 1, uw.LIMIT)])

    def test_stops_once_both_saves_are_found(self):
        arch = Arch(LISTING)
        uw._scan(arch, START, END)
        self.assertEqual(arch.consumed, 5)                 # up to the s0 save

    def test_saves_before_the_allocation_are_ignored(self):
        listing = [(0x1000, "sw ra,12(sp)"), (0x1004, "addi sp,sp,-16"), (0x1008, "sw ra,8(sp)")]
        self.assertEqual(uw._scan(Arch(listing), 0x1000, 0x100C), (16, 0x1004, {"ra": (8, 0x1008)}))

    def test_first_save_of_a_register_wins(self):
        listing = [(0x1000, "addi sp,sp,-32"), (0x1004, "sw s0,24(sp)"),
                   (0x1008, "sw s0,4(sp)"), (0x100C, "sw ra,28(sp)")]
        self.assertEqual(uw._scan(Arch(listing), 0x1000, 0x1010)[2],
                         {"s0": (24, 0x1004), "ra": (28, 0x100C)})

    def test_only_the_first_allocation_counts(self):
        listing = [(0x1000, "  addi sp, sp, -16  "), (0x1004, "addi sp,sp,-64")]
        self.assertEqual(uw._scan(Arch(listing), 0x1000, 0x1008), (16, 0x1000, {}))

    def test_patterns_that_are_not_prologue(self):
        listing = [(0x1000, "addi sp,sp,16"),               # deallocation
                   (0x1004, "addi s0,sp,-16"),              # not sp
                   (0x1008, "addi sp,sp,-32"),
                   (0x100C, "sw ra,12(s0)"),                # not sp-relative
                   (0x1010, "sw a0,8(sp)"),                 # not ra/s0
                   (0x1014, "sw ra,-4(sp)")]                # negative offset
        self.assertEqual(uw._scan(Arch(listing), 0x1000, 0x1018), (32, 0x1008, {}))

    def test_leaf_without_frame(self):
        self.assertEqual(uw._scan(Arch([(0x1000, "ret")]), 0x1000, 0x1004), (0, None, {}))


class FunctionRangeTest(UnwindTestBase):
    def test_unknown_pc(self):
        self.assertIsNone(uw._function_range(0x4242))

    def test_lexical_block_resolves_to_its_function(self):
        fn = Block(0x1000, 0x1100, function="fn")
        self.blocks[0x1050] = Block(0x1040, 0x1060, superblock=Block(0x1030, 0x1080, superblock=fn))
        self.assertEqual(uw._function_range(0x1050), (0x1000, 0x1100))

    def test_block_without_function(self):
        self.blocks[0x1050] = Block(0x1040, 0x1060, superblock=Block(0, 0xFFFF))
        self.assertIsNone(uw._function_range(0x1050))


class UnwinderTest(UnwindTestBase):
    def test_framed_after_both_saves(self):
        info = self.unwind(0x1014)
        self.assertEqual(self.saved(info), {"pc": 0x5550, "sp": SP + 48, "fp": 0x7770})
        self.assertEqual((int(info.frame_id.sp), int(info.frame_id.pc)), (SP + 48, START))
        self.assertEqual(info.frame_id.sp.type.name, "sp")
        self.assertEqual(info.saved["pc"].type.name, "pc")
        self.assertEqual(info.saved["sp"].type.name, "sp")
        self.assertEqual(info.saved["fp"].type.name, "fp")
        self.assertEqual(sorted(self.inferior.reads), [(SP + 40, 4), (SP + 44, 4)])

    def test_framed_before_ra_is_saved(self):
        info = self.unwind(0x1008)
        self.assertEqual(self.saved(info), {"pc": 0x6660, "sp": SP + 48})
        self.assertEqual(self.inferior.reads, [])

    def test_ra_saved_but_s0_not_yet(self):
        info = self.unwind(0x1010)
        self.assertEqual(self.saved(info), {"pc": 0x5550, "sp": SP + 48})

    def test_before_the_allocation(self):
        for pc in (0x1000, 0x1004):
            info = self.unwind(pc)
            self.assertEqual(self.saved(info), {"pc": 0x6660, "sp": SP}, hex(pc))
            self.assertEqual(int(info.frame_id.sp), SP)

    # Suspected bug, u64ii_gdb_unwind.py line 81: `framed` compares addresses
    # (alloc < pc), not execution order. The shrink-wrapped early exit at
    # 0x1030 lies after the allocation but is reached by the branch at 0x1000
    # without it, so the unwinder adds 48 to sp and reads a stale "ra" from the
    # stack instead of using the live ra register.
    @unittest.expectedFailure
    def test_shrink_wrapped_exit_path_after_the_frame_code(self):
        info = self.unwind(0x1030)
        self.assertEqual(self.saved(info), {"pc": 0x6660, "sp": SP})

    # Same root cause (line 81): at the `ret` after `addi sp,sp,48` the frame
    # is already released and ra restored, but the unwinder still treats the
    # function as framed and unwinds through the caller's stack.
    @unittest.expectedFailure
    def test_after_the_epilogue_released_the_frame(self):
        info = self.unwind(0x1020)
        self.assertEqual(self.saved(info), {"pc": 0x6660, "sp": SP})

    def test_leaf_function(self):
        info = self.unwind(0x1004, listing=[(0x1000, "addi a0,a0,1"), (0x1004, "ret")])
        self.assertEqual(self.saved(info), {"pc": 0x6660, "sp": SP})

    def test_no_function_for_pc(self):
        self.arch = Arch(LISTING)
        frame = PendingFrame({"pc": 0x4242, "sp": SP, "ra": 0x6660}, self.arch)
        self.assertIsNone(uw.U64iiScanUnwinder()(frame))
        self.assertEqual(self.arch.calls, [])

    def test_pc_zero(self):
        self.assertIsNone(self.unwind(0, block=Block(0, 0x40, function="reset")))

    def test_unreadable_ra(self):
        self.assertIsNone(self.unwind(0x1004, regs={"ra": None}))

    def test_end_of_chain_return_addresses(self):
        self.assertIsNone(self.unwind(0x1000, regs={"ra": 0}))
        self.assertIsNone(self.unwind(0x1000, regs={"ra": 0xFFFFFFFF}))
        self.inferior.memory = memory_with(o44=0, o40=0x7770)
        self.assertIsNone(self.unwind(0x1014))

    def test_unframed_return_to_itself_is_refused(self):
        self.assertIsNone(self.unwind(0x1000, regs={"ra": 0x1000}))

    def test_framed_return_to_same_pc_still_unwinds(self):
        # Recursion: the caller's pc equals this one but its sp differs.
        self.inferior.memory = memory_with(o44=0x1014, o40=0x7770)
        info = self.unwind(0x1014)
        self.assertEqual(self.saved(info), {"pc": 0x1014, "sp": SP + 48, "fp": 0x7770})


if __name__ == "__main__":
    unittest.main()
