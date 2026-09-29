"""gdb unwinder for the Ultimate 64 Elite II / C64 Ultimate RISC-V application.

Loaded by u64ii_gdb.sh (`source` in gdb). Neither of gdb's own unwinders gets
past the first frame of this firmware:
  - the call frame information GCC emits leaves the return address undefined
    and the frame address at sp+0 in shrink-wrapped functions, so gdb treats
    the innermost frame as the outermost one;
  - the RISC-V prologue analyser stops at the first branch, and shrink-wrapped
    functions branch before they allocate their frame.
This unwinder scans the whole function instead: it finds the frame allocation
(addi sp,sp,-N) and the saves of ra and s0 (sw ra,K(sp) / sw s0,K(sp)), and
recovers the caller's pc, sp and s0 from them.
"""

import re

import gdb
from gdb.unwinder import Unwinder, register_unwinder

ALLOC = re.compile(r"^addi\s+sp,\s*sp,\s*-(\d+)$")
SAVE = re.compile(r"^sw\s+(ra|s0),\s*(\d+)\(sp\)$")
LIMIT = 4096           # instructions scanned per function


class _FrameId:
    """gdb reads `sp` and `pc`; older gdb releases need them as gdb.Value."""

    def __init__(self, sp, pc, reg_type):
        self.sp = gdb.Value(sp).cast(reg_type)
        self.pc = gdb.Value(pc).cast(reg_type)


def _function_range(pc):
    try:
        block = gdb.block_for_pc(pc)
    except RuntimeError:
        return None
    while block is not None and block.function is None:
        block = block.superblock
    if block is None:
        return None
    return block.start, block.end


def _scan(arch, start, end):
    """(frame size, alloc address, {reg: (offset, save address)})."""
    size, alloc, saves = 0, None, {}
    for insn in arch.disassemble(start, end - 1, LIMIT):
        text = insn["asm"].strip()
        if alloc is None:
            m = ALLOC.match(text)
            if m:
                size, alloc = int(m.group(1)), insn["addr"]
            continue
        m = SAVE.match(text)
        if m and m.group(1) not in saves:
            saves[m.group(1)] = (int(m.group(2)), insn["addr"])
        if len(saves) == 2:
            break
    return size, alloc, saves


class U64iiScanUnwinder(Unwinder):
    def __init__(self):
        super().__init__("u64ii-riscv-scan")

    def __call__(self, pending):
        pc = int(pending.read_register("pc"))
        sp = int(pending.read_register("sp"))
        span = _function_range(pc)
        if span is None or pc == 0:
            return None
        start, end = span
        arch = pending.architecture()
        size, alloc, saves = _scan(arch, start, end)
        inferior = gdb.selected_inferior()

        def word(address):
            return int.from_bytes(bytes(inferior.read_memory(address, 4)), "little")

        framed = alloc is not None and alloc < pc
        caller_sp = sp + size if framed else sp
        if framed and "ra" in saves and saves["ra"][1] < pc:
            ra = word(sp + saves["ra"][0])
        else:
            try:
                ra = int(pending.read_register("ra"))
            except gdb.error:          # an outer frame whose ra nobody saved
                return None
        if ra in (0, 0xFFFFFFFF) or (ra == pc and caller_sp == sp):
            return None
        info = pending.create_unwind_info(
            _FrameId(caller_sp, start, pending.read_register("sp").type))
        info.add_saved_register("pc", gdb.Value(ra).cast(pending.read_register("pc").type))
        info.add_saved_register("sp", gdb.Value(caller_sp).cast(pending.read_register("sp").type))
        if framed and "s0" in saves and saves["s0"][1] < pc:
            s0 = word(sp + saves["s0"][0])
            info.add_saved_register("fp", gdb.Value(s0).cast(pending.read_register("fp").type))
        return info


register_unwinder(None, U64iiScanUnwinder(), replace=True)
