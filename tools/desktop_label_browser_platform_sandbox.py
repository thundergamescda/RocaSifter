"""UNRUN fresh GitHub VM Chrome sandbox observation, not a Docker assertion.

The platform's existing Chrome AppArmor policy is observed, never selected,
modified or replaced. Native execution requires Root's separate source grant.
No browser/renderer fallback or privileged host operation is implemented.
"""
import os
from pathlib import Path
import re
import time


class SandboxFailure(RuntimeError):
    pass


def check(value, code):
    if not value:
        raise SandboxFailure(code)


def bounded(path, maximum=16384):
    with Path(path).open('rb') as stream:
        raw = stream.read(maximum + 1)
    check(len(raw) <= maximum, 'PLATFORM_SANDBOX_PROC_READ_BOUND')
    return raw


def process(pid):
    check(type(pid) is int and pid > 0, 'PLATFORM_ACTUAL_SANDBOX_PID')
    root = Path('/proc') / str(pid)
    started = bounded(root / 'stat', 8192).decode('ascii').rsplit(') ', 1)[1].split()[19]
    values = {}
    for line in bounded(root / 'status').decode('ascii').splitlines():
        key, _, value = line.partition(':')
        check(key not in values, 'PLATFORM_DUPLICATE_PROC_STATUS_KEY')
        values[key] = value.strip()
    command = bounded(root / 'cmdline', 16384).split(b'\0')
    forbidden = (b'--no-sandbox', b'--disable-setuid-sandbox', b'--disable-seccomp-filter-sandbox',
                 b'--disable-namespace-sandbox', b'--single-process', b'--no-zygote-sandbox',
                 b'--remote-debugging-port')
    check(not any(value.split(b'=', 1)[0] in forbidden for value in command),
          'PLATFORM_NO_SANDBOX_OR_REMOTE_DEBUGGING_BYPASS')
    result = {'pid': pid, 'start_ticks': started, 'parent_pid': int(values['PPid']),
        'uid': int(values['Uid'].split()[1]), 'gid': int(values['Gid'].split()[1]),
        'no_new_privileges': int(values['NoNewPrivs']), 'seccomp': int(values['Seccomp']),
        'seccomp_filters': int(values.get('Seccomp_filters', '0')),
        'capabilities': {name: values[name] for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')},
        'apparmor': bounded(root / 'attr/current').decode('ascii').strip(),
        'namespaces': {name: os.readlink(root / 'ns' / name) for name in ('user', 'pid', 'net', 'mnt')},
        'uid_map': [list(map(int, row.split())) for row in bounded(root / 'uid_map').decode('ascii').splitlines()],
        'gid_map': [list(map(int, row.split())) for row in bounded(root / 'gid_map').decode('ascii').splitlines()],
        'executable': str((root / 'exe').resolve(strict=True)),
        'remote_debugging_pipe': b'--remote-debugging-pipe' in command}
    check(started == bounded(root / 'stat', 8192).decode('ascii').rsplit(') ', 1)[1].split()[19],
          'PLATFORM_SANDBOX_PROCESS_REPLACED')
    check(result['uid'] == os.geteuid() > 0 and result['gid'] == os.getegid() > 0
          and result['no_new_privileges'] == 1 and result['seccomp'] in (0, 2)
          and all(int(result['capabilities'][name], 16) == 0 for name in
                  ('CapInh', 'CapPrm', 'CapEff', 'CapAmb')),
          'PLATFORM_ACTUAL_NONROOT_NNP_NO_EFFECTIVE_CAPABILITIES')
    return result


def child_debug_transport_fds(browser_pid, driver_pid):
    """Observe real debug descriptors; Node's 'pipe' can be a Unix socketpair.

    Equal FIFO inode observations identify a shared pipe. Different socketpair
    endpoints do not identify their peer merely from /proc symlink numbers.
    The latter is recorded without an invented kernel peer-equality assertion.
    """
    node = Path('/proc') / str(driver_pid) / 'fd'
    entries = list(node.iterdir())
    check(len(entries) <= 128, 'PLATFORM_NODE_FD_BOUND')
    targets = {}
    for entry in entries:
        check(entry.name.isdecimal(), 'PLATFORM_NODE_FD_NUMBER')
        target = os.readlink(entry)
        if re.fullmatch(r'(?:pipe|socket):\[[0-9]+\]', target):
            targets.setdefault(target, []).append(int(entry.name))
    result = []
    for index in (3, 4):
        target = os.readlink(Path('/proc') / str(browser_pid) / 'fd' / str(index))
        check(re.fullmatch(r'(?:pipe|socket):\[[0-9]+\]', target), 'PLATFORM_ACTUAL_BROWSER_DEBUG_DESCRIPTOR')
        kind = target.split(':', 1)[0]
        candidates = sorted(value for node_target, values in targets.items()
                            if node_target.startswith(kind + ':') for value in values if value > 2)
        check(candidates, 'PLATFORM_NODE_HAS_LOCAL_DEBUG_TRANSPORT_DESCRIPTORS')
        matching = sorted(value for value in targets.get(target, ()) if value > 2)
        if kind == 'pipe':
            check(matching, 'PLATFORM_BROWSER_NODE_FIFO_INODE_MUST_MATCH')
        result.append({'browser_fd': index, 'target': target, 'node_candidate_fds': candidates,
                       'equal_pipe_inode_node_fds': matching if kind == 'pipe' else [],
                       'kernel_peer_equality_proved': kind == 'pipe' and bool(matching)})
    check(result[0]['target'] != result[1]['target'], 'PLATFORM_DISTINCT_BROWSER_DEBUG_PIPES')
    return {'browser_descriptors': result,
            'node_local_transport_fds': [{'target': key, 'fds': sorted(value)}
                                         for key, value in sorted(targets.items())],
            'limit': 'For Unix socketpairs, distinct endpoint symlink inodes do not prove peers. Actual parent/argv/FD facts and the pinned local-launch source support the separate conditional API-scope review; no network-isolation or kernel socket-peer proof is inferred.'}


def sandbox_probe(browser, context, driver_pid, expected_browser, expected_version):
    check(browser.version == expected_version, 'PLATFORM_OBSERVED_BROWSER_VERSION_CHANGED')
    check(expected_browser == '/opt/google/chrome/chrome', 'PLATFORM_ONLY_OFFICIAL_INSTALLED_CHROME_PATH')
    check(bounded('/sys/module/apparmor/parameters/enabled', 16).strip() == b'Y',
          'PLATFORM_DEFAULT_APPARMOR_MUST_STAY_ENABLED')
    baseline, driver = process(os.getpid()), process(driver_pid)
    check(driver['parent_pid'] == os.getpid(), 'PLATFORM_WITNESS_REAL_NODE_PARENT')
    page = context.new_page()
    try:
        page.goto('data:text/html,<title>Owned platform sandbox readiness</title>',
                  wait_until='domcontentloaded')
        session = browser.new_browser_cdp_session()
        try:
            information = session.send('SystemInfo.getProcessInfo')['processInfo']
        finally:
            session.detach()
        check(type(information) is list and len(information) <= 128, 'PLATFORM_BROWSER_PROCESS_INFO_BOUND')
        main = [process(int(row['id'])) for row in information if row['type'] == 'browser']
        renderers = [process(int(row['id'])) for row in information if row['type'] == 'renderer']
        check(len(main) == 1 and renderers and len({row['pid'] for row in main + renderers}) ==
              len(main) + len(renderers), 'PLATFORM_ACTUAL_BROWSER_RENDERER_IDENTITIES')
        chrome = main[0]
        check(chrome['parent_pid'] == driver_pid and chrome['executable'] == expected_browser
              and chrome['remote_debugging_pipe'], 'PLATFORM_REAL_CHROME_PARENT_EXE_DEBUG_PIPE')
        # These are existing vendor-profile labels, not docker-default(enforce).
        check(chrome['apparmor'] in ('chrome (unconfined)', 'chrome (enforce)'),
              'PLATFORM_EXISTING_NAMED_VENDOR_CHROME_APPARMOR_LABEL')
        transport_fds = child_debug_transport_fds(chrome['pid'], driver_pid)
        for renderer in renderers:
            check(renderer['executable'] == expected_browser and renderer['seccomp'] == 2
                  and renderer['seccomp_filters'] > chrome['seccomp_filters']
                  and all(renderer['namespaces'][name] != driver['namespaces'][name]
                          for name in ('user', 'pid', 'net')),
                  'PLATFORM_RENDERER_USER_PID_NET_NAMESPACES_AND_ADDITIONAL_SECCOMP')
            for mapping, outside in ((renderer['uid_map'], os.geteuid()),
                                     (renderer['gid_map'], os.getegid())):
                check(len(mapping) == 1 and len(mapping[0]) == 3
                      and mapping[0][1:] == [outside, 1], 'PLATFORM_RENDERER_ONLY_OWNED_UID_GID_MAP')
        for snapshot in (baseline, driver, chrome, *renderers):
            check(process(snapshot['pid']) == snapshot, 'PLATFORM_WITNESS_FINAL_PROCESS_BOOKEND')
        return {'browser': chrome, 'renderers': renderers, 'outer': baseline, 'driver': driver,
            'explicit_chromium_sandbox': True, 'additional_renderer_filter': True,
            'renderer_user_pid_net_namespaces_differ_from_driver': True,
            'actual_browser_node_debug_transport_fd_observations': transport_fds,
            'existing_vendor_apparmor_label_only': True, 'docker_default_enforcement_claim': False,
            'capability_bounding_set_is_observed_platform_fact': True,
            'host_root_or_kernel_policy_changed': False,
            'limit': 'Fresh bounded process observations on a trusted ephemeral GitHub VM. This is not continuous tracing, egress isolation, a Docker boundary, or customer isolation acceptance.'}
    finally:
        page.close()


def owned_browser_processes_gone(identities):
    stop = time.monotonic() + 5
    while True:
        remaining = []
        for identity in identities:
            try:
                raw = bounded(Path('/proc') / str(identity['pid']) / 'stat', 8192)
                ticks = raw.decode('ascii').rsplit(') ', 1)[1].split()[19]
                if ticks == identity['start_ticks']:
                    remaining.append(identity)
            except (FileNotFoundError, ProcessLookupError):
                pass
            except Exception:
                return False
        if not remaining:
            return True
        if time.monotonic() >= stop:
            return False
        time.sleep(0.05)
