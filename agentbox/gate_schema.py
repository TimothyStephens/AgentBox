"""The gate's request-body schema: the refusal exceptions, the create bodies' known and refused field tables,
and the strict JSON reading that keeps the gate and podman agreeing on field names (podman matches them
case-insensitively). Split out of gate.py; gate.py re-exports every name here."""
import json


class Deny(Exception):
    status = 403


class NotFound(Deny):
    """The named object does not exist: answered 404, as podman would (compose asks before it creates)."""
    status = 404


class UpstreamError(Deny):
    """podman failed to answer a lookup: answered 502, never taken as "no such object"."""
    status = 502


# Podman decodes JSON field names case-insensitively, so "PortMappings" sets portmappings. Found 1 Oct:
# "PortMappings" got a published host port past the gate and sockguard. Bodies are parsed strictly (two keys
# that differ only in case are refused), top-level fields are renamed to their exact spelling (unknown ones
# refused), and nested fields are read case-insensitively. Values of maps the client owns (env, labels,
# annotations) are never renamed.
LIBPOD_KEYS = (
    "name", "pod", "entrypoint", "command", "env", "terminal", "stdin", "labels", "annotations", "stop_signal",
    "stop_timeout", "timeout", "log_configuration", "raw_image_name", "restart_policy", "restart_tries", "systemd",
    "sdnotifyMode", "pidns", "utsns", "hostname", "hostusers", "sysctl", "remove", "removeImage",
    "containerCreateCommand", "personality", "httpproxy", "envmerge", "unsetenv", "unsetenvall", "manage_password",
    "healthconfig", "health_check_on_failure_action", "startupHealthConfig", "healthLogDestination",
    "healthMaxLogCount", "healthMaxLogSize", "image", "image_volume_mode", "image_volumes", "image_arch",
    "image_os", "image_variant", "mounts", "volumes", "init", "ipcns", "shm_size", "shm_size_systemd", "work_dir",
    "create_working_dir", "chroot_directories", "privileged", "user", "groups", "cap_add", "cap_drop",
    "selinux_opts", "apparmor_profile", "seccomp_policy", "seccomp_profile_path", "no_new_privileges", "userns",
    "idmappings", "read_only_filesystem", "read_write_tmpfs", "label_nested", "procfs_opts", "mask", "unmask",
    "umask", "cgroupns", "cgroups_mode", "cgroup_parent", "netns", "portmappings", "publish_image_ports", "expose",
    "Networks", "cni_networks", "dns_server", "dns_search", "dns_option", "use_image_resolve_conf",
    "use_image_hosts", "hostadd", "base_hosts_file", "network_options", "resource_limits", "r_limits",
    "oom_score_adj", "weightDevice", "throttleReadBpsDevice", "throttleWriteBpsDevice", "throttleReadIOPSDevice",
    "throttleWriteIOPSDevice", "intelRdt", "devices", "device_cgroup_rule", "devices_from", "rootfs",
    "rootfs_overlay", "rootfs_propagation", "rootfs_mapping", "overlay_volumes", "artifact_volumes",
    "volumes_from", "oci_runtime", "conmon_pid_file", "init_path", "env_host", "secrets", "secret_env",
    "dependencyContainers", "timezone", "passwd_entry", "group_entry", "init_container_type", "storage_opts",
    "use_image_hostname", "hostname_path", "stop_after", "volatile")
# Refused whenever set to anything but an empty value.
LIBPOD_REFUSED = {
    "privileged": "privileged containers", "cap_add": "added capabilities", "devices": "devices",
    "device_cgroup_rule": "device cgroup rules", "devices_from": "devices", "portmappings": "published ports",
    "publish_image_ports": "published ports", "cgroup_parent": "a client-chosen cgroup parent",
    "unmask": "unmasked kernel paths", "apparmor_profile": "AppArmor profiles", "seccomp_profile_path":
    "seccomp profiles", "selinux_opts": "SELinux options", "label_nested": "nested SELinux labels",
    "rootfs": "a host directory as root filesystem", "rootfs_overlay": "a host directory as root filesystem",
    "rootfs_mapping": "a host directory as root filesystem", "overlay_volumes": "overlay volumes (host paths)",
    "artifact_volumes": "artifact volumes", "volumes_from": "volumes from other containers",
    "oci_runtime": "a client-chosen OCI runtime", "conmon_pid_file": "a conmon PID file on the host",
    "init_path": "an init binary from the host", "env_host": "the podman service's environment",
    "secrets": "podman secrets", "secret_env": "podman secrets",
    "storage_opts": "storage options",
    # `podman run --rmi`: removes the image when the container is removed, which is image removal through a
    # create (review 2026-11-16, item 12); DELETE /images is refused, so this is too
    "removeImage": "removing the image with the container"}
POD_KEYS = (
    "name", "hostname", "exit_policy", "labels", "no_infra", "infra_name", "pod_create_command", "pidns", "ipcns",
    "utsns", "userns", "cgroupns", "restart_policy", "restart_tries", "shared_namespaces", "share_parent",
    "sysctl", "netns", "portmappings", "Networks", "network_options", "cni_networks", "dns_server", "dns_search",
    "dns_option", "no_manage_resolv_conf", "no_manage_hosts", "hostadd", "base_hosts_file", "cgroup_parent",
    "resource_limits", "cpu_period", "cpu_quota", "throttleReadBpsDevice", "mounts", "volumes", "image_volumes",
    "overlay_volumes", "volumes_from", "shm_size", "shm_size_systemd", "security_opt", "idmappings",
    "pod_devices", "infra_image", "infra_command", "infra_conmon_pid_file", "serviceContainerID",
    "use_image_hosts", "use_image_resolve_conf", "no_manage_hostname")
POD_REFUSED = {
    "portmappings": "published ports", "cgroup_parent": "a client-chosen cgroup parent",
    "overlay_volumes": "overlay volumes (host paths)", "volumes_from": "volumes from other containers",
    "pod_devices": "devices", "infra_image": "a client-chosen infra image", "infra_command": "an infra command",
    "infra_conmon_pid_file": "a conmon PID file on the host", "serviceContainerID": "service containers"}
COMPAT_KEYS = (
    "Hostname", "Domainname", "User", "AttachStdin", "AttachStdout", "AttachStderr", "ExposedPorts", "Tty",
    "OpenStdin", "StdinOnce", "Env", "Cmd", "Healthcheck", "ArgsEscaped", "Image", "Volumes", "WorkingDir",
    "Entrypoint", "NetworkDisabled", "MacAddress", "OnBuild", "Labels", "StopSignal", "StopTimeout", "Shell",
    "HostConfig", "NetworkingConfig", "EnvMerge", "UnsetEnv", "UnsetEnvAll", "Platform")
HOSTCONFIG_KEYS = (
    "Binds", "ContainerIDFile", "LogConfig", "NetworkMode", "PortBindings", "RestartPolicy", "AutoRemove",
    "VolumeDriver", "VolumesFrom", "ConsoleSize", "Annotations", "CapAdd", "CapDrop", "CgroupnsMode", "Dns",
    "DnsOptions", "DnsSearch", "ExtraHosts", "GroupAdd", "IpcMode", "Cgroup", "Links", "OomScoreAdj", "PidMode",
    "Privileged", "PublishAllPorts", "ReadonlyRootfs", "SecurityOpt", "StorageOpt", "Tmpfs", "UTSMode",
    "UsernsMode", "ShmSize", "Sysctls", "Runtime", "Isolation", "CpuShares", "Memory", "NanoCpus", "CgroupParent",
    "BlkioWeight", "BlkioWeightDevice", "BlkioDeviceReadBps", "BlkioDeviceWriteBps", "BlkioDeviceReadIOps",
    "BlkioDeviceWriteIOps", "CpuPeriod", "CpuQuota", "CpuRealtimePeriod", "CpuRealtimeRuntime", "CpusetCpus",
    "CpusetMems", "Devices", "DeviceCgroupRules", "DeviceRequests", "KernelMemory", "KernelMemoryTCP",
    "MemoryReservation", "MemorySwap", "MemorySwappiness", "OomKillDisable", "PidsLimit", "Ulimits", "CpuCount",
    "CpuPercent", "IOMaximumIOps", "IOMaximumBandwidth", "Mounts", "MaskedPaths", "ReadonlyPaths", "Init")
HOSTCONFIG_REFUSED = {
    "PortBindings": "published ports", "PublishAllPorts": "published ports", "Privileged": "privileged containers",
    "CapAdd": "added capabilities", "Devices": "devices", "DeviceRequests": "devices",
    "DeviceCgroupRules": "device cgroup rules", "CgroupParent": "a client-chosen cgroup parent",
    "VolumeDriver": "volume drivers", "VolumesFrom": "volumes from other containers",
    "Runtime": "a client-chosen OCI runtime", "MaskedPaths": "changed masked paths",
    "ReadonlyPaths": "changed read-only paths", "StorageOpt": "storage options", "Cgroup": "cgroup sharing",
    "OomKillDisable": "disabling the OOM killer"}
VOLUME_KEYS = ("Name", "Driver", "Options", "DriverOpts", "Labels", "Label", "UID", "GID", "IgnoreIfExists")
NETWORK_KEYS = ("name", "driver", "internal", "labels", "options", "dns_enabled", "ipv6_enabled", "id", "created",
                "network_interface", "subnets", "routes", "network_dns_servers", "ipam_options")
COMPAT_NETWORK_KEYS = ("Name", "CheckDuplicate", "Driver", "Scope", "EnableIPv4", "EnableIPv6", "IPAM", "Internal",
                       "Attachable", "Ingress", "ConfigOnly", "ConfigFrom", "Options", "Labels")


# Maps whose keys are data, not field names: podman keeps them case-sensitive (NO_PROXY and no_proxy are two
# variables). Keyed by body kind and by the map's exact path (lower-cased field names from the body's root), so
# a field that happens to share a name elsewhere is still checked (review of 785ee15, M2). Only the map's own
# keys are exempt; objects inside it are field names again.
DATA_PATHS = {
    "libpod": {("env",), ("labels",), ("annotations",), ("sysctl",), ("secret_env",), ("expose",), ("networks",),
               ("resource_limits", "unified"), ("log_configuration", "options"), ("weightdevice",),
               ("throttlereadbpsdevice",), ("throttlewritebpsdevice",), ("throttlereadiopsdevice",),
               ("throttlewriteiopsdevice",), ("storage_opts",)},
    "pod": {("labels",), ("sysctl",), ("networks",), ("throttlereadbpsdevice",)},
    "compat": {("labels",), ("volumes",), ("exposedports",), ("hostconfig", "portbindings"),
               ("hostconfig", "annotations"), ("hostconfig", "sysctls"), ("hostconfig", "tmpfs"),
               ("hostconfig", "logconfig", "config"), ("hostconfig", "storageopt"),
               ("networkingconfig", "endpointsconfig")},
    "network": {("labels",), ("options",)},
    "volume": {("label",), ("labels",), ("options",), ("driveropts",)},
}


class _Obj(dict):
    """A JSON object as parsed, remembering keys that differ only in case."""
    case_dups = ()


def strict_loads(body, kind=None):
    """JSON whose field names never differ only in case (podman would see just one of them). Keys of the
    data maps in DATA_PATHS[kind] are data and may (Tim, 2 Oct: NO_PROXY and no_proxy)."""
    data_paths = DATA_PATHS.get(kind, set())
    def pairs(items):
        seen, dups = {}, []
        for k, _ in items:
            if k.lower() in seen:
                dups.append((seen[k.lower()], k))
            seen[k.lower()] = k
        obj = _Obj(items)
        obj.case_dups = dups
        return obj
    try:
        doc = json.loads(body or b"{}", object_pairs_hook=pairs)
    except ValueError as e:
        raise Deny(f"the request body is not valid JSON ({e})")
    if not isinstance(doc, dict):
        raise Deny("the request body must be a JSON object")

    def walk(obj, path):
        if isinstance(obj, dict):
            data = path in data_paths
            if obj.case_dups and not data:
                a, b = obj.case_dups[0]
                raise Deny(f"ambiguous request: keys {a!r} and {b!r} differ only in case")
            for k, v in obj.items():
                # Field names are ASCII. Go's decoder folds U+017F (long s) onto s and U+212A (Kelvin) onto k,
                # which str.lower does not, so a field spelt that way would reach podman unread by the gate
                # (review 2026-11-16, item 8). Data maps keep their keys as they are.
                if not data and not k.isascii():
                    raise Deny(f"request field {k!r} is not ASCII; refused")
                walk(v, path + ("*",) if data else path + (k.lower(),))
        elif isinstance(obj, list):
            for v in obj:
                walk(v, path + ("[]",))
    walk(doc, ())
    return doc


def canonical(obj, known, what):
    """Rename an object's own keys to their exact spelling; refuse keys the gate does not know. Values are
    left as they are (nested fields are read with cget)."""
    if not isinstance(obj, dict):
        raise Deny(f"{what} must be a JSON object")
    canon = {k.lower(): k for k in known}
    out = {}
    for k, v in obj.items():
        if k.lower() not in canon:
            raise Deny(f"{what} field {k!r} is not one the gate knows; refused")
        out[canon[k.lower()]] = v
    return out


def cget(d, key, default=None):
    """d[key] matched case-insensitively (strict_loads guarantees at most one match)."""
    if isinstance(d, dict):
        for k, v in d.items():
            if k.lower() == key.lower():
                return v
    return default


def cset(d, key, value):
    for k in [k for k in d if k.lower() == key.lower()]:
        del d[k]
    d[key] = value
