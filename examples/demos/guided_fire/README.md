# guided_fire_basic

RED operator 20 is an affiliated unmanned ground guider. It observes BLUE
operator 30. RED operator 10 carries guided-capable weapon 73 and is within
weapon range, but the concealed target is outside its adjusted observation
distance. The Engine therefore exposes GUIDED_FIRE while no equivalent direct
SHOOT action exists for the launch platform.

The demo submits the explicit Engine-issued FIRE action, expects
`GUIDED_FIRE_RESOLVED`, records the result and performs strict deterministic
Replay verification.
