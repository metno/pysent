"""Smoke-check the 6SV build, and pin the numbers it has to reproduce.

Run inside the generator image (``docker run --rm pysent-rt6s:1.1 python3
tools/rt6s/check.py``). It asserts the values measured when the image was
first built, so a rebuild that silently changes the physics fails here rather
than in a committed table.

The reference case is 490 nm, solar zenith 70 degrees, view zenith 20, relative
azimuth 90, mid-latitude-summer profile, no aerosols, black ground, target at
sea level and sensor at satellite level.

Three traps are baked into that description, each of which silently returns
zero or one rather than failing:

* **The sensor defaults to ground level.** With it there, there is no
  atmosphere above the sensor, so the path reflectance is 0 and the upward
  transmittance exactly 1, while the optical depth still reports correctly -
  so the output looks half-plausible. ``set_sensor_satellite_level()`` is not
  optional.
* **The geometry needs a date.** Without ``day``/``month`` the solar term is
  zero and every radiance and reflectance comes back 0.000.
* **gfortran prints a floating-point-exception summary** that lands in the
  output Py6S parses, so every run raises ``OutputParsingError`` until the
  build passes ``-ffpe-summary=none``.
"""
from __future__ import annotations

import sys

from Py6S import AeroProfile, AtmosProfile, Geometry, GroundReflectance, SixS, Wavelength

SIXS = "/usr/local/bin/sixsV1.1"

# Measured on the first build of the image, 2026-11-02, gfortran 13 / 6SV 1.1.
EXPECTED = {
    "path_reflectance": 0.09609,
    "tau_rayleigh": 0.15676,
    "transmittance_down": 0.81451,
    "transmittance_up": 0.92285,
    "spherical_albedo": 0.12390,
}
TOLERANCE = 5e-5


def reference_case():
    model = SixS(SIXS)
    model.geometry = Geometry.User()
    model.geometry.solar_z, model.geometry.view_z = 70.0, 20.0
    model.geometry.solar_a, model.geometry.view_a = 0.0, 90.0
    # Without a date the solar term is zero and every reflectance is 0.000.
    model.geometry.day, model.geometry.month = 25, 10
    # Without this the sensor sits on the ground: no atmosphere above it, so no
    # path reflectance and an upward transmittance of exactly 1.
    model.altitudes.set_target_sea_level()
    model.altitudes.set_sensor_satellite_level()
    model.atmos_profile = AtmosProfile.PredefinedType(AtmosProfile.MidlatitudeSummer)
    model.aero_profile = AeroProfile.PredefinedType(AeroProfile.NoAerosols)
    model.ground_reflectance = GroundReflectance.HomogeneousLambertian(0.0)
    model.wavelength = Wavelength(0.490)
    model.run()
    out = model.outputs

    # Py6S exposes no accessor for the intrinsic atmospheric reflectance, so it
    # is read from the "reflectance I" row of the satellite-level block, whose
    # first column is the atmosphere's own contribution.
    row = next(line for line in out.fulltext.splitlines() if "reflectance I" in line)
    return {
        "path_reflectance": float(row.split(":")[1].split()[0]),
        "tau_rayleigh": out.optical_depth_total.rayleigh,
        "transmittance_down": out.transmittance_rayleigh_scattering.downward,
        "transmittance_up": out.transmittance_rayleigh_scattering.upward,
        "spherical_albedo": out.spherical_albedo.total,
    }


def main() -> int:
    measured = reference_case()
    failures = []
    print(f"{'quantity':22s} {'measured':>10s} {'expected':>10s}")
    for name, expected in EXPECTED.items():
        value = measured[name]
        mark = "" if abs(value - expected) <= TOLERANCE else "  <-- differs"
        if mark:
            failures.append(name)
        print(f"{name:22s} {value:10.5f} {expected:10.5f}{mark}")

    # A black surface under a scattering atmosphere must still be brighter than
    # nothing, and the two transmittances must be below one: the two failures
    # the traps above produce look like plausible numbers otherwise.
    if measured["path_reflectance"] <= 0.0:
        failures.append("path reflectance is not positive - is the sensor at satellite level?")
    if measured["transmittance_up"] >= 1.0:
        failures.append("upward transmittance is 1 - the sensor is below the atmosphere")

    if failures:
        print(f"\nFAILED: {', '.join(str(f) for f in failures)}")
        return 1
    print("\n6SV reproduces the reference case.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
