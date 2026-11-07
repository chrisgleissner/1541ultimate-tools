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

Whether the frame exists at the pc is decided by following the control flow
from the function's entry, not by comparing addresses: a shrink-wrapped early
exit lies after the allocation in address order but is reached without it, and
the `ret` after the epilogue's `addi sp,sp,N` is reached with the frame already
released. Only if the paths reaching the pc disagree, or the pc is not reached
from the entry at all, does the unwinder fall back to comparing addresses.
"""

import re

import gdb
from gdb.unwinder import Unwinder, register_unwinder

ALLOC = re.compile(r"^addi\s+sp,\s*sp,\s*-(\d+)$")
SAVE = re.compile(r"^sw\s+(ra|s0),\s*(\d+)\(sp\)$")
DEALLOC = re.compile(r"^addi\s+sp,\s*sp,\s*(\d+)$")
BRANCH = re.compile(r"^(?:c\.)?(?:beq|bne|blt|bge|bltu|bgeu|beqz|bnez|blez|bgez|bltz|bgtz|bgt|ble|bgtu|bleu)$")
# the target address, with or without 0x (gdb prints 0x1030, objdump 1030), and an optional <symbol+offset>
TARGET = re.compile(r"(?:^|,)\s*(?:0x)?([0-9a-fA-F]+)\s*(?:<[^>]*>)?\s*$")
LIMIT = 4096           # instructions scanned per function

# Frame state of a path from the function entry: allocated or not, ra and s0 saved or not.
NONE, ALLOCATED, RELEASED = 0, 1, 2


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


def _flow(arch, start, end, pc, saves):
    """The set of (frame, ra saved, s0 saved) states in which execution can reach `pc`.

    Follows branches, jumps and calls from the function's entry. Empty when `pc` is not reached."""
    insns = list(arch.disassemble(start, end - 1, LIMIT))
    index = {insn["addr"]: i for i, insn in enumerate(insns)}
    save_at = {address: reg for reg, (_offset, address) in saves.items()}
    seen, reach, work = set(), set(), [(0, (NONE, False, False))]
    while work:
        i, state = work.pop()
        if i >= len(insns) or (i, state) in seen:
            continue
        seen.add((i, state))
        insn = insns[i]
        if insn["addr"] == pc:
            reach.add(state)
        frame, ra, s0 = state
        text = insn["asm"].strip()
        mnemonic, _, operands = text.replace("\t", " ").partition(" ")
        operands = operands.strip()
        if ALLOC.match(text):
            frame = ALLOCATED if frame == NONE else frame
        elif DEALLOC.match(text) and frame == ALLOCATED:
            frame = RELEASED
        reg = save_at.get(insn["addr"])
        ra, s0 = ra or reg == "ra", s0 or reg == "s0"
        after = (frame, ra, s0)
        target = TARGET.search(operands)
        target = index.get(int(target.group(1), 16)) if target else None
        first = operands.split(",")[0].strip()
        if BRANCH.match(mnemonic):
            work.append((i + 1, after))
            if target is not None:
                work.append((target, after))
        elif mnemonic in ("j", "c.j") or (mnemonic == "jal" and first in ("zero", "x0")):
            if target is not None:
                work.append((target, after))
        elif mnemonic in ("ret", "jr", "c.jr", "tail", "mret", "sret", "uret") or \
                (mnemonic == "jalr" and first in ("zero", "x0")):
            pass
        else:                       # includes calls: execution continues after them
            work.append((i + 1, after))
    return reach


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

        # by address order, the fallback; the control flow decides when it is clear
        framed = alloc is not None and alloc < pc
        ra_saved = framed and "ra" in saves and saves["ra"][1] < pc
        s0_saved = framed and "s0" in saves and saves["s0"][1] < pc
        states = _flow(arch, start, end, pc, saves) if alloc is not None else set()
        frames = {state[0] for state in states}
        if frames == {ALLOCATED}:
            framed = True
            ra_saved = all(state[1] for state in states)
            s0_saved = all(state[2] for state in states)
        elif states and ALLOCATED not in frames:
            framed = ra_saved = s0_saved = False
        caller_sp = sp + size if framed else sp
        if ra_saved:
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
        if s0_saved:
            s0 = word(sp + saves["s0"][0])
            info.add_saved_register("fp", gdb.Value(s0).cast(pending.read_register("fp").type))
        return info


register_unwinder(None, U64iiScanUnwinder(), replace=True)
