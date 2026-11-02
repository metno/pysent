# The radiative-transfer table generator.
#
# pysent itself depends on numpy and GDAL from apt and nothing else. The
# atmospheric correction needs coefficients no closed-form expression gives
# accurately - above all the Rayleigh path reflectance, which is polarising, so
# a scalar treatment is several per cent wrong at blue, the band the correction
# exists for. The coefficients are therefore generated **once, here, offline**
# by 6SV, and the resulting tables are committed to the repository. This image
# is not part of the runtime and nothing pysent does at processing time needs
# it; it exists so the committed tables can be regenerated and audited.
#
#   docker build -t pysent-rt6s:1.1 tools/rt6s
#   docker run --rm -v "$PWD:/work" -w /work pysent-rt6s:1.1 \
#       python3 tools/rt6s/generate_tables.py --out src/pysent/data
#
FROM ubuntu:24.04

# gfortran builds 6SV; Py6S drives it. numpy and scipy come from pip rather
# than apt on purpose: Py6S depends on both, and pip cannot upgrade an
# apt-installed numpy (it has no RECORD to uninstall), so mixing the two fails
# the build. Nothing here touches pysent's runtime, where numpy stays apt's.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gfortran make ca-certificates curl python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 6SV 1.1, pinned by checksum. Published by Robert Wilson (rtwilson.com), who
# maintains Py6S; the code itself is the Vermote et al. 6S model.
ARG SIXS_URL=https://rtwilson.com/downloads/6SV-1.1.tar
ARG SIXS_SHA256=eedf652e6743b3991b5b9e586da2f55c73f9c9148335a193396bf3893c2bc88f
RUN curl -sL -o /tmp/6SV.tar "$SIXS_URL" \
    && echo "$SIXS_SHA256  /tmp/6SV.tar" | sha256sum -c - \
    && mkdir -p /opt && tar -xf /tmp/6SV.tar -C /opt && rm /tmp/6SV.tar

# 6SV ships a Makefile written for g77. Modern gfortran needs the legacy
# dialect and will not accept the argument mismatches 6SV's own code contains,
# which are warnings in g77 and errors from gfortran 10 on.
#
# -ffpe-summary=none is not cosmetic: 6SV raises an invalid-operation flag
# somewhere harmless, and gfortran prints "the following floating-point
# exceptions are signalling" on exit. That line lands in the output Py6S
# parses, so every single run fails with OutputParsingError until it is gone.
RUN cd /opt/6SV1.1 \
    && sed -i 's|^FC *=.*|FC = gfortran|; s|^FFLAGS *=.*|FFLAGS = -std=legacy -ffixed-line-length-none -fallow-argument-mismatch -ffpe-summary=none -O2|' Makefile \
    && make -s \
    && install -m 0755 sixsV1.1 /usr/local/bin/sixsV1.1 \
    && ln -sf /usr/local/bin/sixsV1.1 /usr/local/bin/sixs

# Pinned: a table regenerated two years from now must come out the same.
# python-dateutil is named explicitly because Py6S 1.9.2 imports it without
# declaring it, so a dependency-only install leaves the package unimportable.
RUN python3 -m pip install --break-system-packages --no-cache-dir \
        Py6S==1.9.2 python-dateutil==2.9.0.post0

ENV PY6S_PATH=/usr/local/bin/sixsV1.1
WORKDIR /work
