Tested this on a Claw 8 AI+ (MS-1T52). Auto-TDP still doesn't run with this PR — two separate problems:

1. The rails check wants all three rails, but the Claw firmware only exposes `ppt_pl1_spl` and `ppt_pl2_sppt`. So `firmware-attr:msi-wmi-platform` wins selection and then reports `auto_tdp_safe=False`, and the `intel-rapl` path you added for this device never gets reached. The existing test passes only because the fixture creates a fake `ppt_pl3_fppt`.

2. `PowerReader` only reads `gpu_busy_percent`, which is amdgpu-only. On Intel it's always `None`, so `_qualify_gameplay()` returns False and the loop just holds at the seed wattage forever.

Both fixed on my fork, branch `claw-autotdp-fix` (commit fb01ee6): https://github.com/jesherman/panel-de-control/tree/claw-autotdp-fix — take either or both, they're independent.

With it applied on the device: `auto_tdp_safe` False→True, `gpu_busy` None→live, loop steps 17→13 W.

One thing to decide: PL1/PL2 writes on both `intel-rapl` and `intel-rapl-mmio` do regulate this chip (17.9→11.8 W under load), but the firmware rails still regulate with RAPL held high and the guard loop re-asserts them every ~2 s, so the effective ceiling is whichever is lower.
