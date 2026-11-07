---
name: device-failure-triage
description: Decide whether a shared test device is really broken, and find the cause of a failure that happens only sometimes. Measure first, then change one thing. Use for "is the U64 wedged", "it fails 1 in 5 runs", "this test is flaky", "it only fails sometimes", "before I ask someone to power-cycle it", or when two probes of the same device disagree.
---

# Triage a device failure

A hand-rolled probe that agrees with what you already suspect is the usual cause of a false "wedged" report. Measure the probe, then the device, then the failure rate.

## Before declaring a device broken

1. Run the project's own health check and quote its output: `python3 tests/lib/health.py -H "$DEVICE_HOST" -c ping -c rest -c ftp -c telnet -c ident -c dma`. If your probe disagrees with it, your probe is wrong.
2. Do not call a U64 wedged from REST alone: check ping too. `machine:reset` makes REST unreachable for a while, so poll for 150 s first.
3. For a C64 Ultimate, `tooling/c64u_monitor.py` shows REST, video and console together. Its console FIFO keeps old boot text, so only text printed after it started is evidence.
4. Validate a new probe on a healthy device first, then change the thing it measures and see the reading move. Two probes that share a transport or a host name are one probe.
5. Check that you did not cause the state: a run killed halfway leaves menus open, locks held or settings changed. Use the project's own teardown before anything else.
6. Ask a person to power-cycle or reseat hardware only when you can name the two outcomes and what each means. A wedged U64 is examined first with JTAG gdb (`tooling/docs/agents/jtag-debug.md`).

## Rules for a failure that happens sometimes

1. Reproduce with the real failing test or command, unmodified. A simplified stand-in once passed 60 of 60 runs while the real test failed 15% of the time, because it took another branch.
2. Measure the failure rate before any fix. To trust a clean result you need `n` runs with `(1 - rate)^n < 0.01`: about 15 runs at 50%, 30 at 25%, 40 at 15% and over 100 at 5%.
3. Dump the real state at the moment of failure, and the same state on a pass. The difference is usually not what you guessed. Check that the device still holds its known-good invariants (`tooling/docs/agents/device-invariants.md`).
4. For each hypothesis, name the measurement that would disprove it and take it. Record the hypotheses that died.
5. Change one variable per experiment. Run the test alone before blaming concurrency. In an A/B comparison the two runs differ only by the fix.
6. A fix that does not move the rate is reverted, however reasonable it looks. Report both numbers, such as 6 of 24 before and 0 of 40 after.
7. Separate the causes in the report: this change, an existing defect it exposed, the test harness, the environment.

## Shared devices

Never touch a device while a run is in progress; contention looks like a defect. The tools take the device lock themselves, so never delete or bypass it. Look for processes left by earlier sessions before you start. Restore anything you changed on the device and verify the restore. A power cycle needs the current task to ask for it.
