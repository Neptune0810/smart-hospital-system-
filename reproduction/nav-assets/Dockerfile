ARG ROS_BASE_IMAGE=ros:humble-ros-base-jammy
FROM ${ROS_BASE_IMAGE}
ARG DEBIAN_FRONTEND=noninteractive
RUN test "$(. /etc/os-release && printf '%s' "$VERSION_CODENAME")" = jammy \
    && test "${ROS_DISTRO}" = humble
COPY apt-packages.lock /tmp/apt-packages.lock
COPY requirements-nav.lock.txt /tmp/requirements-nav.lock.txt
RUN printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/80-smart-hospital-retry \
    && apt-get update \
    && xargs -r -a /tmp/apt-packages.lock apt-get install -y --no-install-recommends \
    && python3 -m pip install --no-cache-dir -r /tmp/requirements-nav.lock.txt \
    && mkdir -p /opt/smart-hospital-dependencies \
    && dpkg-query -W -f='${Package}=${Version}\n' | sort > /opt/smart-hospital-dependencies/apt-resolved.txt \
    && python3 -m pip freeze --all | sort > /opt/smart-hospital-dependencies/pip-resolved.txt \
    && rm -rf /var/lib/apt/lists/*
ENV ROS_DOMAIN_ID=74 RMW_IMPLEMENTATION=rmw_fastrtps_cpp PYTHONUNBUFFERED=1 PYTHONPATH=/project
WORKDIR /project
CMD ["bash"]
