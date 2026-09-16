Tested this on a Claw 8 AI+ (MS-1T52) with a game running. Auto-TDP doesn't step with this PR — two separate problems:

1. The rails check wants all three rails, but the Claw firmware only exposes `ppt_pl1_spl` and `ppt_pl2_sppt`. So `firmware-attr:msi-wmi-platform` wins selection and then reports `auto_tdp_safe=False`, and the `intel-rapl` path you added for this device never gets reached. The existing test passes only because the fixture creates a fake `ppt_pl3_fppt`.

2. `PowerReader` only reads `gpu_busy_percent`, which is amdgpu-only. On Intel it's always `None`, so `_qualify_gameplay()` returns False and the loop holds at the seed wattage forever.

Both fixed on my fork, branch `claw-autotdp-fix` (commit b498b7e): https://github.com/jesherman/panel-de-control/tree/claw-autotdp-fix — they're independent, take either or both.

The second one needs a note, because the obvious implementation is wrong twice over (I shipped both mistakes before testing against a real game):

- A process holds several fdinfo fds, but only the fd that submitted work reports cycles. A Proton game showed 7 fds: one at 89%, six at 0%. Aggregate per pid and keep the fd with the highest busy. Summing fds double-counts (I measured 108%); picking by total or first-seen gives ~0%.
- The raw ratio is already 0-100, so don't subtract an idle floor. I did, and since the floor got sampled while the GPU was busy it clamped real gameplay to 0% and the loop stalled.

With it applied, Spyro (Proton) running:

| check | before | after |
|---|---|---|
| `auto_tdp_safe` | False | True |
| `gpu_busy` | `None` | 93-94% |
| loop setpoint | pinned 17 W | probes 17 → 13 → 12 → 11 → 10 W |

It restores the profile value when the game loses focus, which looks right.

One thing to decide: PL1/PL2 writes on both `intel-rapl` and `intel-rapl-mmio` do regulate this chip (17.9→11.8 W under load), but the firmware rails still regulate with RAPL held high and the guard loop re-asserts them every ~2 s, so the effective ceiling is whichever is lower.
