#!/usr/bin/env bash
# capacity-fingerprint.sh — what this machine can run, and what it is doing now.
#
# Answers three questions:
#   1. What is this host?          CPU/arch/RAM/disk, and crucially whether VMs
#                                  get hardware virtualization or software emulation.
#   2. What is it doing?           platform + range consumption right now.
#   3. What can it hold?           derived range capacity, and the binding constraint.
#
# Read-only by default. --disk additionally writes a temp file to measure
# throughput; skip it on a busy production host.
#
# Usage:
#   ./scripts/capacity-fingerprint.sh [--disk] [--json]
set -uo pipefail

DISK_TEST=0; JSON=0
for a in "$@"; do
  case "$a" in
    --disk) DISK_TEST=1 ;;
    --json) JSON=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

# ---- measured per-unit costs -------------------------------------------------
# From benchmarking on pg-ec2 (r6i.4xlarge), 26-27 Aug 2026. Steady-state, not
# the configured caps: range_default_memory is 8 GiB, roughly 4x real Linux use,
# so sizing from the cap under-provisions a host about fourfold.
PLATFORM_RAM_GB=2.7        # 9 services + host dockerd
PLATFORM_DISK_GB=35        # images + volumes at rest
LINUX_RANGE_RAM_GB=2.0     # settles here after ~1 day
LINUX_RANGE_DISK_GB=4
WIN_RANGE_RAM_GB=5.5
WIN_RANGE_DISK_GB=21
POOL_MEMBER_RAM_GB=0.27
POOL_MEMBER_DISK_GB=3.8
DEPLOY_VCPU=4              # burst per concurrent deploy

# ---- host ---------------------------------------------------------------------
ARCH="$(uname -m)"
KERNEL="$(uname -r)"
OS="$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME" || uname -s)"
CPU_MODEL="$(grep -m1 '^model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//')"
[ -z "$CPU_MODEL" ] && CPU_MODEL="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo unknown)"
VCPU="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 0)"
if [ -r /proc/meminfo ]; then
  RAM_GB="$(awk '/MemTotal/{printf "%.0f", $2/1048576}' /proc/meminfo)"
  RAM_AVAIL_GB="$(awk '/MemAvailable/{printf "%.0f", $2/1048576}' /proc/meminfo)"
  DISK_TOTAL_GB="$(df -BG / | awk 'NR==2{gsub("G","",$2); print $2}')"
  DISK_FREE_GB="$(df -BG / | awk 'NR==2{gsub("G","",$4); print $4}')"
  HYPERVISOR="$(systemd-detect-virt 2>/dev/null || echo unknown)"
else
  # macOS / BSD
  RAM_GB="$(awk -v b="$(sysctl -n hw.memsize 2>/dev/null || echo 0)" 'BEGIN{printf "%.0f", b/1073741824}')"
  PAGE="$(sysctl -n hw.pagesize 2>/dev/null || echo 4096)"
  FREEP="$(vm_stat 2>/dev/null | awk -F: '/Pages free|Pages inactive|Pages speculative/{gsub(/[ .]/,"",$2); s+=$2} END{print s+0}')"
  RAM_AVAIL_GB="$(awk -v p="$FREEP" -v z="$PAGE" 'BEGIN{printf "%.0f", p*z/1073741824}')"
  DISK_TOTAL_GB="$(df -g / 2>/dev/null | awk 'NR==2{print $2}')"
  DISK_FREE_GB="$(df -g / 2>/dev/null | awk 'NR==2{print $4}')"
  HYPERVISOR="$(sysctl -n machdep.cpu.brand_string >/dev/null 2>&1 && echo bare-metal || echo unknown)"
fi

# ---- virtualization vs emulation ---------------------------------------------
# The single biggest determinant of what a Windows/QEMU range costs. With
# /dev/kvm the guest runs at near-native speed; without it QEMU falls back to
# TCG, which interprets guest instructions on the host CPU -- an order of
# magnitude slower and far more CPU-hungry.
if [ -e /dev/kvm ]; then
  ACCEL="kvm"; ACCEL_NOTE="hardware virtualization available"
elif [ "$(uname -s)" = "Darwin" ]; then
  if [ "$(sysctl -n kern.hv_support 2>/dev/null || echo 0)" = "1" ]; then
    ACCEL="hvf"; ACCEL_NOTE="Apple Hypervisor.framework available — native-arch guests accelerate, x86 guests do not"
  else
    ACCEL="tcg"; ACCEL_NOTE="no hypervisor support — QEMU VMs run under TCG software emulation"
  fi
elif grep -qE '(vmx|svm)' /proc/cpuinfo 2>/dev/null; then
  ACCEL="none-but-capable"; ACCEL_NOTE="CPU supports virtualization but /dev/kvm is absent — load the kvm module or enable nested virt"
else
  ACCEL="tcg"; ACCEL_NOTE="no hardware virtualization — QEMU VMs run under TCG software emulation"
fi

# Cross-architecture guest emulation: an x86 guest image on an arm64 host (or
# the reverse) is emulated even when KVM is present, because KVM cannot
# accelerate a foreign instruction set.
case "$ARCH" in
  x86_64|amd64) NATIVE_GUEST="linux/amd64"; FOREIGN_GUEST="linux/arm64" ;;
  aarch64|arm64) NATIVE_GUEST="linux/arm64"; FOREIGN_GUEST="linux/amd64" ;;
  *) NATIVE_GUEST="$ARCH"; FOREIGN_GUEST="(other)" ;;
esac

# ---- current consumption -----------------------------------------------------
PLATFORM_N=0; RANGE_N=0; POOL_N=0; USED_RAM_MB=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  STATS="$(docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>/dev/null || true)"
  if [ -n "$STATS" ]; then
    PLATFORM_N=$(echo "$STATS" | grep -cE '^pg-(api|worker|worker-deploy|db|redis|minio|registry|traefik|frontend)' || true)
    RANGE_N=$(echo "$STATS" | grep -cE '^pg-range-' || true)
    POOL_N=$(echo "$STATS" | grep -cE '^pg-pool-' || true)
    USED_RAM_MB=$(echo "$STATS" | awk -F'\t' '{split($3,a," / "); v=a[1];
      if (v ~ /GiB/) {gsub("GiB","",v); s+=v*1024}
      else if (v ~ /MiB/) {gsub("MiB","",v); s+=v}
      else if (v ~ /KiB/) {gsub("KiB","",v); s+=v/1024} } END{printf "%.0f", s}')
  fi
fi

# ---- disk throughput (opt-in) -------------------------------------------------
SEQ_WRITE="not measured"
if [ "$DISK_TEST" -eq 1 ]; then
  T="$(mktemp -p "${TMPDIR:-/tmp}" capfp.XXXXXX)"
  R=$(dd if=/dev/zero of="$T" bs=1M count=2048 oflag=direct 2>&1 | tail -1 || true)
  SEQ_WRITE="$(echo "$R" | grep -oE '[0-9.]+ [MG]B/s' | tail -1)"
  [ -z "$SEQ_WRITE" ] && SEQ_WRITE="measurement failed (O_DIRECT unsupported?)"
  rm -f "$T"
fi

# ---- derived capacity ---------------------------------------------------------
cap() { awk -v a="$1" -v b="$2" 'BEGIN{ if (b<=0) print 0; else printf "%d", int(a/b) }'; }
USABLE_RAM=$(awk -v t="$RAM_GB" -v p="$PLATFORM_RAM_GB" 'BEGIN{printf "%.1f", (t-p>0)?t-p:0}')
USABLE_DISK=$(awk -v f="$DISK_FREE_GB" 'BEGIN{printf "%.0f", f}')
LINUX_BY_RAM=$(cap "$USABLE_RAM" "$LINUX_RANGE_RAM_GB")
LINUX_BY_DISK=$(cap "$USABLE_DISK" "$LINUX_RANGE_DISK_GB")
WIN_BY_RAM=$(cap "$USABLE_RAM" "$WIN_RANGE_RAM_GB")
WIN_BY_DISK=$(cap "$USABLE_DISK" "$WIN_RANGE_DISK_GB")
LINUX_MAX=$(( LINUX_BY_RAM < LINUX_BY_DISK ? LINUX_BY_RAM : LINUX_BY_DISK ))
WIN_MAX=$(( WIN_BY_RAM < WIN_BY_DISK ? WIN_BY_RAM : WIN_BY_DISK ))
[ "$LINUX_BY_RAM" -lt "$LINUX_BY_DISK" ] && LINUX_BIND="RAM" || LINUX_BIND="disk"
MAX_CONCURRENT_DEPLOY=$(( VCPU / DEPLOY_VCPU )); [ "$MAX_CONCURRENT_DEPLOY" -lt 1 ] && MAX_CONCURRENT_DEPLOY=1

if [ "$JSON" -eq 1 ]; then
  printf '{\n'
  printf '  "host": {"arch":"%s","cpu":"%s","vcpu":%s,"ram_gb":%s,"ram_available_gb":%s,' "$ARCH" "$CPU_MODEL" "$VCPU" "$RAM_GB" "$RAM_AVAIL_GB"
  printf '"disk_total_gb":%s,"disk_free_gb":%s,"os":"%s","kernel":"%s","hypervisor":"%s"},\n' "$DISK_TOTAL_GB" "$DISK_FREE_GB" "$OS" "$KERNEL" "$HYPERVISOR"
  printf '  "virtualization": {"accel":"%s","note":"%s","native_guest":"%s","emulated_guest":"%s"},\n' "$ACCEL" "$ACCEL_NOTE" "$NATIVE_GUEST" "$FOREIGN_GUEST"
  printf '  "current": {"platform_services":%s,"ranges":%s,"pool_members":%s,"container_ram_mb":%s},\n' "$PLATFORM_N" "$RANGE_N" "$POOL_N" "$USED_RAM_MB"
  printf '  "disk_seq_write": "%s",\n' "$SEQ_WRITE"
  printf '  "capacity": {"linux_ranges":%s,"windows_ranges":%s,"binding":"%s","max_concurrent_deploys":%s}\n' "$LINUX_MAX" "$WIN_MAX" "$LINUX_BIND" "$MAX_CONCURRENT_DEPLOY"
  printf '}\n'
  exit 0
fi

line(){ printf '%s\n' "------------------------------------------------------------"; }
printf '\nPROVING GROUND — capacity fingerprint\n'; line
printf '%-22s %s\n' "Host"        "$(hostname)"
printf '%-22s %s\n' "OS / kernel" "$OS · $KERNEL"
printf '%-22s %s (%s)\n' "CPU"    "$CPU_MODEL" "$ARCH"
printf '%-22s %s vCPU\n' "Cores"  "$VCPU"
printf '%-22s %s GB total · %s GB available\n' "Memory" "$RAM_GB" "$RAM_AVAIL_GB"
printf '%-22s %s GB total · %s GB free\n' "Disk"  "$DISK_TOTAL_GB" "$DISK_FREE_GB"
printf '%-22s %s\n' "Runs on"     "$HYPERVISOR"
[ "$DISK_TEST" -eq 1 ] && printf '%-22s %s\n' "Seq write (O_DIRECT)" "$SEQ_WRITE"
line
printf 'VM execution\n'
printf '%-22s %s\n' "  Acceleration" "$ACCEL"
printf '%-22s %s\n' "  Meaning"      "$ACCEL_NOTE"
printf '%-22s %s runs natively; %s is emulated\n' "  Guest images" "$NATIVE_GUEST" "$FOREIGN_GUEST"
if [ "$ACCEL" != "kvm" ]; then
printf '%-22s %s\n' "  ⚠ Impact" "Windows/QEMU ranges cost several-fold more CPU and boot far slower"
fi
line
printf 'Right now\n'
printf '%-22s %s platform services · %s ranges · %s pool members\n' "  Running" "$PLATFORM_N" "$RANGE_N" "$POOL_N"
printf '%-22s %s MB across all containers\n' "  Container memory" "$USED_RAM_MB"
line
printf 'Capacity (steady state, from measured per-range cost)\n'
printf '%-22s %s   (%s GB each)\n' "  Linux ranges"   "$LINUX_MAX" "$LINUX_RANGE_RAM_GB"
printf '%-22s %s   (%s GB each)\n' "  Windows ranges" "$WIN_MAX"   "$WIN_RANGE_RAM_GB"
printf '%-22s %s\n' "  Binding constraint" "$LINUX_BIND"
printf '%-22s %s   (%s vCPU burst each)\n' "  Concurrent deploys" "$MAX_CONCURRENT_DEPLOY" "$DEPLOY_VCPU"
line
printf 'Sized from measured steady-state usage, not the 8 GiB range cap.\n'
printf 'Reserving at the cap under-provisions a host roughly fourfold.\n\n'
