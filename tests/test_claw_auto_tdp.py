"""Regression tests for Auto-TDP on the MSI Claw 8 AI+ (A2VM) — PR #647's target device.

Reproduces the exact failure found on real hardware (MS-1T52, verified 2026-09-16)
and locks in the fix.

THE BUG: PR #647 routes Claw auto-TDP through Intel RAPL, but backend selection
hands the device to `firmware-attr:msi-wmi-platform` (first in the Intel chain).
That backend required ALL THREE rails (PL1+PL2+PL3); the Claw's firmware publishes
ONLY `ppt_pl1_spl` + `ppt_pl2_sppt`. Result: `auto_tdp_safe=False` ->
`_auto_tdp_supported()=False` -> Auto-TDP silently unavailable on the one device
the PR was written for. Upstream CI missed it because its fixture fabricates a
third rail.

THE FIX: the factory declares PL3 optional for a DMI-verified A2VM, so the
sustained rail the loop actually regulates decides auto-eligibility. A separate
regression pins that NON-Claw devices keep the strict 3-rail behaviour.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from device_profiles import GENERIC  # noqa: E402

import test_tdp_factory as T  # noqa: E402  (same-directory test helpers)


def mk_real_claw_fw(root, driver="msi-wmi-platform", pl1=30, pl2=37):
    """The REAL Claw rail set: pl1 + pl2 only. No ppt_pl3_fppt exists."""
    base = os.path.join(root, "sys/class/firmware-attributes", driver, "attributes")
    for attr, mx in (("ppt_pl1_spl", pl1), ("ppt_pl2_sppt", pl2)):
        d = os.path.join(base, attr)
        os.makedirs(d, exist_ok=True)
        for name, value in (("current_value", 15), ("min_value", 8), ("max_value", mx)):
            with open(os.path.join(d, name), "w") as handle:
                handle.write(str(value))


def claw_root(tmp_path, with_rapl=True):
    root = str(tmp_path)
    if with_rapl:
        T._mk_rapl(root)                              # mmio
        T._mk_rapl(root, "intel-rapl/intel-rapl:0")   # msr
    T._mk_dmi(root, "Micro-Star International Co., Ltd.", "Claw 8 AI+ A2VM")
    return root


def select(root):
    return T.select_backend(
        T._p("msi_claw_8_ai_plus"),
        root=root,
        ryzenadj_resolve=T._NO_RYZENADJ,
    )


# --------------------------------------------------------------- the fix


def test_real_claw_rails_enable_auto_tdp(tmp_path):
    """A2VM with its real 2-rail firmware must be auto-capable (the fix)."""
    root = claw_root(tmp_path)
    mk_real_claw_fw(root)

    backend = select(root)

    assert backend.name == "firmware-attr:msi-wmi-platform"
    assert backend.auto_tdp_safe is True, (
        "Auto-TDP must be available on the Claw 8 AI+ with its real rail set"
    )


def test_real_claw_reports_pl1_and_pl2_limits(tmp_path):
    """The rails the loop can actually drive are exposed, with pl1/pl2 both present."""
    root = claw_root(tmp_path)
    mk_real_claw_fw(root)
    backend = select(root)

    limits = backend.get_limits()
    assert limits.min_w == 8
    assert limits.max_w >= 30

    # Both driveable rails are advertised. `level_limits` reports the device
    # profile's static envelope for a recognised device (the live firmware
    # envelope is folded in at write time by `_clamp_live`), so assert presence
    # and a sane floor rather than a specific ceiling.
    caps = backend.level_limits()
    assert "pl1" in caps and "pl2" in caps
    assert caps["pl1"]["min"] >= 8
    assert caps["pl1"]["max"] >= 30
    assert caps["pl2"]["max"] >= 30


def test_claw_auto_path_is_gated_on_exact_identity(tmp_path):
    """A different product name must NOT get the optional-rail relaxation."""
    root = str(tmp_path)
    T._mk_rapl(root)
    T._mk_rapl(root, "intel-rapl/intel-rapl:0")
    # Right board family, wrong product string (e.g. a future Claw revision).
    T._mk_dmi(root, "Micro-Star International Co., Ltd.", "Claw 8 AI+")
    mk_real_claw_fw(root)

    backend = select(root)

    assert backend.auto_tdp_safe is False, (
        "only the DMI-verified A2VM may relax the rail requirement"
    )


def test_non_claw_firmware_keeps_strict_three_rail_rule(tmp_path):
    """ASUS/other firmware with a missing rail must still refuse auto."""
    root = str(tmp_path)
    T._mk_fw(root, "asus-armoury")   # creates pl1+pl2+pl3 ...
    T._mk_dmi(root, "ASUSTeK COMPUTER INC.", "ROG Ally X RC71L")

    rail_dir = os.path.join(
        root, "sys/class/firmware-attributes/asus-armoury/attributes/ppt_pl3_fppt"
    )
    for leaf in ("current_value", "min_value", "max_value"):
        os.remove(os.path.join(rail_dir, leaf))
    os.rmdir(rail_dir)

    backend = T.select_backend(
        T._p("rog_ally_x"), root=root, ryzenadj_resolve=T._NO_RYZENADJ
    )

    assert backend.auto_tdp_safe is False


def test_claw_still_works_with_all_three_rails(tmp_path):
    """If a future firmware adds pl3, auto stays on (no regression)."""
    root = claw_root(tmp_path)
    T._mk_fw(root, "msi-wmi-platform")   # full 3-rail set

    backend = select(root)

    assert backend.auto_tdp_safe is True


def test_claw_without_pl2_fails_closed(tmp_path):
    """PL2 is genuinely required: only PL3 was made optional."""
    root = claw_root(tmp_path)
    base = os.path.join(root, "sys/class/firmware-attributes", "msi-wmi-platform", "attributes")
    d = os.path.join(base, "ppt_pl1_spl")
    os.makedirs(d, exist_ok=True)
    for name, value in (("current_value", 15), ("min_value", 8), ("max_value", 30)):
        with open(os.path.join(d, name), "w") as handle:
            handle.write(str(value))

    backend = select(root)

    assert backend.auto_tdp_safe is False


def test_claw_readonly_rail_fails_closed(tmp_path):
    """A present-but-unwritable rail must still disqualify auto."""
    root = claw_root(tmp_path)
    mk_real_claw_fw(root)
    pl2 = os.path.join(
        root,
        "sys/class/firmware-attributes/msi-wmi-platform/attributes/ppt_pl2_sppt/current_value",
    )
    os.chmod(pl2, 0o444)
    try:
        backend = select(root)
        assert backend.auto_tdp_safe is False
    finally:
        os.chmod(pl2, 0o644)
