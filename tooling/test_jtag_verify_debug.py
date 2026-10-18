"""Debug policy and fault recovery tests, with no target hardware."""
import contextlib
import io
import itertools
import json
from pathlib import Path
import socket
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jtag_verify_debug as d
import jtag_verify as v


def frame(raw):
    return b"+$" + raw + b"#" + d.checksum(raw)


class Connection:
    def __init__(self, replies=None):
        self.incoming = bytearray()
        self.sent = []
        self.registers = [0] * 36
        self.registers[32] = 0x100
        self.registers[35] = 1
        self.replies = replies
        self.closed = False
        self.steps = 0
        self.bad_step = False

    def sendall(self, data):
        self.sent.append(data)
        if data == b"+":
            return
        if data == b"\x03":
            self.incoming.extend(frame(b"S02"))
            return
        packet = data[1:data.index(b"#")]
        if self.replies is not None:
            self.incoming.extend(self.replies.pop(0))
            return
        if packet == b"?" or packet == b"s":
            if packet == b"s":
                self.registers[2] += 2 if self.bad_step else 1
                self.registers[32] += 4
                self.steps += 1
            reply = b"S05"
        elif packet == b"g":
            reply = b"".join(r.to_bytes(4, "little").hex().encode() for r in self.registers)
        elif packet.startswith(b"m"):
            # addi r2,r2,1; after two steps the next instruction is a store,
            # which must be rejected by the step policy without sending 's'.
            instruction = 0x10800044 if self.steps < 2 else 0x10800015
            reply = (bytes(range(256)) if packet.endswith(b",100") else
                     instruction.to_bytes(4, "little")).hex().encode()
        elif packet == b"c":
            self.incoming.extend(b"+")
            return
        else:
            raise AssertionError(f"prohibited packet {packet!r}")
        self.incoming.extend(frame(reply))

    def recv(self, count):
        reply = bytes(self.incoming[:count])
        del self.incoming[:count]
        return reply

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True


class PolicyTest(unittest.TestCase):
    def test_instruction_oracle_and_unknown_ops(self):
        regs = [0] * 33
        regs[1], regs[2], regs[32] = 10, 20, 0x100
        def ins(op, a=1, b=2, immediate=1):
            return a << 27 | b << 22 | (immediate & 0xFFFF) << 6 | op
        self.assertEqual(d.predict(ins(4, immediate=-1), regs)[2], 9)
        self.assertEqual(d.predict(ins(4, b=0), regs)[0], 0)
        self.assertEqual(d.predict(ins(0x34), regs)[2], 0x1000a)
        for op, taken in [(6, True), (0x26, False), (0x2e, False)]:
            self.assertEqual(d.predict(ins(op, immediate=8), regs)[32], 0x10c if taken else 0x104)
        regs[1] = 20
        for op in (0x26, 0x2e):
            self.assertEqual(d.predict(ins(op, immediate=-4), regs)[32], 0x100)
        for op in set(range(64)) - {4, 0x34, 6, 0x26, 0x2e}:
            self.assertIsNone(d.predict(ins(op), regs))
        for address, count in [(0, 4), (0x21, 4), (d.RAM_END, 4), (0x100, 0), (1.5, 4)]:
            with self.assertRaises(d.DebugError):
                d.ram(address, count)

    @patch.object(d, "STEPPING_QUALIFIED", True)
    def test_steps_compare_full_general_register_file(self):
        connection = Connection()
        session = d.DebugSession(connection)
        session.stopped()
        self.assertEqual(session.checked_step()['pc_after'], '0x00000104')
        self.assertIsNotNone(session.checked_step())
        self.assertIsNone(session.checked_step())
        session.resume()
        self.assertTrue(session.resumed)
        self.assertEqual(connection.steps, 2)
        connection = Connection()
        connection.registers[35] = 0
        self.assertIsNone(d.DebugSession(connection).checked_step())
        self.assertEqual(connection.steps, 0)
        self.assertFalse(any(x.startswith(b"$m") for x in connection.sent))
        connection = Connection()
        connection.bad_step = True
        with self.assertRaisesRegex(d.DebugError, "oracle mismatch"):
            d.DebugSession(connection).checked_step()

    def test_reply_checks_and_decoding(self):
        for raw, message in [(b"", "connection closed"), (b"-", "acknowledgement"),
                             (b"+$S05#00", "checksum"), (frame(b"}"), "escape"),
                             (frame(b"*a"), "repeat"), (frame(b"a*"), "repeat"),
                             (frame(b"a*\x10"), "repeat"), (b"+$" + b"a" * 4097, "oversized")]:
            with self.subTest(message=message), self.assertRaisesRegex(d.DebugError, message):
                d.DebugSession(Connection([raw])).stopped()
        # Escape and run-length decoding are independent of the wire checksum.
        session = d.DebugSession(Connection([frame(b"S0}5")]))
        with self.assertRaisesRegex(d.DebugError, "unexpected debug stop"):
            session.stopped()
        session = d.DebugSession(Connection([frame(b"0*~" * 43)]))
        with self.assertRaisesRegex(d.DebugError, "oversized decoded"):
            session.stopped()
        session = d.DebugSession(Connection([frame(b"S05")] ))
        with patch.object(d.time, "monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(d.DebugError, "deadline"):
                session.stopped()

    @patch.object(d, "STEPPING_QUALIFIED", True)
    def test_malformed_registers_and_code(self):
        for reply, message in [(b"", "incomplete"), (b"x" * 288, "invalid Nios"),
                               (b"01" + b"00" * 143, "zero register"),
                               (b"00" * 140 + b"xxxxxxxx", "pre-debug status")]:
            with self.assertRaisesRegex(d.DebugError, message):
                d.DebugSession(Connection([frame(reply)])).registers()
        for reply, message in [(b"xx", "invalid instruction"), (b"00", "incomplete instruction")]:
            conn = Connection()
            registers = b"".join(r.to_bytes(4, "little").hex().encode() for r in conn.registers)
            conn.replies = [frame(registers), frame(reply)]
            with self.assertRaisesRegex(d.DebugError, message):
                d.DebugSession(conn).checked_step()
        conn = Connection()
        registers = b"".join(r.to_bytes(4, "little").hex().encode() for r in conn.registers)
        conn.replies = [frame(registers), frame(b"44008010"), frame(b"E01")]
        with self.assertRaisesRegex(d.DebugError, "did not trap"):
            d.DebugSession(conn).checked_step()
        with self.assertRaisesRegex(d.DebugError, "resume"):
            d.DebugSession(Connection([b"-"])).resume()


class RunTest(unittest.TestCase):
    def test_health_and_tools_validation(self):
        for host in ("", "u64/path", "u64:80", "user@host"):
            with self.assertRaises(d.DebugError):
                d.health(host)
        for info in ({'product':'C64 Ultimate'}, {'product':'Ultimate 64 Elite', 'errors':['bad']}):
            with patch.object(d.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(info).encode())):
                with self.assertRaises(d.DebugError):
                    d.health('u64')
        with patch.object(d.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"product":"Ultimate 64 Elite"}')):
            self.assertEqual(d.health('u64')['product'], 'Ultimate 64 Elite')
        with self.assertRaises(d.DebugError):
            d.tools('/nonexistent-intel')

    def test_tool_paths_and_listener_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory) / 'quartus/bin'
            bin_dir.mkdir(parents=True)
            (bin_dir / 'nios2-gdb-server').touch()
            (bin_dir / 'jtagconfig').touch()
            (bin_dir.parent / 'version.txt').write_text('Version=18.1.0.625\n')
            self.assertEqual(d.tools(directory)[0], bin_dir / 'nios2-gdb-server')
            (bin_dir.parent / 'version.txt').write_text('Version=other\n')
            with self.assertRaisesRegex(d.DebugError, 'unqualified'):
                d.tools(directory)
        conn = Connection()
        with patch.object(d.socket, 'create_connection', side_effect=[ConnectionRefusedError(), conn]), \
                patch.object(d.time, 'sleep'):
            self.assertIs(d.connect_ready(1234), conn)
        with patch.object(d.socket, 'create_connection', side_effect=ConnectionRefusedError()), \
                patch.object(d.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(d.DebugError, 'listener'):
                d.connect_ready(1234)

    @contextlib.contextmanager
    def fixture(self, banner="Pausing target processor: OK\n", listing=None):
        adapter = types.SimpleNamespace(kind='blaster', vid=0x9fb, pid=0x6001, bus=1, ports=[6])
        processes, connections = [], []
        def start(argv, stdout, stderr):
            self.assertEqual(argv[:4], ['/usr/bin/stdbuf', '-oL', '-eL', '/intel/server'])
            self.assertEqual(argv[4:8], ['--cable', 'USB-Blaster [1-6]', '--device', '1'])
            for prohibited in ('--reset-target', '--init-cache', '--go', '--stop'):
                self.assertNotIn(prohibited, argv)
            stdout.write(banner)
            stdout.flush()
            process = Mock()
            process.poll.return_value = None
            process.wait.return_value = 0
            processes.append(process)
            return process
        def connect(*args, **kwargs):
            conn = Connection()
            connections.append(conn)
            return conn
        with patch.object(d, 'STEPPING_QUALIFIED', True), \
                patch.object(d, 'tools', return_value=(Path('/intel/server'), Path('/intel/config'))), \
                patch.object(d.subprocess, 'run', return_value=types.SimpleNamespace(stdout=listing if listing is not None else '1) USB-Blaster [1-6]\n  02B050DD FPGA\n')), \
                patch.object(d.subprocess, 'Popen', side_effect=start), \
                patch.object(d.socket, 'create_connection', side_effect=connect), \
                patch.object(d, 'health', return_value={'product':'Ultimate 64 Elite'}):
            yield adapter, connections, processes

    def test_unavailable_status_cannot_enable_live_stepping(self):
        for status in (0, 1, 0xFFFFFFFF):
            conn = Connection()
            conn.registers[35] = status
            self.assertIsNone(d.DebugSession(conn).checked_step())
            self.assertEqual(conn.sent, [])
        with self.fixture() as (adapter, connections, _), patch.object(d, 'STEPPING_QUALIFIED', False):
            result = d.run(adapter, '/intel', 'u64', 1, 1)
        self.assertEqual(result['status'], 'UNVERIFIED')
        self.assertEqual(result['steps'], [])
        self.assertFalse(any(x.startswith(b'$s#') for x in connections[0].sent))
        with patch.object(d, 'STEPPING_QUALIFIED', True):
            conn = Connection()
            conn.registers[35] = 0xFFFFFFFF
            self.assertIsNone(d.DebugSession(conn).checked_step())
            self.assertEqual(conn.steps, 0)

    def test_run_success_and_insufficient_step_count(self):
        with self.fixture() as (adapter, connections, processes):
            result = d.run(adapter, '/intel', 'u64', 3, 6)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['stable_register_reads'], 36)
        self.assertEqual(len(result['steps']), 6)
        self.assertTrue(all(c.closed for c in connections))
        self.assertEqual(len(processes), 3)
        for process in processes:
            process.terminate.assert_not_called()
        with self.fixture() as (adapter, _, _):
            self.assertEqual(d.run(adapter, '/intel', 'u64', 1, 3)['status'], 'UNVERIFIED')

    def test_unsupported_identity_or_cable_never_starts_server(self):
        with self.fixture() as (adapter, _, processes):
            adapter.kind = 'ft232h'
            with self.assertRaises(d.DebugError):
                d.run(adapter, '/intel', 'u64', 1, 1)
            self.assertEqual(processes, [])
        for listing in ('', '1) USB-Blaster [1-6]', '1) USB-Blaster [1-6]\n  DEADBEEF FPGA'):
            with self.fixture(listing=listing) as (adapter, _, processes):
                with self.assertRaises(d.DebugError):
                    d.run(adapter, '/intel', 'u64', 1, 1)
                self.assertEqual(processes, [])

    def test_initially_paused_cpu_is_not_resumed(self):
        with self.fixture(banner='Processor is already paused\n') as (adapter, connections, processes):
            with self.assertRaisesRegex(d.DebugError, 'already paused'):
                d.run(adapter, '/intel', 'u64', 1, 1)
            self.assertEqual(connections, [])
            processes[0].terminate.assert_called_once()

    def test_error_paths_still_detach_and_close(self):
        with self.fixture() as (adapter, connections, processes), \
                patch.object(d.DebugSession, 'checked_step', side_effect=d.DebugError('step failed')):
            with self.assertRaisesRegex(d.DebugError, 'step failed'):
                d.run(adapter, '/intel', 'u64', 1, 1)
            self.assertTrue(connections[0].closed)
            self.assertTrue(any(x.startswith(b'$c#') for x in connections[0].sent))
            processes[0].terminate.assert_not_called()
        with self.fixture() as (adapter, connections, processes), \
                patch.object(d.DebugSession, 'resume', side_effect=d.DebugError('resume failed')):
            with self.assertRaisesRegex(d.DebugError, 'resume failed'):
                d.run(adapter, '/intel', 'u64', 1, 1)
            self.assertTrue(connections[0].closed)
            processes[0].terminate.assert_called_once()
        with self.fixture(banner='') as (adapter, connections, processes), \
                patch.object(d.time, 'monotonic', side_effect=itertools.chain([0, 0, 0, 10], itertools.repeat(10))):
            with self.assertRaisesRegex(d.DebugError, 'running-to-paused'):
                d.run(adapter, '/intel', 'u64', 1, 1)
            self.assertTrue(connections[0].closed)

    def test_budget_and_health_failures(self):
        fake_session = Mock(resumed=True)
        fake_session.registers.return_value = [0] * 33
        fake_session.checked_memory.return_value = {}
        with self.fixture() as (adapter, _, _), \
                patch.object(d, 'DebugSession', return_value=fake_session), \
                patch.object(d.time, 'monotonic', side_effect=[0, 0, 0, 0, 1, 1, 1]):
            result = d.run(adapter, '/intel', 'u64', 1, 1)
            self.assertEqual(result['stable_register_reads'], 2)
        with self.fixture() as (adapter, _, _), \
                patch.object(d.DebugSession, 'registers', side_effect=[[0] * 33, [1] * 33]):
            with self.assertRaisesRegex(d.DebugError, 'registers changed'):
                d.run(adapter, '/intel', 'u64', 1, 1)
        with self.fixture() as (adapter, _, _), \
                patch.object(d, 'health', side_effect=[{}, {'changed': True}, {}]):
            with self.assertRaisesRegex(d.DebugError, 'matching health'):
                d.run(adapter, '/intel', 'u64', 1, 1)

    def test_memory_consistency_and_remote_interrupt_policy(self):
        conn = Connection()
        session = d.DebugSession(conn)
        self.assertEqual(session.checked_memory()['compared_bytes'], 256)
        with self.assertRaisesRegex(d.DebugError, 'running session'):
            session.interrupt()
        session.resume()
        session.interrupt()
        self.assertFalse(session.resumed)
        session.resume()
        conn.incoming.extend(frame(b'E01'))
        with self.assertRaisesRegex(d.DebugError, 'did not stop'):
            session.interrupt()
        conn = Connection()
        registers = b''.join(r.to_bytes(4, 'little').hex().encode() for r in conn.registers)
        conn.replies = [frame(registers), frame(b'00' * 256), frame(b'01' * 256)]
        with self.assertRaisesRegex(d.DebugError, 'code-memory'):
            d.DebugSession(conn).checked_memory()
        with self.fixture() as (adapter, _, _), \
                patch.object(d, 'health', side_effect=[{}, {}, {'changed': True}]):
            with self.assertRaisesRegex(d.DebugRunError, 'identity/version changed') as raised:
                d.run(adapter, '/intel', 'u64', 1, 1)
            self.assertIn('Pausing target processor', raised.exception.evidence['server_log'])
            self.assertIn('identity/version changed', raised.exception.evidence['exception_chain'])

    def test_slow_server_process_is_killed_after_detach(self):
        original_mock = Mock
        def process_mock(*args, **kwargs):
            process = original_mock(*args, **kwargs)
            process.poll.return_value = None
            process.wait.side_effect = [d.subprocess.TimeoutExpired('server', 2), d.subprocess.TimeoutExpired('server', 3), 0]
            return process
        with self.fixture() as (adapter, connections, processes), \
                patch(__name__ + '.Mock', side_effect=process_mock):
            d.run(adapter, '/intel', 'u64', 1, 1)
            processes[0].kill.assert_called_once()
            self.assertTrue(connections[0].closed)


if __name__ == '__main__':
    unittest.main()
