# What to post — panel-de-control PR #647 (MSI Claw A2VM Auto-TDP)

Two things to say: the PR body (when opening the PR from the fork), and a short comment on #647.

Fork branch: `jesherman:claw-autotdp-fix` → base `codex/autotdp-next-recovered`
Commit: `fb01ee6`

---

## 1. PR title

```
fix(auto-tdp): reach Auto-TDP on the MSI Claw 8 AI+ A2VM
```

## 2. PR body

Physically validated the A2VM path on hardware (MSI Claw 8 AI+, MS-1T52, CachyOS). Auto-TDP does not engage even with this PR applied — two defects, both fixed here.

**1. `msi` shadows `intel-rapl`, then refuses auto.**

The Claw publishes only `ppt_pl1_spl` + `ppt_pl2_sppt`. `_auto_tdp_rails_ready()` required all three rails, so `firmware-attr:msi-wmi-platform` won selection and reported `auto_tdp_safe=False`, leaving the `intel-rapl` path added for this device unreachable:

```
selected backend : firmware-attr:msi-wmi-platform
_primary_rails   : ['pl1','pl2'] (len 2) vs _RAIL_ATTRS (len 3)
auto_tdp_safe    : False
```

`test_only_msi_claw_enables_dual_surface_rapl_auto_tdp` passes because `_mk_fw` fabricates `ppt_pl3_fppt`:

| fixture | selected | auto_tdp_safe |
|---|---|---|
| `_mk_fw` (3 rails) | firmware-attr:msi-wmi-platform | True |
| real Claw (pl1+pl2) | firmware-attr:msi-wmi-platform | **False** |
| no fw-attr node | intel-rapl | True |

Fixed with a per-device `optional_rails` set, declared by the factory for the DMI-verified A2VM only. PL1 stays mandatory, every non-optional rail must still be present, and every present rail must still be readable+writable. Other devices keep the existing strict 3-rail rule.

**2. `gpu_busy` is `None` on Intel, so the loop can never step.**

`PowerReader` only read `gpu_busy_percent`, an amdgpu node. With `gpu_busy=None`, `_qualify_gameplay()` returns False and the controller holds at its seed indefinitely:

```
no GPU%  -> setpoint 17..17 W  (awaiting_gameplay)
GPU%     -> setpoint 13..17 W  (steps down)
```

Utilisation is now derived from xe per-client engine cycles in procfs fdinfo (`drm-cycles-rcs` / `drm-total-cycles-rcs`), wired as a lazy fallback so AMD paths are unaffected. Raw fdinfo counts compositor work (~25–31% at idle), so `calibrate()` measures the idle floor and subtracts it; the 88/97 thresholds are untouched.

**Verified on hardware:**

| check | before | after |
|---|---|---|
| `auto_tdp_safe` | False | True |
| `gpu_busy` | `None` | 0–86% live |
| loop setpoint | pinned 17 W | steps 17 → 13 W |

Tests: `tests/test_power_intel.py`, `tests/test_claw_auto_tdp.py`. Existing partial-rails tests pass unchanged.

One thing worth deciding deliberately: on this device the firmware rails still regulate with RAPL held high, and the guard loop re-asserts them every ~2 s, so the effective ceiling is the lower of the two surfaces. Auto owning RAPL while manual/guard owns the firmware rails otherwise works by accident rather than design.

---

## 3. Comment on #647

Heads up — physically validated the A2VM path on hardware (MSI Claw 8 AI+, MS-1T52, CachyOS) and Auto-TDP doesn't engage even with this PR applied. Two defects, fixed in #<PR_NUMBER> (branch `jesherman:claw-autotdp-fix`):

**1. `msi` wins selection, then refuses auto.** The Claw publishes only `ppt_pl1_spl` + `ppt_pl2_sppt`, but `_auto_tdp_rails_ready()` required all three rails — so `firmware-attr:msi-wmi-platform` reported `auto_tdp_safe=False` and the `intel-rapl` path for this device was never reached. The existing test passes only because `_mk_fw` fabricates `ppt_pl3_fppt`.

**2. `gpu_busy` is `None` on Intel**, so `_qualify_gameplay()` returns False and the loop holds at its seed forever (`17..17 W` vs `13..17 W` with the signal present).

| check | before | after |
|---|---|---|
| `auto_tdp_safe` | False | True |
| `gpu_busy` | `None` | 0–86% live |
| loop setpoint | pinned 17 W | steps 17 → 13 W |

Also relevant: PL1/PL2 writes on both `intel-rapl` and `intel-rapl-mmio` do regulate this silicon (17.9 W → 11.8 W under load, restored cleanly), so the RAPL approach is sound — it just never got selected.

---

### Before posting

- Replace `#<PR_NUMBER>` in section 3 with the real PR number (or delete the parenthetical and link the branch).
- Both #647 and `codex/autotdp-next-recovered` are readable, so the links resolve.
