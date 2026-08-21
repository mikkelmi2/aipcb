"""Signal-integrity simulation: slices, the solver that eats them, and the verdict.

The path is: a routed board goes in, one *slice* per differential pair comes out --
a small self-contained board carrying that pair, whatever else is nearby, and four
simulation ports -- and each slice is handed to openEMS through Antmicro's
gerber2ems, running in a pinned container. What comes back is S-parameters, which
:mod:`aipcb.si.results` turns into the same findings every other check emits.

Why a slice and not the board: full-board FDTD is not a thing anyone does. Phase 0
measured the 60 x 30 mm ``diff-pair`` board at 380,640 cells against 105,000 for the
same pair's slice, and that ratio grows with board area while the answer does not
change -- the fields that decide a pair's impedance live within a millimetre of it.

The honesty clause, which belongs at the top of this package rather than in a
footnote: **this validates the layout, not the board.** Every number here is
computed from the stackup the *source* declares. A fabricator who presses a
different prepreg builds a different impedance, and no simulation of ours will know.
Impedance coupons and a hardware measurement are still the things that decide.
"""

from __future__ import annotations

__all__ = ["IMAGE", "IMAGE_DIGEST"]

#: The pinned container this package shells out to. Built locally from Antmicro's
#: Dockerfile at gerber2ems ``9eaf3033``/openEMS ``a3058772`` -- see ADR 0011, which
#: also records why the pin is a local build rather than a published image (there is
#: no published image) and why it is not fully reproducible (upstream pins its base
#: by tag).
IMAGE = "localhost/gerber2ems:phase0"

#: The image's config digest, recorded so a run can say what it ran on rather than
#: assuming. ``aipcb simulate`` reports the digest it actually found.
IMAGE_DIGEST = "sha256:e1921ec2b3fc9e99216b075f65328b3022d424f9486a4da0b54ca0627f4d23b5"
