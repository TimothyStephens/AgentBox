# Step 5 coverage: mounts and privilege, field by field (1 Oct)

The independent reviews A2 (mounts) and A3 (privilege and limits) did not finish. This replaces them with a table-driven integration test, `tests/integration/test_step5_fields.py`. It sends raw API requests through the real gate (gate → dev sockguard → podman service) of one dev-tier session. The table below is printed by `python3 -m tests.integration.test_step5_fields --table` from the same `CASES` list, so the two cannot drift.

**How the field list was found.** podman-remote's own requests were captured: `pod create`, `create`, `build`, `volume create` and `network create`, with every flag in these areas. A local proxy answered each create with 500, so nothing was created. The capture showed the JSON names podman actually uses (`shared_namespaces`, `pod_devices`, `conmon_pid_file`, `healthLogDestination`, `artifact_volumes`, `addcaps`, the volume API's `Label`, and others). The gate now refuses any top-level create field it does not know.

**What each case asserts.**
- **Disallowed value:** the gate itself answers 403. Nothing by the case's name is left: no container, pod, volume or network, and no new volume.
- **Post-create read-back:** where podman's inspect shows the field, the test takes a real inspect document of an object created through the gate and puts the disallowed value where podman stores it. The gate's own post-create check must then flag it.
- **Allowed value (where one exists):** the create works, podman stored that value, and the container starts and is removed through the gate.
- **Gate-independent checks:** builds, RUN mounts and the start-time swap are checked by their outcome (no marker read, image IDs unchanged, cache location, container never ran).

**Result.** 99 tests pass: 98 cases and a final teardown check. Run 1 Oct against podman 5.7.0 and sockguard 2.2.1. The run found and fixed:
- **Image volumes after start:** podman reports an image volume as a bind from the container's own storage. The mount checks accept exactly that path.
- **`MaskedPaths: []`** ("unmask everything") passed the gate because an empty list is falsy. Sockguard refused it. Fixed in the gate, with a unit test.
- **Sockguard refusals:** it refuses the forced cgroup parent on Docker-API creates, it cannot resolve pods for a member container, and it refuses all sysctls and all network options. The gate now refuses each up front, with a reason (`docs/decisions.md`).
- **The session's own squid proxy** has an anonymous volume, which teardown did not remove before `rm -v`.
- **The full itest found one more field:** the agent images' podman client sends `volatile` on every create, so the allowlist refused compose's creates. Both clients' requests were then compared against the allowlist (`volatile` was the only gap), and it was added.

Found while building it: podman resolves a bind source when the container **starts**, so a directory swapped for a symlink after create was mounted with the symlink's target. Tim's decision (1 Oct): only a grant root is bound as is. A container's sub-path bind becomes a `SubPath` of the gate's bind-backed volume over that root, which podman resolves inside the volume (the evidence, including a 150-start race, is in `docs/decisions.md`). `test_bind_sub_path` swaps the sub-path for a symlink before start: podman refuses it, and the outside file never shows. Also added: the dev-tier image allowlist (`test_image_run`, `test_image_build_from`), and on 2 Oct the gate's record of its own builds in place of labels (`test_image_forged_label`).

"Not in podman's inspect" means the post-create check cannot see that field, so the refusal before forwarding is the only control for it.

| Area | Field | Refused before forwarding | Checked after create | Test | Notes |
|---|---|---|---|---|---|
| mounts | mounts[type=bind].source (outside the project) | yes (403) | yes | `test_step5_fields.Fields.test_bind_outside` | allowed value tested |
| mounts | mounts[type=bind].source (a symlink in the project pointing out) | yes (403) | yes | `test_step5_fields.Fields.test_bind_symlink_out` |  |
| mounts | mounts[type=bind].source (a path inside the project, not a grant root) | rewritten (containers); yes, 403 (pods) | yes: a bind must be a grant root; a grant volume's device must be one; mountinfo after start | `test_step5_fields.Fields.test_bind_sub_path` | allowed value tested; rewritten: a sub-path becomes a SubPath of the gate's bind-backed volume over its grant root, resolved by podman inside the volume, so a symlink swapped in before start cannot lead out (Tim's option 3, 1 Oct). Pods: sub-paths refused |
| mounts | mounts[type=bind].source (relative path) | yes (403) | yes | `test_step5_fields.Fields.test_bind_relative` |  |
| mounts | volumes[].Name (a relative path) | yes (403) | yes | `test_step5_fields.Fields.test_volume_relative` |  |
| mounts | volumes[].Name (a volume the session did not create) | yes (403) | yes | `test_step5_fields.Fields.test_volume_foreign` | allowed value tested; a new name is created by the gate, labelled, first |
| mounts | volumes[].Options (volume driver options) | yes (403) | options are not stored on the container; the volume's own Driver/Options are (volume cases) | `test_step5_fields.Fields.test_volume_options` | allowed value tested |
| mounts | mounts[type=volume] (a volume through mounts) | yes (403) | yes | `test_step5_fields.Fields.test_volume_mount_type` |  |
| mounts | image_volumes[].Source | yes (403) | Mounts[].Type image allowed (any local image: reads are not scoped, see A4 lows) | `test_step5_fields.Fields.test_image_volume` | allowed value tested; only images already present: no pull past the registry allowlist |
| mounts | image (another project's or a live stack's localhost/ image) | yes (403) | refused before forwarding (the image is chosen before podman stores anything) | `test_step5_fields.Fields.test_image_run` | allowed value tested; Tim, 1-2 Oct: public registry images, this project's builds (the gate's record), or dev_images |
| mounts | image carrying this project's labels, built outside the gate | n/a | refused before forwarding (run and build) | `test_step5_fields.Fields.test_image_forged_label` | Tim, 2 Oct: the project's own builds are the gate's record of image IDs, never labels |
| mounts | build FROM / COPY --from / RUN --mount from= (a localhost/ image) | n/a | no object until the build ends | `test_step5_fields.Fields.test_image_build_from` | the context is spooled and its Containerfile read before forwarding |
| mounts | mounts[type=tmpfs].options | yes (403) | a tmpfs holds only the container's own memory; options are not stored | `test_step5_fields.Fields.test_tmpfs` | allowed value tested; no host reach; only listed mount options pass |
| mounts | mounts[type=devpts] (other mount types) | yes (403) | yes | `test_step5_fields.Fields.test_devpts` |  |
| mounts | overlay_volumes[].source | yes (403) | yes | `test_step5_fields.Fields.test_overlay` |  |
| mounts | artifact_volumes | yes (403) | yes | `test_step5_fields.Fields.test_artifact` |  |
| mounts | volumes_from | yes (403) | yes | `test_step5_fields.Fields.test_volumes_from` |  |
| mounts | rootfs (a host directory as root) | yes (403) | yes | `test_step5_fields.Fields.test_rootfs` |  |
| mounts | init_path (a host binary bind-mounted in) | yes (403) | not in podman's inspect: refused before forwarding only | `test_step5_fields.Fields.test_init_path` |  |
| mounts | base_hosts_file (a host file read in) | yes (403) | not in podman's inspect: refused before forwarding only | `test_step5_fields.Fields.test_hosts_file` |  |
| mounts | conmon_pid_file (a host file written) | yes (403) | yes | `test_step5_fields.Fields.test_conmon_pidfile` |  |
| mounts | log_configuration.path (a host file written) | yes (403) | yes | `test_step5_fields.Fields.test_log_path` | allowed value tested |
| mounts | healthLogDestination (a host directory written) | yes (403) | yes | `test_step5_fields.Fields.test_health_log` | allowed value tested |
| mounts | secrets / secret_env (podman's secret store) | yes (403) | yes | `test_step5_fields.Fields.test_secrets` |  |
| mounts | HostConfig.Binds (outside the project) | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_binds_outside` |  |
| mounts | HostConfig.Binds (a volume the session did not create) | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_binds_volume` |  |
| mounts | HostConfig.Mounts[].VolumeOptions.DriverConfig | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | the volume's Driver/Options (volume cases) | `test_step5_fields.Fields.test_compat_mount_driver` |  |
| mounts | HostConfig.VolumeDriver | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | podman stores no volume driver on the container; volumes are checked by their own labels | `test_step5_fields.Fields.test_compat_volume_driver` |  |
| mounts | HostConfig.VolumesFrom | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_volumes_from` |  |
| mounts | pod mounts[type=bind].source | yes (403) | yes (infra container) | `test_step5_fields.Fields.test_pod_bind_outside` |  |
| mounts | pod volumes_from | yes (403) | yes (infra container) | `test_step5_fields.Fields.test_pod_volumes_from` |  |
| mounts | pod overlay_volumes | yes (403) | refused before forwarding; the infra container's Mounts are checked (pod_bind_outside) | `test_step5_fields.Fields.test_pod_overlay` |  |
| mounts | volume create Options / Driver | yes (403) | yes (volume inspect) | `test_step5_fields.Fields.test_volume_create_options` | allowed value tested |
| mounts | volume create Driver | yes (403) | yes (volume inspect) | `test_step5_fields.Fields.test_volume_create_driver` |  |
| mounts | volume create IgnoreIfExists | yes (403) | refused before forwarding (an existing volume would come back) | `test_step5_fields.Fields.test_volume_create_exists` |  |
| mounts | build volume= | yes (403) | no object until the build ends | `test_step5_fields.Fields.test_build_volume` |  |
| mounts | build secrets= (src) | yes (403) | no object until the build ends | `test_step5_fields.Fields.test_build_secret` | allowed: a secret file inside the context |
| mounts | build additionalbuildcontexts= / remote= | yes (403) | no object until the build ends | `test_step5_fields.Fields.test_build_context` |  |
| mounts | build outputs= (writes on the host) | yes (403) | no object until the build ends | `test_step5_fields.Fields.test_build_outputs` |  |
| mounts | RUN --mount=type=cache | n/a (isolated TMPDIR) | not visible to the gate (inside the context) | `test_step5_fields.Fields.test_run_mount_cache` | the session's podman service has its own TMPDIR, so the cache is the session's alone |
| mounts | RUN --mount=type=bind,source=<outside the context> | n/a (buildah refuses) | not visible to the gate (inside the context) | `test_step5_fields.Fields.test_run_mount_bind_escape` | refused by buildah, not by the gate |
| mounts | build t= / output= (an existing image's name) | yes (403) | no object until the build ends | `test_step5_fields.Fields.test_build_tag` |  |
| privilege | privileged | yes (403) | yes | `test_step5_fields.Fields.test_privileged` | allowed value tested |
| privilege | cap_add / cap_drop | yes (403) | yes | `test_step5_fields.Fields.test_cap_add` | allowed value tested |
| privilege | devices | yes (403) | yes | `test_step5_fields.Fields.test_devices` |  |
| privilege | device_cgroup_rule | yes (403) | not in podman's inspect: refused before forwarding only | `test_step5_fields.Fields.test_device_cgroup_rule` |  |
| privilege | seccomp_profile_path / seccomp_policy | yes (403) | yes | `test_step5_fields.Fields.test_seccomp` | allowed value tested |
| privilege | seccomp_policy=image | yes (403) | the effective profile shows in SecurityOpt (seccomp case) | `test_step5_fields.Fields.test_seccomp_image` |  |
| privilege | apparmor_profile | yes (403) | yes | `test_step5_fields.Fields.test_apparmor` |  |
| privilege | selinux_opts / label_nested | yes (403) | yes | `test_step5_fields.Fields.test_selinux` |  |
| privilege | unmask | yes (403) | yes | `test_step5_fields.Fields.test_unmask` | allowed value tested |
| privilege | no_new_privileges | n/a | any value allowed; only no-new-privileges may appear in SecurityOpt | `test_step5_fields.Fields.test_no_new_privileges` | allowed value tested |
| privilege | pidns | yes (403) | yes | `test_step5_fields.Fields.test_pidns` | allowed value tested |
| privilege | ipcns | yes (403) | yes | `test_step5_fields.Fields.test_ipcns` | allowed value tested |
| privilege | utsns | yes (403) | yes | `test_step5_fields.Fields.test_utsns` | allowed value tested |
| privilege | userns | yes (403) | yes | `test_step5_fields.Fields.test_userns` | allowed value tested |
| privilege | cgroupns | yes (403) | yes | `test_step5_fields.Fields.test_cgroupns` | allowed value tested |
| privilege | netns | yes (403) | yes | `test_step5_fields.Fields.test_netns` | allowed value tested |
| privilege | netns pasta + network_options | yes (403) | yes | `test_step5_fields.Fields.test_netns_pasta` |  |
| privilege | sysctl | yes (403) | yes | `test_step5_fields.Fields.test_sysctl` | every sysctl: the dev sockguard refuses them all, so the gate does too |
| privilege | annotations | yes (403) | yes | `test_step5_fields.Fields.test_annotations` | allowed value tested |
| privilege | oom_score_adj | yes (403) | yes | `test_step5_fields.Fields.test_oom_score` | allowed value tested |
| privilege | cgroup_parent / cgroups_mode | yes (403) | yes | `test_step5_fields.Fields.test_cgroup_parent` |  |
| privilege | cgroups_mode=disabled | yes (403) | yes | `test_step5_fields.Fields.test_cgroups_disabled` |  |
| privilege | resource_limits.memory (limit, swap, unified, OOM killer) | yes (403) | yes | `test_step5_fields.Fields.test_memory` | allowed value tested |
| privilege | resource_limits.memory.disableOOMKiller | yes (403) | yes | `test_step5_fields.Fields.test_memory_oomkiller` |  |
| privilege | resource_limits.pids | yes (403) | yes | `test_step5_fields.Fields.test_pids` | allowed value tested |
| privilege | oci_runtime (a host binary run as the runtime) | yes (403) | yes | `test_step5_fields.Fields.test_oci_runtime` |  |
| privilege | env_host (the podman service's environment) | yes (403) | not distinguishable in inspect: refused before forwarding only | `test_step5_fields.Fields.test_env_host` |  |
| privilege | dependencyContainers (starts other containers) | yes (403) | yes | `test_step5_fields.Fields.test_dependencies` |  |
| privilege | groups keep-groups | yes (403) | yes | `test_step5_fields.Fields.test_keep_groups` | allowed value tested |
| privilege | health_check_on_failure_action=restart | yes (403) | yes | `test_step5_fields.Fields.test_health_restart` | a restart by podman itself would skip the gate's start check |
| privilege | portmappings / publish_image_ports | yes (403) | yes | `test_step5_fields.Fields.test_ports` |  |
| privilege | any field the gate does not know | yes (403) | refused before forwarding | `test_step5_fields.Fields.test_unknown_field` |  |
| privilege | HostConfig.Privileged / CapAdd / Devices | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_privileged` |  |
| privilege | HostConfig.SecurityOpt | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_securityopt` |  |
| privilege | HostConfig.PidMode / IpcMode / UTSMode / UsernsMode / CgroupnsMode | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_pidmode` |  |
| privilege | HostConfig.Sysctls | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_sysctls` |  |
| privilege | HostConfig.Annotations | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_annotations` |  |
| privilege | HostConfig.OomScoreAdj / OomKillDisable | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_oom` |  |
| privilege | HostConfig.CgroupParent / Cgroup | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_cgroup` |  |
| privilege | HostConfig.Memory / MemorySwap / PidsLimit | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_memory` |  |
| privilege | HostConfig.Runtime | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_runtime` |  |
| privilege | HostConfig.MaskedPaths / ReadonlyPaths | yes (403: every Docker-API create is refused; the field's own check is unit-tested) | yes | `test_step5_fields.Fields.test_compat_masked` |  |
| privilege | pod portmappings | yes (403) | yes (pod inspect) | `test_step5_fields.Fields.test_pod_ports` |  |
| privilege | pod pod_devices | yes (403) | yes (infra container) | `test_step5_fields.Fields.test_pod_devices` |  |
| privilege | pod security_opt | yes (403) | yes (infra container) | `test_step5_fields.Fields.test_pod_security_opt` |  |
| privilege | pod netns / pidns / utsns / userns | yes (403) | yes (pod inspect) | `test_step5_fields.Fields.test_pod_netns` |  |
| privilege | pods with an infra container (no_infra false; shared_namespaces) | yes (403) | yes: with infra, SharedNamespaces must hold net and InfraConfig.Networks == the session network | `test_step5_fields.Fields.test_pod_infra` | allowed value tested; refused: the dev sockguard cannot resolve pods, so no container could join one |
| privilege | a container joining a pod (pod, netns, Networks) | yes (403 for pasta); pod membership dropped, network replaced | yes: networks == the session network; in an infra pod, NetworkMode container:<infra> | `test_step5_fields.Fields.test_pod_member` |  |
| privilege | pod sysctl | yes (403) | yes (infra container) | `test_step5_fields.Fields.test_pod_sysctl` |  |
| privilege | pod infra_image / infra_command / infra_conmon_pid_file | yes (403) | refused before forwarding only | `test_step5_fields.Fields.test_pod_infra_image` |  |
| privilege | pod cgroup_parent | yes (403) | yes (pod inspect) | `test_step5_fields.Fields.test_pod_cgroup` |  |
| privilege | network create driver / network_interface | yes (403) | yes (network inspect) | `test_step5_fields.Fields.test_network_driver` | allowed value tested |
| privilege | network create subnets / routes / ipam_options | yes (403) | the session network is the only one used | `test_step5_fields.Fields.test_network_subnet` |  |
| privilege | network create options (isolate, mtu, ...) | yes (403) | yes (network inspect) | `test_step5_fields.Fields.test_network_options` | no options at all: the dev sockguard refuses them, so isolate=strict cannot be set either |
| privilege | network create internal / ipv6_enabled | yes (403) | yes (network inspect) | `test_step5_fields.Fields.test_network_internal` |  |
| privilege | network create (Docker API) Driver / IPAM | yes (403) | no | `test_step5_fields.Fields.test_compat_network` | allowed value tested |

---

## Reopened: covers were not enforced for spawned containers (5 Oct)

The mount coverage above tested gate enforcement of bind *sources* (grant roots, sub-paths, symlink swaps) but
assumed the agent's read-only/empty covers (secret masks, `.git/hooks`, `.git/config`, compose and lockdown
covers, the `.env` dev-swap) protected those paths. They do not for a container the agent starts through the
gate: those covers are in the agent's own mount namespace only. A `-v` of the whole project root bound it
writable with no covers. See `docs/decisions.md` (5 Oct, "Covers are enforced at the gate").

Closed by making the covers a gate bind check: the gate refuses any bind (grant-root, sub-path/grant-volume, or
compat `Binds`/`Mounts`) whose canonical source resolves onto, under, or over a covered path, and the
after-start mountinfo check refuses a grant-volume SubPath that a swap redirected onto a cover.

Added to this area's tests: `tests/integration/test_step6_covers.py` (the spec's §5.2 bypass test, run from a
gate-spawned container — a must-pass), `test_review_step5.Covers`, `test_session.CoveredPaths`. The field
test's allowed bind case now uses a cover-free grant, because the project root contains covers and is refused.
