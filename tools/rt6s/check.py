"""Smoke-check the 6SV build, and pin the numbers it has to reproduce.

Run inside the generator image (``docker run --rm pysent-rt6s:1.1 python3
tools/rt6s/check.py``). It asserts the values measured when the image was
first built, so a rebuild that silently changes the physics fails here rather
than in a committed table.

The reference case is 490 nm, solar zenith 70 degrees, view zenith 20, relative
azimuth 90, mid-latitude-summer profile, no aerosols, black ground, target at
sea level and sensor at satellite level.

Five traps are baked into that description, each of which silently returns a
plausible wrong answer rather than failing:

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
* **``set_target_pressure`` does nothing.** It is accepted, and the Rayleigh
  optical depth stays at its sea-level value - identical at 1013, 850 and
  700 hPa - while ``set_target_custom_altitude`` varies it correctly. A table
  generated through the pressure call has a pressure axis that silently holds
  one value six times over, which looks like a table until something asks it
  how scattering varies with elevation.
* **With an aerosol present, the path reflectance is split across columns.**
  The column labelled ``atm. intrin. ref.`` holds only the molecular part, the
  next column the aerosol part, and the third their sum. Read the first column
  with an aerosol loaded and the number does not move when the optical depth or
  the model changes - it sits at the Rayleigh value, which reads as an axis
  having no effect. With a black ground the apparent reflectance is the sum.
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


def elevation_cases():
    """Rayleigh depth at sea level, and the two ways of asking for elevation.

    Returns ``(sea_level, by_altitude, by_pressure)`` optical depths at 490 nm.
    The second must differ from the first and the third must not, which is the
    whole of the ``set_target_pressure`` trap.
    """
    depths = []
    for setter in (
        lambda model: model.altitudes.set_target_sea_level(),
        lambda model: model.altitudes.set_target_custom_altitude(3.0),
        lambda model: model.altitudes.set_target_pressure(701.09),
    ):
        model = SixS(SIXS)
        model.geometry = Geometry.User()
        model.geometry.solar_z, model.geometry.view_z = 70.0, 20.0
        model.geometry.solar_a, model.geometry.view_a = 0.0, 90.0
        model.geometry.day, model.geometry.month = 25, 10
        setter(model)
        model.altitudes.set_sensor_satellite_level()
        model.atmos_profile = AtmosProfile.PredefinedType(AtmosProfile.NoGaseousAbsorption)
        model.aero_profile = AeroProfile.PredefinedType(AeroProfile.NoAerosols)
        model.ground_reflectance = GroundReflectance.HomogeneousLambertian(0.0)
        model.wavelength = Wavelength(0.490)
        model.run()
        depths.append(model.outputs.optical_depth_total.rayleigh)
    return tuple(depths)


def aerosol_cases():
    """Path reflectance with and without aerosol, both ways of reading it.

    Returns ``(molecular_column, apparent)`` for a clear run and for one with
    a continental aerosol of optical depth 0.4. The molecular column must be
    the *same* in both - that is the trap - while the apparent reflectance must
    rise.
    """
    results = []
    for depth in (0.0, 0.4):
        model = SixS(SIXS)
        model.geometry = Geometry.User()
        model.geometry.solar_z, model.geometry.view_z = 70.0, 40.0
        model.geometry.solar_a, model.geometry.view_a = 0.0, 90.0
        model.geometry.day, model.geometry.month = 25, 10
        model.altitudes.set_target_sea_level()
        model.altitudes.set_sensor_satellite_level()
        model.atmos_profile = AtmosProfile.PredefinedType(AtmosProfile.NoGaseousAbsorption)
        if depth:
            model.aero_profile = AeroProfile.PredefinedType(AeroProfile.Continental)
            model.aot550 = depth
        else:
            model.aero_profile = AeroProfile.PredefinedType(AeroProfile.NoAerosols)
        model.ground_reflectance = GroundReflectance.HomogeneousLambertian(0.0)
        model.wavelength = Wavelength(0.490)
        model.run()
        row = next(
            line for line in model.outputs.fulltext.splitlines() if "reflectance I" in line
        )
        results.append((float(row.split(":")[1].split()[0]), model.outputs.apparent_reflectance))
    return results


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

    sea_level, by_altitude, by_pressure = elevation_cases()
    print(f"\n{'rayleigh depth at sea level':34s} {sea_level:.5f}")
    print(f"{'  via set_target_custom_altitude':34s} {by_altitude:.5f}  (must differ)")
    print(f"{'  via set_target_pressure':34s} {by_pressure:.5f}  (known no-op)")
    if by_altitude >= sea_level:
        failures.append("a target 3 km up did not thin the atmosphere")
    if by_pressure != sea_level:
        failures.append(
            "set_target_pressure now has an effect - it was a silent no-op when the tables "
            "were generated, so the generator drives altitude instead and may want revisiting"
        )

    (clear_column, clear_apparent), (hazy_column, hazy_apparent) = aerosol_cases()
    print(f"\n{'clear: molecular column / apparent':40s} {clear_column:.5f} / {clear_apparent:.5f}")
    print(f"{'aerosol 0.4: molecular column / apparent':40s} "
          f"{hazy_column:.5f} / {hazy_apparent:.5f}")
    if abs(clear_column - clear_apparent) > 1e-4:
        failures.append("with no aerosol the molecular column and the apparent reflectance differ")
    if abs(hazy_column - clear_column) > 1e-4:
        failures.append(
            "the molecular column now moves with the aerosol - the tables take the total from "
            "the apparent reflectance on the assumption that it does not"
        )
    if hazy_apparent <= clear_apparent:
        failures.append("adding an aerosol did not brighten the path")

    if failures:
        print(f"\nFAILED: {', '.join(str(f) for f in failures)}")
        return 1
    print("\n6SV reproduces the reference case.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
