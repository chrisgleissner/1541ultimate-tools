#!/usr/bin/env bash
# verify.sh — post-build artifact sanity checks and post-deploy REST verification

verify_build_artifact() {
    local target=$1
    local elf="" min_size=0 artifact artifact_size magic

    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "Would verify the ${target} build artifact."
        return 0
    fi

    if jtag_app_only "$target"; then
        local image
        image="$REPO_DIR/$(target_jtag_image "$target")"
        elf="${image%.*}.elf"
        CURRENT_ACTION="Verifying ${target} JTAG application"
        if [ ! -s "$image" ] || [ ! -s "$elf" ]; then
            log_error "${target} JTAG application is missing: ${image}"
            CURRENT_ACTION=""; return 1
        fi
        magic=$(xxd -p -l 4 "$elf" 2>/dev/null || true)
        if [ "$magic" != "7f454c46" ]; then
            log_error "ELF ${elf} has invalid magic: ${magic} (expected 7f454c46)."
            CURRENT_ACTION=""; return 1
        fi
        log_info "${target} JTAG application OK: ${image}"
        log_success "${target} JTAG application verified."
        CURRENT_ACTION=""; return 0
    fi

    case "$target" in
        u64)
            elf="$REPO_DIR/target/u64/nios2/ultimate/result/ultimate.elf"
            min_size=$((512 * 1024))
            ;;
        u64ii)
            elf="$REPO_DIR/target/u64ii/riscv/ultimate/result/ultimate.elf"
            min_size=$((512 * 1024))
            ;;
        u2)
            elf="$REPO_DIR/target/u2/riscv/ultimate/result/ultimate.elf"
            min_size=$((256 * 1024))
            ;;
        u2plus)
            elf="$REPO_DIR/target/u2plus/nios/ultimate/result/ultimate.elf"
            min_size=$((256 * 1024))
            ;;
        u2pl)
            elf="$REPO_DIR/target/u2plus_L/riscv/ultimate/result/ultimate.elf"
            min_size=$((256 * 1024))
            ;;
        *) return 0 ;;
    esac

    # The file this run produced: update_<version>.<ext>, never an older one.
    artifact=$(target_artifact_path "$target")
    CURRENT_ACTION="Verifying build artifact for ${target}"

    if [ -z "$artifact" ] || [ ! -f "$artifact" ]; then
        log_error "No artifact found for ${target}."
        CURRENT_ACTION=""; return 1
    fi

    artifact_size=$(wc -c <"$artifact" | tr -d ' ')
    if [ "$artifact_size" -lt "$min_size" ]; then
        log_error "Artifact ${artifact} is too small (${artifact_size} bytes, expected >= ${min_size})."
        CURRENT_ACTION=""; return 1
    fi
    log_info "Artifact ${artifact}: ${artifact_size} bytes OK"

    if [ -n "$elf" ] && [ -f "$elf" ]; then
        magic=$(xxd -p -l 4 "$elf" 2>/dev/null || true)
        if [ "$magic" != "7f454c46" ]; then
            log_error "ELF ${elf} has invalid magic: ${magic} (expected 7f454c46)."
            CURRENT_ACTION=""; return 1
        fi
        log_info "ELF magic OK: ${elf}"
    fi

    log_success "Build artifact verified for ${target}."
    CURRENT_ACTION=""; return 0
}

target_verify_host() {
    # Device hostnames for the post-deploy REST health check are deliberately
    # not hardcoded. Set these environment variables (for example in
    # .build-tool.env), or pass --verify-host, to enable the check:
    #   U64_VERIFY_HOST=my-u64
    #   U64II_VERIFY_HOST=my-c64u
    # With neither set, verify_deployment logs "No verify host known" and skips
    # the check instead of failing the run.
    case "$1" in
        u64)   printf '%s' "${U64_VERIFY_HOST:-}" ;;
        u64ii) printf '%s' "${U64II_VERIFY_HOST:-}" ;;
        *) return 1 ;;
    esac
}

verify_deployment() {
    local target=$1
    local host="${VERIFY_HOST}"

    [ -z "$host" ] && host=$(target_verify_host "$target" 2>/dev/null || true)
    if [ -z "$host" ]; then
        log_warn "No verify host known for ${target}, skipping post-deploy verification."
        return 0
    fi
    if [ "$DRY_RUN" -eq 1 ]; then
        log_info "Dry run: skipping post-deploy verification against ${host}."
        return 0
    fi

    require_command curl
    require_command python3

    CURRENT_ACTION="Verifying deployment on ${host}"
    local python_verify
    python_verify=$(cat <<'PYEOF'
import json, sys, time, urllib.request, urllib.error

host = sys.argv[1]
base = f"http://{host}"
errors = []
checks_passed = 0

def api_get_json(path, timeout=10):
    url = f"{base}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def readmem(address, length, timeout=10):
    url = f"{base}/v1/machine:readmem?address={address:04X}&length={length}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ct = r.headers.get("Content-Type", "")
        raw = r.read()
        if "json" in ct:
            obj = json.loads(raw)
            data = obj.get("data", [])
            if isinstance(data, str):
                import base64
                return list(base64.b64decode(data))
            return data
        return list(raw)

# 1. Wait for firmware REST API
print("  Waiting for firmware REST API...")
info = None
for attempt in range(30):
    try:
        info = api_get_json("/v1/info")
        break
    except Exception:
        time.sleep(1)
if info is None:
    print("  FAIL: Device REST API did not respond within 30 seconds", file=sys.stderr)
    sys.exit(1)

product = info.get("product", "unknown")
fw = info.get("firmware_version", "unknown")
print(f"  Device: {product}, firmware: {fw}")
checks_passed += 1

# 2. Reset C64 core
try:
    print("  Resetting C64 core...")
    req = urllib.request.Request(f"{base}/v1/machine:reset", method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
except Exception as e:
    errors.append(f"Machine reset failed: {e}")

# 3. Wait for C64 READY prompt (screen codes R=0x12 E=0x05 A=0x01 D=0x04 Y=0x19)
ready = [0x12, 0x05, 0x01, 0x04, 0x19]
print("  Waiting for C64 boot (READY prompt)...")
found_ready = False
for attempt in range(15):
    try:
        data = readmem(0x0400, 1000)
        if any(data[i:i+5] == ready for i in range(len(data) - 4)):
            found_ready = True
            break
    except Exception:
        pass
    time.sleep(1)

if found_ready:
    print("  Screen RAM: READY prompt found")
    checks_passed += 1
else:
    errors.append("C64 boot screen 'READY.' not found in screen RAM within 15 seconds")

# 4. VIC raster line advancing
try:
    d1 = readmem(0xD012, 1)
    d2 = readmem(0xD012, 1)
    if d1 != d2:
        print(f"  VIC raster: changed ({d1} -> {d2})")
        checks_passed += 1
    else:
        errors.append(f"VIC raster line did not change between reads (both {d1})")
except Exception as e:
    errors.append(f"VIC raster read failed: {e}")

# 5. Jiffy clock advancing
try:
    d1 = readmem(0x00A0, 3)
    d2 = d1
    for attempt in range(120):
        time.sleep(0.050)
        d2 = readmem(0x00A0, 3)
        if d1 != d2:
            print(f"  Jiffy clock: advanced ({d1} -> {d2})")
            checks_passed += 1
            break
    else:
        errors.append(f"Jiffy clock did not advance within 6 seconds ({d1} -> {d2})")
except Exception as e:
    errors.append(f"Jiffy clock read failed: {e}")

total = checks_passed + len(errors)
print(f"  Result: {checks_passed}/{total} checks passed")
if errors:
    for err in errors:
        print(f"  FAIL: {err}", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
PYEOF
)

    log_info "Running post-deploy verification against ${host}..."
    if ! python3 -c "$python_verify" "$host"; then
        VERIFY_FAILED=1
        log_error "Post-deploy verification failed for ${target}."
        CURRENT_ACTION=""; return 1
    fi

    log_success "Post-deploy verification passed for ${target}."
    CURRENT_ACTION=""; return 0
}
