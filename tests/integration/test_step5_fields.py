"""Step 5 audit, mounts and privilege (replaces the A2/A3 reviews that did not finish): one case per field a
container create, pod create, build, volume create or network create can carry in those areas, sent through
the real gate (gate -> dev sockguard -> podman service) of one dev-tier session.

Every case:
  * sends a disallowed value and asserts a 403 from the gate itself, that nothing by the case's name (and no
    new volume) was left behind, and, where podman's inspect shows the field, that the post-create check
    flags it when it is in what podman stored (the field is set in a real inspect document the way podman
    stores it, and the gate's own check reads it);
  * sends an allowed value (where one exists) and asserts the create works and podman stored that value.

`python3 -m tests.integration.test_step5_fields --table` prints the coverage table for
docs/reviews/2026-10-01-step-5-coverage.md from the same CASES list, so the two cannot drift.
"""
import copy
import io
import json
import os
import sys
import tarfile
import unittest
from dataclasses import dataclass, field
from typing import Callable, Optional

from agentbox import config, gate, session

from . import harness

ENV = None
SESS = None
CTX = None
IMG = "docker.io/library/busybox:1.36"
P = f"abt-{harness.RUN}-f"


@dataclass
class Case:
    id: str
    area: str                    # "mounts" or "privilege"
    kind: str                    # container | compat | pod | volume | network | compat_network | build | custom
    field: str
    bad: Optional[Callable] = None        # ctx -> request fragment (or query for builds)
    good: Optional[Callable] = None
    stored: Optional[Callable] = None     # doc -> None: puts the disallowed value where podman stores it
    stored_in: str = "container"          # which document `stored` edits: container | pod | infra | volume | network
    check: Optional[Callable] = None      # (doc, ctx) -> bool: the allowed value as stored
    after: str = ""                       # table text for "checked after create" when `stored` is None
    custom: Optional[Callable] = None     # (test, ctx) -> None: the whole case, for builds and starts
    note: str = ""


def hc(d, **kw):
    d.setdefault("HostConfig", {}).update(kw)


def cfg(d, **kw):
    d.setdefault("Config", {}).update(kw)


def mounts(d, *ms):
    d.setdefault("Mounts", []).extend(ms)


CASES = [
    # ---- mounts -------------------------------------------------------------------------------------
    Case("bind_outside", "mounts", "container", "mounts[type=bind].source (outside the project)",
         bad=lambda c: {"mounts": [{"type": "bind", "source": "/etc", "destination": "/h"}]},
         good=lambda c: {"mounts": [{"type": "bind", "source": c.root, "destination": "/d"}]},
         stored=lambda d: mounts(d, {"Type": "bind", "Source": "/etc", "Destination": "/h"}),
         check=lambda d, c: any(m["Source"] == c.root for m in d["Mounts"])),
    Case("bind_symlink_out", "mounts", "container", "mounts[type=bind].source (a symlink in the project pointing out)",
         bad=lambda c: {"mounts": [{"type": "bind", "source": c.link_out, "destination": "/h"}]},
         stored=lambda d: mounts(d, {"Type": "bind", "Source": CTX.link_out, "Destination": "/h"})),
    Case("bind_sub_path", "mounts", "container", "mounts[type=bind].source (a path inside the project, not a grant root)",
         good=lambda c: {"mounts": [{"type": "bind", "source": c.root, "destination": "/d"}]},
         stored=lambda d: mounts(d, {"Type": "bind", "Source": CTX.data, "Destination": "/d"}),
         check=lambda d, c: any(m["Source"] == c.root for m in d["Mounts"]),
         custom=lambda t, c: t.swap_case(c),
         after="yes: a bind must be a grant root; a grant volume's device must be one; mountinfo after start",
         note="rewritten: a sub-path becomes a SubPath of the gate's bind-backed volume over its grant root, "
              "resolved by podman inside the volume, so a symlink swapped in before start cannot lead out "
              "(Tim's option 3, 1 Oct). Pods: sub-paths refused"),
    Case("bind_relative", "mounts", "container", "mounts[type=bind].source (relative path)",
         bad=lambda c: {"mounts": [{"type": "bind", "source": "data", "destination": "/h"}]},
         stored=lambda d: mounts(d, {"Type": "bind", "Source": "data", "Destination": "/h"})),
    Case("volume_relative", "mounts", "container", "volumes[].Name (a relative path)",
         bad=lambda c: {"volumes": [{"Name": "rel/path", "Dest": "/r"}]},
         stored=lambda d: mounts(d, {"Type": "volume", "Name": "rel/path", "Destination": "/r"})),
    Case("volume_foreign", "mounts", "container", "volumes[].Name (a volume the session did not create)",
         bad=lambda c: {"volumes": [{"Name": c.canary_vol, "Dest": "/v"}]},
         good=lambda c: {"volumes": [{"Name": f"{P}-newvol", "Dest": "/v"}]},
         stored=lambda d: mounts(d, {"Type": "volume", "Name": CTX.canary_vol, "Destination": "/v"}),
         check=lambda d, c: any(m.get("Name") == f"{P}-newvol" for m in d["Mounts"]),
         note="a new name is created by the gate, labelled, first"),
    Case("volume_options", "mounts", "container", "volumes[].Options (volume driver options)",
         bad=lambda c: {"volumes": [{"Name": f"{P}-optvol", "Dest": "/v", "Options": ["volume-opt=o=bind"]}]},
         good=lambda c: {"volumes": [{"Name": f"{P}-rovol", "Dest": "/v", "Options": ["ro"]}]},
         check=lambda d, c: any(m.get("Name") == f"{P}-rovol" and not m.get("RW") for m in d["Mounts"]),
         after="options are not stored on the container; the volume's own Driver/Options are (volume cases)"),
    Case("volume_mount_type", "mounts", "container", "mounts[type=volume] (a volume through mounts)",
         bad=lambda c: {"mounts": [{"type": "volume", "source": c.canary_vol, "destination": "/v"}]},
         stored=lambda d: mounts(d, {"Type": "volume", "Name": CTX.canary_vol, "Destination": "/v"})),
    Case("image_volume", "mounts", "container", "image_volumes[].Source",
         bad=lambda c: {"image_volumes": [{"Source": "docker.io/library/abt-missing:none", "Destination": "/i"}]},
         good=lambda c: {"image_volumes": [{"Source": IMG, "Destination": "/i"}]},
         check=lambda d, c: any(m.get("Type") == "image" and m.get("Destination") == "/i" for m in d["Mounts"]),
         after="Mounts[].Type image allowed (any local image: reads are not scoped, see A4 lows)",
         note="only images already present: no pull past the registry allowlist"),
    Case("image_run", "mounts", "container", "image (another project's or a live stack's localhost/ image)",
         bad=lambda c: {"image": c.live_img}, good=lambda c: {"image": IMG},
         check=lambda d, c: d["ImageName"] == IMG,
         after="refused before forwarding (the image is chosen before podman stores anything)",
         note="Tim, 1 Oct: public registry images, this session's builds, or the project's dev_images"),
    Case("image_build_from", "mounts", "custom", "build FROM / COPY --from / RUN --mount from= (a localhost/ image)",
         custom=lambda t, c: t.build_from_case(c), after="no object until the build ends",
         note="the context is spooled and its Containerfile read before forwarding"),
    Case("tmpfs", "mounts", "container", "mounts[type=tmpfs].options",
         bad=lambda c: {"mounts": [{"type": "tmpfs", "destination": "/t", "options": ["context=x"]}]},
         good=lambda c: {"mounts": [{"type": "tmpfs", "destination": "/t", "options": ["size=16m"]}]},
         check=lambda d, c: any(m.get("Type") == "tmpfs" for m in d["Mounts"]) or "/t" in str(d["HostConfig"].get("Tmpfs")),
         after="a tmpfs holds only the container's own memory; options are not stored",
         note="no host reach; only listed mount options pass"),
    Case("devpts", "mounts", "container", "mounts[type=devpts] (other mount types)",
         bad=lambda c: {"mounts": [{"type": "devpts", "destination": "/e"}]},
         stored=lambda d: mounts(d, {"Type": "bind", "Source": "devpts", "Destination": "/e"})),
    Case("overlay", "mounts", "container", "overlay_volumes[].source",
         bad=lambda c: {"overlay_volumes": [{"source": c.root, "destination": "/o"}]},
         stored=lambda d: mounts(d, {"Type": "bind", "Source": "/etc", "Destination": "/o", "Options": ["O"]})),
    Case("artifact", "mounts", "container", "artifact_volumes",
         bad=lambda c: {"artifact_volumes": [{"source": "x", "destination": "/a"}]},
         stored=lambda d: mounts(d, {"Type": "artifact", "Source": "x", "Destination": "/a"})),
    Case("volumes_from", "mounts", "container", "volumes_from",
         bad=lambda c: {"volumes_from": [CTX.canary]},
         stored=lambda d: hc(d, VolumesFrom=["x"])),
    Case("rootfs", "mounts", "container", "rootfs (a host directory as root)",
         bad=lambda c: {"rootfs": c.root, "image": ""},
         stored=lambda d: d.update(Rootfs="/")),
    Case("init_path", "mounts", "container", "init_path (a host binary bind-mounted in)",
         bad=lambda c: {"init": True, "init_path": "/usr/bin/true"},
         after="not in podman's inspect: refused before forwarding only"),
    Case("hosts_file", "mounts", "container", "base_hosts_file (a host file read in)",
         bad=lambda c: {"base_hosts_file": "/etc/hostname"},
         after="not in podman's inspect: refused before forwarding only"),
    Case("conmon_pidfile", "mounts", "container", "conmon_pid_file (a host file written)",
         bad=lambda c: {"conmon_pid_file": f"{c.tmp}/conmon.pid"},
         stored=lambda d: d.update(ConmonPidFile="/tmp/x.pid")),
    Case("log_path", "mounts", "container", "log_configuration.path (a host file written)",
         bad=lambda c: {"log_configuration": {"driver": "k8s-file", "path": f"{c.tmp}/x.log"}},
         good=lambda c: {"log_configuration": {"driver": "k8s-file"}},
         stored=lambda d: hc(d, LogConfig={"Type": "k8s-file", "Path": "/tmp/x.log"}),
         check=lambda d, c: d["HostConfig"]["LogConfig"]["Type"] == "k8s-file"),
    Case("health_log", "mounts", "container", "healthLogDestination (a host directory written)",
         bad=lambda c: {"healthLogDestination": c.tmp},
         good=lambda c: {"healthLogDestination": "local"},
         stored=lambda d: cfg(d, HealthLogDestination="/tmp"),
         check=lambda d, c: d["Config"].get("HealthLogDestination") == "local"),
    Case("secrets", "mounts", "container", "secrets / secret_env (podman's secret store)",
         bad=lambda c: {"secrets": [{"Source": "abt-none"}]},
         stored=lambda d: cfg(d, Secrets=[{"Name": "x"}])),
    Case("compat_binds_outside", "mounts", "compat", "HostConfig.Binds (outside the project)",
         bad=lambda c: {"HostConfig": {"Binds": ["/etc:/h"]}},
         stored=lambda d: mounts(d, {"Type": "bind", "Source": "/etc", "Destination": "/h"})),
    Case("compat_binds_volume", "mounts", "compat", "HostConfig.Binds (a volume the session did not create)",
         bad=lambda c: {"HostConfig": {"Binds": [f"{c.canary_vol}:/v"]}},
         stored=lambda d: mounts(d, {"Type": "volume", "Name": CTX.canary_vol, "Destination": "/v"})),
    Case("compat_mount_driver", "mounts", "compat", "HostConfig.Mounts[].VolumeOptions.DriverConfig",
         bad=lambda c: {"HostConfig": {"Mounts": [{"Type": "volume", "Source": f"{P}-dvol", "Target": "/v",
                                                   "VolumeOptions": {"DriverConfig": {"Name": "local",
                                                                     "Options": {"o": "bind", "device": "/etc"}}}}]}},
         after="the volume's Driver/Options (volume cases)"),
    Case("compat_volume_driver", "mounts", "compat", "HostConfig.VolumeDriver",
         bad=lambda c: {"HostConfig": {"VolumeDriver": "local"}},
         after="podman stores no volume driver on the container; volumes are checked by their own labels"),
    Case("compat_volumes_from", "mounts", "compat", "HostConfig.VolumesFrom",
         bad=lambda c: {"HostConfig": {"VolumesFrom": [CTX.canary]}},
         stored=lambda d: hc(d, VolumesFrom=["x"])),
    Case("pod_bind_outside", "mounts", "pod", "pod mounts[type=bind].source",
         bad=lambda c: {"mounts": [{"type": "bind", "source": "/etc", "destination": "/h"}]},
         stored=lambda d: mounts(d, {"Type": "bind", "Source": "/etc", "Destination": "/h"}), stored_in="infra"),
    Case("pod_volumes_from", "mounts", "pod", "pod volumes_from",
         bad=lambda c: {"volumes_from": [CTX.canary]}, stored=lambda d: hc(d, VolumesFrom=["x"]), stored_in="infra"),
    Case("pod_overlay", "mounts", "pod", "pod overlay_volumes",
         bad=lambda c: {"overlay_volumes": [{"source": c.root, "destination": "/o"}]},
         after="refused before forwarding; the infra container's Mounts are checked (pod_bind_outside)"),
    Case("volume_create_options", "mounts", "volume", "volume create Options / Driver",
         bad=lambda c: {"Options": {"o": "bind", "device": "/etc", "type": "none"}},
         good=lambda c: {},
         stored=lambda d: d.update(Options={"o": "bind"}), stored_in="volume",
         check=lambda d, c: d["Labels"].get("agentbox.dev-owner") == SESS.name),
    Case("volume_create_driver", "mounts", "volume", "volume create Driver",
         bad=lambda c: {"Driver": "nfs"}, stored=lambda d: d.update(Driver="nfs"), stored_in="volume"),
    Case("volume_create_exists", "mounts", "volume", "volume create IgnoreIfExists",
         bad=lambda c: {"IgnoreIfExists": True}, after="refused before forwarding (an existing volume would come back)"),
    Case("build_volume", "mounts", "build", "build volume=",
         bad=lambda c: {"volume": "/etc:/h"}, after="no object until the build ends"),
    Case("build_secret", "mounts", "build", "build secrets= (src)",
         bad=lambda c: {"secrets": json.dumps(["id=s,src=/etc/hostname"])},
         custom=lambda t, c: t.build_secret_case(c), after="no object until the build ends",
         note="allowed: a secret file inside the context"),
    Case("build_context", "mounts", "build", "build additionalbuildcontexts= / remote=",
         bad=lambda c: {"additionalbuildcontexts": json.dumps({"x": "/etc"})}, after="no object until the build ends"),
    Case("build_outputs", "mounts", "build", "build outputs= (writes on the host)",
         bad=lambda c: {"outputs": "type=local,dest=/tmp"}, after="no object until the build ends"),
    Case("run_mount_cache", "mounts", "custom", "RUN --mount=type=cache",
         custom=lambda t, c: t.run_cache_case(c), after="not visible to the gate (inside the context)",
         note="the session's podman service has its own TMPDIR, so the cache is the session's alone"),
    Case("run_mount_bind_escape", "mounts", "custom", "RUN --mount=type=bind,source=<outside the context>",
         custom=lambda t, c: t.run_bind_case(c), after="not visible to the gate (inside the context)",
         note="refused by buildah, not by the gate"),
    Case("build_tag", "mounts", "custom", "build t= / output= (an existing image's name)",
         custom=lambda t, c: t.build_tag_case(c), after="no object until the build ends"),

    # ---- privilege ----------------------------------------------------------------------------------
    Case("privileged", "privilege", "container", "privileged",
         bad=lambda c: {"privileged": True}, good=lambda c: {"privileged": False},
         stored=lambda d: hc(d, Privileged=True), check=lambda d, c: d["HostConfig"]["Privileged"] is False),
    Case("cap_add", "privilege", "container", "cap_add / cap_drop",
         bad=lambda c: {"cap_add": ["NET_ADMIN"]}, good=lambda c: {"cap_drop": ["ALL"]},
         stored=lambda d: hc(d, CapAdd=["CAP_NET_ADMIN"]), check=lambda d, c: not d["HostConfig"].get("CapAdd")),
    Case("devices", "privilege", "container", "devices",
         bad=lambda c: {"devices": [{"path": "/dev/null"}]}, stored=lambda d: hc(d, Devices=[{"PathOnHost": "/dev/null"}])),
    Case("device_cgroup_rule", "privilege", "container", "device_cgroup_rule",
         bad=lambda c: {"device_cgroup_rule": [{"allow": True, "type": "c", "major": 1, "minor": 3, "access": "r"}]},
         after="not in podman's inspect: refused before forwarding only"),
    Case("seccomp", "privilege", "container", "seccomp_profile_path / seccomp_policy",
         bad=lambda c: {"seccomp_profile_path": "unconfined"}, good=lambda c: {"seccomp_policy": "default"},
         stored=lambda d: hc(d, SecurityOpt=["seccomp=unconfined"]),
         check=lambda d, c: not any("seccomp" in o for o in d["HostConfig"].get("SecurityOpt") or [])),
    Case("seccomp_image", "privilege", "container", "seccomp_policy=image",
         bad=lambda c: {"seccomp_policy": "image"}, after="the effective profile shows in SecurityOpt (seccomp case)"),
    Case("apparmor", "privilege", "container", "apparmor_profile",
         bad=lambda c: {"apparmor_profile": "unconfined"}, stored=lambda d: hc(d, SecurityOpt=["apparmor=unconfined"])),
    Case("selinux", "privilege", "container", "selinux_opts / label_nested",
         bad=lambda c: {"selinux_opts": ["disable"]}, stored=lambda d: hc(d, SecurityOpt=["label=disable"])),
    Case("unmask", "privilege", "container", "unmask",
         bad=lambda c: {"unmask": ["ALL"]}, good=lambda c: {"mask": ["/proc/acpi"]},
         stored=lambda d: hc(d, SecurityOpt=["unmask=all"]), check=lambda d, c: True),
    Case("no_new_privileges", "privilege", "container", "no_new_privileges",
         good=lambda c: {"no_new_privileges": True},
         check=lambda d, c: any("no-new-privileges" in o for o in d["HostConfig"].get("SecurityOpt") or []),
         after="any value allowed; only no-new-privileges may appear in SecurityOpt"),
    Case("pidns", "privilege", "container", "pidns",
         bad=lambda c: {"pidns": {"nsmode": "host"}}, good=lambda c: {"pidns": {"nsmode": "private"}},
         stored=lambda d: hc(d, PidMode="host"), check=lambda d, c: d["HostConfig"]["PidMode"] != "host"),
    Case("ipcns", "privilege", "container", "ipcns",
         bad=lambda c: {"ipcns": {"nsmode": "host"}}, good=lambda c: {"ipcns": {"nsmode": "shareable"}},
         stored=lambda d: hc(d, IpcMode="host"), check=lambda d, c: d["HostConfig"]["IpcMode"] == "shareable"),
    Case("utsns", "privilege", "container", "utsns",
         bad=lambda c: {"utsns": {"nsmode": "host"}}, good=lambda c: {"utsns": {"nsmode": "private"}},
         stored=lambda d: hc(d, UTSMode="host"), check=lambda d, c: d["HostConfig"]["UTSMode"] != "host"),
    Case("userns", "privilege", "container", "userns",
         bad=lambda c: {"userns": {"nsmode": "host"}}, good=lambda c: {"userns": {"nsmode": "keep-id"}},
         stored=lambda d: hc(d, UsernsMode="host"), check=lambda d, c: True),
    Case("cgroupns", "privilege", "container", "cgroupns",
         bad=lambda c: {"cgroupns": {"nsmode": "host"}}, good=lambda c: {"cgroupns": {"nsmode": "private"}},
         stored=lambda d: hc(d, CgroupnsMode="host"), check=lambda d, c: d["HostConfig"].get("CgroupnsMode") != "host"),
    Case("netns", "privilege", "container", "netns",
         bad=lambda c: {"netns": {"nsmode": "host"}}, good=lambda c: {"netns": {"nsmode": "bridge"}},
         stored=lambda d: hc(d, NetworkMode="host"),
         check=lambda d, c: set(d["NetworkSettings"]["Networks"]) == {SESS.int_net}),
    Case("netns_pasta", "privilege", "container", "netns pasta + network_options",
         bad=lambda c: {"netns": {"nsmode": "pasta"}, "network_options": {"pasta": ["-t", "18093"]}},
         stored=lambda d: hc(d, NetworkMode="pasta")),
    Case("sysctl", "privilege", "container", "sysctl",
         bad=lambda c: {"sysctl": {"net.core.somaxconn": "512"}},
         stored=lambda d: hc(d, Sysctls={"kernel.domainname": "x"}),
         note="every sysctl: the dev sockguard refuses them all, so the gate does too"),
    Case("annotations", "privilege", "container", "annotations",
         bad=lambda c: {"annotations": {"run.oci.keep_original_groups": "1"}},
         good=lambda c: {"annotations": {"org.example.note": "x"}},
         stored=lambda d: cfg(d, Annotations={"run.oci.keep_original_groups": "1"}),
         check=lambda d, c: d["Config"]["Annotations"].get("org.example.note") == "x"),
    Case("oom_score", "privilege", "container", "oom_score_adj",
         bad=lambda c: {"oom_score_adj": -1000}, good=lambda c: {"oom_score_adj": 1000},
         stored=lambda d: hc(d, OomScoreAdj=-1000), check=lambda d, c: d["HostConfig"]["OomScoreAdj"] == 1000),
    Case("cgroup_parent", "privilege", "container", "cgroup_parent / cgroups_mode",
         bad=lambda c: {"cgroup_parent": "/"}, stored=lambda d: hc(d, CgroupParent="/"),
         check=lambda d, c: True),
    Case("cgroups_disabled", "privilege", "container", "cgroups_mode=disabled",
         bad=lambda c: {"cgroups_mode": "disabled"}, stored=lambda d: hc(d, Cgroups="disabled")),
    Case("memory", "privilege", "container", "resource_limits.memory (limit, swap, unified, OOM killer)",
         bad=lambda c: {"resource_limits": {"memory": {"limit": 64 << 30}}},
         good=lambda c: {"resource_limits": {"memory": {"limit": 256 << 20}}},
         stored=lambda d: hc(d, Memory=0, MemorySwap=-1),
         check=lambda d, c: d["HostConfig"]["Memory"] == 256 << 20 and d["HostConfig"]["MemorySwap"] == 256 << 20),
    Case("memory_oomkiller", "privilege", "container", "resource_limits.memory.disableOOMKiller",
         bad=lambda c: {"resource_limits": {"memory": {"disableOOMKiller": True}}},
         stored=lambda d: hc(d, OomKillDisable=True)),
    Case("pids", "privilege", "container", "resource_limits.pids",
         bad=lambda c: {"resource_limits": {"pids": {"limit": -1}}},
         good=lambda c: {"resource_limits": {"pids": {"limit": 100}}},
         stored=lambda d: hc(d, PidsLimit=0), check=lambda d, c: d["HostConfig"]["PidsLimit"] == 100),
    Case("oci_runtime", "privilege", "container", "oci_runtime (a host binary run as the runtime)",
         bad=lambda c: {"oci_runtime": f"{c.root}/runtime.sh"}, stored=lambda d: d.update(OCIRuntime="/tmp/x")),
    Case("env_host", "privilege", "container", "env_host (the podman service's environment)",
         bad=lambda c: {"env_host": True}, after="not distinguishable in inspect: refused before forwarding only"),
    Case("dependencies", "privilege", "container", "dependencyContainers (starts other containers)",
         bad=lambda c: {"dependencyContainers": [CTX.canary]}, stored=lambda d: d.update(Dependencies=["x"])),
    Case("keep_groups", "privilege", "container", "groups keep-groups",
         bad=lambda c: {"groups": ["keep-groups"]}, good=lambda c: {"groups": ["10"]},
         stored=lambda d: cfg(d, Annotations={"run.oci.keep_original_groups": "1"}), check=lambda d, c: True),
    Case("health_restart", "privilege", "container", "health_check_on_failure_action=restart",
         bad=lambda c: {"healthconfig": {"Test": ["CMD", "true"]}, "health_check_on_failure_action": 3},
         stored=lambda d: cfg(d, HealthcheckOnFailureAction="restart"),
         note="a restart by podman itself would skip the gate's start check"),
    Case("ports", "privilege", "container", "portmappings / publish_image_ports",
         bad=lambda c: {"publish_image_ports": True, "expose": {"80": "tcp"}},
         stored=lambda d: hc(d, PortBindings={"80/tcp": [{"HostPort": "18094"}]})),
    Case("unknown_field", "privilege", "container", "any field the gate does not know",
         bad=lambda c: {"no_such_field": 1}, after="refused before forwarding"),
    Case("compat_privileged", "privilege", "compat", "HostConfig.Privileged / CapAdd / Devices",
         bad=lambda c: {"HostConfig": {"Privileged": True}}, stored=lambda d: hc(d, Privileged=True)),
    Case("compat_securityopt", "privilege", "compat", "HostConfig.SecurityOpt",
         bad=lambda c: {"HostConfig": {"SecurityOpt": ["seccomp=unconfined"]}},
         stored=lambda d: hc(d, SecurityOpt=["seccomp=unconfined"])),
    Case("compat_pidmode", "privilege", "compat", "HostConfig.PidMode / IpcMode / UTSMode / UsernsMode / CgroupnsMode",
         bad=lambda c: {"HostConfig": {"PidMode": "host"}}, stored=lambda d: hc(d, PidMode="host")),
    Case("compat_sysctls", "privilege", "compat", "HostConfig.Sysctls",
         bad=lambda c: {"HostConfig": {"Sysctls": {"kernel.domainname": "x"}}},
         stored=lambda d: hc(d, Sysctls={"kernel.domainname": "x"})),
    Case("compat_annotations", "privilege", "compat", "HostConfig.Annotations",
         bad=lambda c: {"HostConfig": {"Annotations": {"run.oci.keep_original_groups": "1"}}},
         stored=lambda d: cfg(d, Annotations={"run.oci.keep_original_groups": "1"})),
    Case("compat_oom", "privilege", "compat", "HostConfig.OomScoreAdj / OomKillDisable",
         bad=lambda c: {"HostConfig": {"OomScoreAdj": -1000}}, stored=lambda d: hc(d, OomScoreAdj=-1000)),
    Case("compat_cgroup", "privilege", "compat", "HostConfig.CgroupParent / Cgroup",
         bad=lambda c: {"HostConfig": {"CgroupParent": "/"}}, stored=lambda d: hc(d, CgroupParent="/")),
    Case("compat_memory", "privilege", "compat", "HostConfig.Memory / MemorySwap / PidsLimit",
         bad=lambda c: {"HostConfig": {"PidsLimit": -1}}, stored=lambda d: hc(d, PidsLimit=0)),
    Case("compat_runtime", "privilege", "compat", "HostConfig.Runtime",
         bad=lambda c: {"HostConfig": {"Runtime": "/tmp/x"}}, stored=lambda d: d.update(OCIRuntime="/tmp/x")),
    Case("compat_masked", "privilege", "compat", "HostConfig.MaskedPaths / ReadonlyPaths",
         bad=lambda c: {"HostConfig": {"MaskedPaths": []}}, stored=lambda d: hc(d, SecurityOpt=["unmask=all"])),
    Case("pod_ports", "privilege", "pod", "pod portmappings",
         bad=lambda c: {"portmappings": [{"host_port": 18095, "container_port": 80}]},
         stored=lambda d: d["InfraConfig"].update(PortBindings={"80/tcp": [{"HostPort": "18095"}]}), stored_in="pod"),
    Case("pod_devices", "privilege", "pod", "pod pod_devices",
         bad=lambda c: {"pod_devices": ["/dev/null"]}, stored=lambda d: hc(d, Devices=[{"PathOnHost": "/dev/null"}]),
         stored_in="infra"),
    Case("pod_security_opt", "privilege", "pod", "pod security_opt",
         bad=lambda c: {"security_opt": ["seccomp=unconfined"]},
         stored=lambda d: hc(d, SecurityOpt=["seccomp=unconfined"]), stored_in="infra"),
    Case("pod_netns", "privilege", "pod", "pod netns / pidns / utsns / userns",
         bad=lambda c: {"netns": {"nsmode": "host"}},
         stored=lambda d: d["InfraConfig"].update(HostNetwork=True), stored_in="pod"),
    Case("pod_infra", "privilege", "pod", "pods with an infra container (no_infra false; shared_namespaces)",
         bad=lambda c: {"no_infra": False, "shared_namespaces": ["ipc"]}, good=lambda c: {},
         stored=lambda d: d.update(SharedNamespaces=["ipc"]), stored_in="pod",
         check=lambda d, c: not d.get("InfraContainerID") and d["Labels"].get("agentbox.dev-owner") == SESS.name,
         after="yes: with infra, SharedNamespaces must hold net and InfraConfig.Networks == the session network",
         note="refused: the dev sockguard cannot resolve pods, so no container could join one"),
    Case("pod_member", "privilege", "custom", "a container joining a pod (pod, netns, Networks)",
         custom=lambda t, c: t.pod_member_case(c), stored_in="container",
         after="yes: networks == the session network; in an infra pod, NetworkMode container:<infra>"),
    Case("pod_sysctl", "privilege", "pod", "pod sysctl",
         bad=lambda c: {"sysctl": {"kernel.domainname": "x"}}, stored=lambda d: hc(d, Sysctls={"kernel.domainname": "x"}),
         stored_in="infra"),
    Case("pod_infra_image", "privilege", "pod", "pod infra_image / infra_command / infra_conmon_pid_file",
         bad=lambda c: {"infra_image": IMG}, after="refused before forwarding only"),
    Case("pod_cgroup", "privilege", "pod", "pod cgroup_parent",
         bad=lambda c: {"cgroup_parent": "/"}, stored=lambda d: d.update(CgroupParent="/"), stored_in="pod"),
    Case("network_driver", "privilege", "network", "network create driver / network_interface",
         bad=lambda c: {"driver": "macvlan", "network_interface": "lo"},
         good=lambda c: {}, stored=lambda d: d.update(driver="macvlan"), stored_in="network",
         check=lambda d, c: d["internal"] is True and not d.get("options")
         and d["labels"].get("agentbox.dev-owner") == SESS.name),
    Case("network_subnet", "privilege", "network", "network create subnets / routes / ipam_options",
         bad=lambda c: {"subnets": [{"subnet": "10.0.0.0/24"}]}, after="the session network is the only one used"),
    Case("network_options", "privilege", "network", "network create options (isolate, mtu, ...)",
         bad=lambda c: {"options": {"isolate": "false"}},
         stored=lambda d: d.update(options={"isolate": "false"}), stored_in="network",
         note="no options at all: the dev sockguard refuses them, so isolate=strict cannot be set either"),
    Case("network_internal", "privilege", "network", "network create internal / ipv6_enabled",
         bad=lambda c: {"ipv6_enabled": True}, stored=lambda d: d.update(internal=False), stored_in="network"),
    Case("compat_network", "privilege", "compat_network", "network create (Docker API) Driver / IPAM",
         bad=lambda c: {"Driver": "macvlan"}, good=lambda c: {},
         check=lambda d, c: d["internal"] is True),
]


class Ctx:
    pass


def setUpModule():
    global ENV, SESS, CTX
    if harness.podman("image", "exists", IMG).returncode != 0:
        raise unittest.SkipTest(f"{IMG} not present")
    ENV = harness.Env()
    root = ENV.make_project("fx", preset="python", extra=["--podman", "dev"])
    c = CTX = Ctx()
    c.root = root
    c.data = os.path.join(root, "data")
    os.makedirs(c.data, exist_ok=True)
    c.tmp = os.path.join(harness.BASE, "fields-tmp")
    c.outside = os.path.join(harness.BASE, "outside")
    for d in (c.tmp, c.outside):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(c.outside, "marker.txt"), "w") as f:
        f.write("OUTSIDE_MARKER\n")
    with open(os.path.join(c.outside, "f"), "w") as f:          # what a sub-path that led out would show
        f.write("OUTSIDE_MARKER\n")
    c.link_out = os.path.join(root, "link-out")
    os.symlink(c.outside, c.link_out)
    c.canary = f"{P}-canary"
    c.canary_vol = f"{P}-canaryvol"
    harness.podman("volume", "create", "--label", "agentbox-test=1", "--label", harness.LABEL,
                   "--label", "io.podman.compose.project=abt-other", c.canary_vol)
    harness.podman("create", "--name", c.canary, "--label", "agentbox-test=1", "--label", harness.LABEL,
                   "--network=none", IMG, "true")
    c.live_img = f"localhost/{P}-liveimg:latest"         # stands in for a live stack's own build
    ctx = os.path.join(c.tmp, "liveimg")
    os.makedirs(ctx, exist_ok=True)
    with open(os.path.join(ctx, "Containerfile"), "w") as f:
        f.write(f"FROM {IMG}\nRUN echo live > /live.txt\n")
    p = harness.podman("build", "-q", "-t", c.live_img, "--label", "agentbox-test=1", "--label", harness.LABEL,
                       ctx, timeout=600)
    assert p.returncode == 0, p.stderr
    c.volumes_before = harness.all_volumes()
    keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
    c.old_env = {k: os.environ.get(k) for k in keys}
    os.environ.update({k: ENV.env[k] for k in keys})
    s = config.load()
    SESS = session.make_session(s, "omp", session.load_project(s, "fx"), warn=lambda m: None)
    SESS.prepare()
    SESS.start_infra()
    with open(os.path.join(s.service_dir(SESS.name), "gate.json")) as f:
        c.gate = gate.Gate(json.load(f))
    c.gate.log_path = os.devnull
    print(f"\n[itest run {harness.RUN}: step 5 fields, session {SESS.name}]")


def tearDownModule():
    try:
        if SESS is not None:
            SESS.teardown()
    finally:
        for k, v in (CTX.old_env if CTX else {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        harness.podman("rmi", f"localhost/{P}-foreign:latest")       # an extra name on busybox, untagged only
        for img in harness.podman("images", "-q", "--filter", f"label={harness.LABEL}").stdout.split():
            harness.podman("rmi", "-f", img)
        harness.cleanup()


def api(method, path, body=None, *, raw=None, ctype="application/json", query=None, timeout=300):
    conn = gate.UnixHTTP(SESS.gate_socket)
    conn.timeout = timeout
    try:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        url = "/v5.0.0" + path + (("?" + "&".join(f"{k}={v}" for k, v in query.items())) if query else "")
        conn.request(method, url, body=data, headers={"Content-Type": ctype} if data is not None else {})
        r = conn.getresponse()
        return r.status, r.read().decode(errors="replace")
    finally:
        conn.close()


def deep_merge(a, b):
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def request_for(case, frag, name):
    from urllib.parse import quote
    if case.kind == "container":
        return "POST", "/libpod/containers/create", {"image": IMG, "name": name, "command": ["sleep", "60"], **frag}, None
    if case.kind == "compat":
        return "POST", f"/containers/create?name={quote(name)}", deep_merge(
            {"Image": IMG, "Cmd": ["sleep", "60"], "HostConfig": {}}, frag), None
    if case.kind == "pod":
        return "POST", "/libpod/pods/create", {"name": name, "no_infra": True, **frag}, None
    if case.kind == "volume":
        return "POST", "/libpod/volumes/create", {"Name": name, **frag}, None
    if case.kind == "network":
        return "POST", "/libpod/networks/create", {"name": name, **frag}, None
    if case.kind == "compat_network":
        return "POST", "/networks/create", {"Name": name, **frag}, None
    raise AssertionError(case.kind)


def exists(name):
    return {kind: harness.podman(*cmd, name).returncode == 0
            for kind, cmd in (("container", ("container", "exists")), ("pod", ("pod", "exists")),
                              ("volume", ("volume", "exists")), ("network", ("network", "exists")))}


def inspect(kind, name):
    cmd = {"container": ("inspect", "--type", "container"), "pod": ("pod", "inspect"),
           "volume": ("volume", "inspect"), "network": ("network", "inspect")}[kind]
    p = harness.podman(*cmd, name)
    doc = json.loads(p.stdout or "null")
    return doc[0] if isinstance(doc, list) else doc


def tar_of(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def build(query, files, timeout=600):
    from urllib.parse import quote
    q = {"dockerfile": quote(json.dumps(["Containerfile"])), "rm": "1", **{k: quote(str(v), safe="") for k, v in query.items()}}
    return api("POST", "/libpod/build", raw=tar_of(files), ctype="application/x-tar", query=q, timeout=timeout)


def build_lines(body):
    out = []
    dec = json.JSONDecoder()
    i = 0
    while i < len(body):
        t = body[i:].lstrip()
        i = len(body) - len(t)
        if not t:
            break
        try:
            o, n = dec.raw_decode(t)
        except ValueError:
            break
        i += n
        out.append(o.get("stream", "") + o.get("error", ""))
    return "".join(out)


class Fields(unittest.TestCase):
    maxDiff = None
    baseline = {}

    @classmethod
    def setUpClass(cls):
        # reference objects created through the gate with nothing special: their stored documents are what
        # each case's disallowed value is put into, for the gate's post-create check to read
        st, body = api("POST", "/libpod/containers/create", {"image": IMG, "name": f"{P}-ref", "command": ["true"]})
        assert st == 201, (st, body)
        cls.baseline["container"] = inspect("container", f"{P}-ref")
        # the gate refuses infra pods (sockguard cannot resolve pods), so the reference infra pod is made on the
        # host, the way the gate would have made it: dev labels, the session network, the slice
        args = ["pod", "create", "--name", f"{P}-refpod", "--network", SESS.int_net]
        args += [f"--label={k}={v}" for k, v in CTX.gate.stamp({}).items()]
        if SESS.settings.cgroup_parent:
            args.append(f"--cgroup-parent={SESS.settings.cgroup_parent}")
        p = harness.podman(*args)
        assert p.returncode == 0, p.stderr
        cls.baseline["pod"] = inspect("pod", f"{P}-refpod")
        cls.baseline["infra"] = inspect("container", cls.baseline["pod"]["InfraContainerID"])
        st, body = api("POST", "/libpod/volumes/create", {"Name": f"{P}-refvol"})
        assert st in (200, 201), (st, body)
        cls.baseline["volume"] = inspect("volume", f"{P}-refvol")
        st, body = api("POST", "/libpod/networks/create", {"name": f"{P}-refnet"})
        assert st in (200, 201), (st, body)
        cls.baseline["network"] = inspect("network", f"{P}-refnet")
        CTX.volumes_before = harness.all_volumes()      # the session's own sidecars have anonymous volumes

    def violations(self, kind, doc):
        g = CTX.gate
        return {"container": g.container_violations, "pod": g.pod_violations, "infra": g.infra_violations,
                "volume": g.volume_violations, "network": g.net_violations}[kind](doc)

    def assert_refused(self, st, body, what):
        self.assertEqual(st, 403, f"{what}: {st} {body[:300]}")
        self.assertIn("agentbox gate", body, f"{what}: refused by the gate itself")

    def assert_nothing_left(self, name):
        self.assertEqual(exists(name), dict.fromkeys(("container", "pod", "volume", "network"), False), name)
        self.assertEqual(sorted(harness.all_volumes() - CTX.volumes_before - self.known_volumes()), [],
                         "no new volume left behind")

    def known_volumes(self):
        return {f"{P}-refvol", f"{P}-newvol", f"{P}-rovol"} | {v for v in harness.all_volumes()
                                                                if v.startswith((f"{P}-ok-", f"{SESS.name}-grant"))}

    def run_case(self, case):
        if case.bad:
            name = f"{P}-bad-{case.id.replace('_', '-')}"
            if case.kind == "build":
                st, body = build({"t": f"{P}-bad-img", **case.bad(CTX)}, {"Containerfile": f"FROM {IMG}\n"})
                self.assert_refused(st, body, case.id)
                self.assertNotEqual(harness.podman("image", "exists", f"{P}-bad-img").returncode, 0)
            else:
                method, path, body, _ = request_for(case, case.bad(CTX), name)
                st, resp = api(method, path, body)
                self.assert_refused(st, resp, case.id)
                self.assert_nothing_left(name)
        if case.stored:
            base = copy.deepcopy(self.baseline[case.stored_in])
            self.assertEqual(self.violations(case.stored_in, base), [], "the reference object passes")
            case.stored(base)
            self.assertTrue(self.violations(case.stored_in, base),
                            f"{case.id}: the post-create check reads {case.field} back from what podman stores")
        if case.custom:
            case.custom(self, CTX)
        if case.good:
            name = f"{P}-ok-{case.id.replace('_', '-')}"
            method, path, body, _ = request_for(case, case.good(CTX), name)
            st, resp = api(method, path, body)
            self.assertIn(st, (200, 201), f"{case.id} allowed value: {st} {resp[:400]}")
            kind = {"container": "container", "compat": "container", "pod": "pod", "volume": "volume",
                    "network": "network", "compat_network": "network"}[case.kind]
            doc = inspect(kind, name)
            self.assertTrue(case.check(doc, CTX), f"{case.id}: stored value: "
                            + json.dumps({k: doc.get(k) for k in ("HostConfig", "Mounts", "Config", "SharedNamespaces",
                                                                   "InfraConfig", "Labels", "internal", "options")})[:1500])
            if kind == "container":
                st, resp = api("POST", f"/libpod/containers/{name}/start")
                self.assertIn(st, (204, 304), f"{case.id}: start {st} {resp[:300]}")
                st, _ = api("DELETE", f"/libpod/containers/{name}?force=true&t=0")
                self.assertIn(st, (200, 204), "removed through the gate")
            elif kind == "pod":
                api("DELETE", f"/libpod/pods/{name}?force=true")
            elif kind == "network":
                api("DELETE", f"/libpod/networks/{name}")

    # ---- custom cases ------------------------------------------------------------------------------
    def swap_case(self, c):
        """A sub-path bind is rewritten to a grant-volume SubPath and works; the same path swapped for a symlink
        out before start never shows the outside file; a pod's sub-path bind is refused."""
        src = os.path.join(c.root, "swapdir")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "f"), "w") as f:
            f.write("SUBPATH_INSIDE\n")
        names = []
        for n in ("ok", "swapped"):
            name = f"{P}-sub-{n}"
            names.append(name)
            st, body = api("POST", "/libpod/containers/create", {
                "image": IMG, "name": name, "command": ["sh", "-c", "cat /s/f; sleep 30"],
                "mounts": [{"type": "bind", "source": src, "destination": "/s", "options": ["ro"]}]})
            self.assertEqual(st, 201, body)
            doc = inspect("container", name)
            self.assertEqual([(m["Type"], m.get("Name")) for m in doc["Mounts"] if m["Destination"] == "/s"],
                             [("volume", f"{SESS.name}-grant0")], "rewritten to the grant volume")
        try:
            st, body = api("POST", f"/libpod/containers/{names[0]}/start")
            self.assertIn(st, (204, 304), body)
            self.assertTrue(harness.wait_for(lambda: "SUBPATH_INSIDE" in harness.podman("logs", names[0]).stdout,
                                             timeout=30), "the sub-path is mounted")
            os.rename(src, src + ".real")
            os.symlink(c.outside, src)
            st, body = api("POST", f"/libpod/containers/{names[1]}/start")
            logs = harness.podman("logs", names[1]).stdout
            self.assertNotIn("OUTSIDE_MARKER", logs, f"the swapped sub-path led out ({st} {body[:200]})")
            print(f"\n  swapped sub-path at start: {st} {body[:160]}")
        finally:
            if os.path.islink(src):
                os.unlink(src)
                os.rename(src + ".real", src)
            for name in names:
                api("DELETE", f"/libpod/containers/{name}?force=true&t=0")
        st, body = api("POST", "/libpod/pods/create", {"name": f"{P}-subpod", "no_infra": True,
                                                         "mounts": [{"type": "bind", "source": src, "destination": "/s"}]})
        self.assert_refused(st, body, "a pod's sub-path bind")
        self.assertIn("only a grant root itself", body)

    def build_from_case(self, c):
        for cf in (f"FROM {c.live_img}\n", f"FROM {IMG}\nCOPY --from={c.live_img} /live.txt /x\n",
                   f"FROM {IMG}\nRUN --mount=type=bind,from={c.live_img},source=/live.txt,target=/l cat /l\n",
                   f"ARG BASE={c.live_img}\nFROM $BASE\n"):
            st, body = build({"t": f"{P}-from"}, {"Containerfile": cf})
            self.assert_refused(st, body, f"build {cf.strip()}")
        self.assertNotEqual(harness.podman("image", "exists", f"{P}-from").returncode, 0)
        st, body = build({"t": f"{P}-from"}, {"Containerfile": f"FROM {IMG}\nRUN echo own > /own.txt\n"})
        self.assertEqual(st, 200, build_lines(body)[-300:])
        st, body = build({"t": f"{P}-from2"}, {"Containerfile": f"FROM localhost/{P}-from:latest\n"})
        self.assertEqual(st, 200, "this session's own build can be the base of the next: "
                         + build_lines(body)[-300:])

    def build_secret_case(self, c):
        st, body = build({"t": f"{P}-secret", "secrets": json.dumps(["id=s,src=secret.txt"])},
                         {"Containerfile": f"FROM {IMG}\nRUN --mount=type=secret,id=s cat /run/secrets/s\n",
                          "secret.txt": "BUILD_SECRET_OK\n"})
        self.assertEqual(st, 200, body[-500:])
        self.assertIn("BUILD_SECRET_OK", build_lines(body), "a secret from the context works")

    def run_cache_case(self, c):
        cid = f"{P}-cache"
        uid = os.getuid()
        glob_dir = f"/var/tmp/buildah-cache-{uid}"
        before = set(os.listdir(glob_dir)) if os.path.isdir(glob_dir) else set()
        st, body = build({"t": f"{P}-cache"}, {"Containerfile": (
            f"FROM {IMG}\nRUN --mount=type=cache,id={cid},target=/c sh -c 'echo CACHE_{harness.RUN} > /c/m'\n")})
        self.assertEqual(st, 200, build_lines(body)[-500:])
        after = set(os.listdir(glob_dir)) if os.path.isdir(glob_dir) else set()
        self.assertEqual(after - before, set(), "nothing new in the host's shared buildah cache")
        tmp = session.service_tmp(SESS.settings, SESS.name)
        found = []
        for dirpath, _d, files in os.walk(tmp):
            found += [os.path.join(dirpath, f) for f in files if f == "m"]
        self.assertTrue(found, f"the cache is under the session's own TMPDIR {tmp}")

    def run_bind_case(self, c):
        rel = os.path.relpath(os.path.join(c.outside, "marker.txt"), c.root)
        st, body = build({"t": f"{P}-escape"}, {"Containerfile": (
            f"FROM {IMG}\nRUN --mount=type=bind,source={rel},target=/e cat /e\n")})
        self.assertNotIn("OUTSIDE_MARKER", build_lines(body), "a RUN bind mount cannot reach outside the context")

    def build_tag_case(self, c):
        harness.podman("tag", IMG, f"localhost/{P}-foreign:latest")
        for tag in ("localhost/agentbox-omp:latest", IMG, f"localhost/{P}-foreign:latest", "lscr.io/x/y:1"):
            before = harness.podman("image", "inspect", "--format", "{{.Id}}", tag).stdout.strip()
            st, body = build({"t": tag}, {"Containerfile": f"FROM {IMG}\nRUN echo poison\n"})
            self.assert_refused(st, body, f"build -t {tag}")
            after = harness.podman("image", "inspect", "--format", "{{.Id}}", tag).stdout.strip()
            self.assertEqual(before, after, f"{tag} still names the same image")
        for _ in range(2):          # this project's own tag, twice: the second build replaces its own image
            st, body = build({"t": f"{P}-own"}, {"Containerfile": f"FROM {IMG}\nRUN echo own\n"})
            self.assertEqual(st, 200, build_lines(body)[-400:])
            self.assertNotIn("agentbox gate", body)

    def pod_member_case(self, c):
        """podman-compose's pods have no infra container: membership is dropped and the container pinned, and
        what it asked for (pasta with a published port, podman's default network) is refused or replaced."""
        pod = f"{P}-mpod"
        st, body = api("POST", "/libpod/pods/create", {"name": pod, "no_infra": True})
        self.assertEqual(st, 201, body)
        base = {"image": IMG, "command": ["sleep", "60"], "pod": pod}
        st, body = api("POST", "/libpod/containers/create", dict(base, name=f"{P}-member-bad", netns={"nsmode": "pasta"},
                                                                 network_options={"pasta": ["-t", "18096"]}))
        self.assert_refused(st, body, "pasta in a pod")
        self.assert_nothing_left(f"{P}-member-bad")
        name = f"{P}-member"
        st, body = api("POST", "/libpod/containers/create", dict(base, name=name, Networks={"podman": {}}))
        self.assertEqual(st, 201, body)
        doc = inspect("container", name)
        self.assertEqual(set(doc["NetworkSettings"]["Networks"]), {SESS.int_net}, "pinned to the session network")
        self.assertEqual(self.violations("container", doc), [])
        bad = copy.deepcopy(doc)
        bad["HostConfig"]["NetworkMode"] = "pasta"
        self.assertTrue(self.violations("container", bad))
        st, body = api("POST", f"/libpod/containers/{name}/start")
        self.assertIn(st, (204, 304), body)
        api("DELETE", f"/libpod/containers/{name}?force=true&t=0")
        api("DELETE", f"/libpod/pods/{pod}?force=true")


def _make(case):
    def test(self):
        self.run_case(case)
    test.__doc__ = f"{case.area}: {case.field}"
    return test


for _case in CASES:
    setattr(Fields, f"test_{_case.id}", _make(_case))


class Zz(unittest.TestCase):
    def test_zz_session_leaves_nothing(self):
        name = SESS.name
        SESS.teardown()
        left = [x for x in harness.leftovers() if x not in (CTX.canary, CTX.canary_vol)]
        self.assertEqual(left, [], f"after {name}")
        self.assertEqual(sorted(harness.all_volumes() - CTX.volumes_before), [], "no unlabelled volume either")
        self.assertFalse(os.path.exists(session.service_tmp(SESS.settings, name)), "the session's TMPDIR is gone")
        if SESS.settings.cgroup_parent:
            from agentbox import buildcap
            cg = "/sys/fs/cgroup" + buildcap.slice_cgroup_path(SESS.settings.cgroup_parent) + f"/{name}-build"
            self.assertFalse(os.path.exists(cg), "the session's build cgroup is removed (review step 5C, L4)")


def table():
    rows = ["| Area | Field | Refused before forwarding | Checked after create | Test | Notes |",
            "|---|---|---|---|---|---|"]
    for c in CASES:
        before = "yes (403)" if c.bad else ("rewritten" if "rewritten" in c.note else "n/a")
        if c.id == "bind_sub_path":
            before = "rewritten (containers); yes, 403 (pods)"
        if c.kind == "compat":
            before = "yes (403: every Docker-API create is refused; the field's own check is unit-tested)"
        if c.kind == "custom" and not c.bad:
            before = {"run_mount_cache": "n/a (isolated TMPDIR)",
                      "run_mount_bind_escape": "n/a (buildah refuses)", "build_tag": "yes (403)",
                      "pod_member": "yes (403 for pasta); pod membership dropped, network replaced"}.get(c.id, "n/a")
        if c.stored:
            after = c.after or {"container": "yes", "pod": "yes (pod inspect)", "infra": "yes (infra container)",
                                "volume": "yes (volume inspect)", "network": "yes (network inspect)"}[c.stored_in]
        else:
            after = c.after or "no"
        notes = (["allowed value tested"] if c.good else []) + ([c.note] if c.note else [])
        rows.append(f"| {c.area} | {c.field} | {before} | {after} | `test_step5_fields.Fields.test_{c.id}` |"
                    f" {'; '.join(notes)} |")
    return "\n".join(rows)


if __name__ == "__main__":
    if "--table" in sys.argv:
        print(table())
    else:
        unittest.main()
