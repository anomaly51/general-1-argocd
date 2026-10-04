"""Expand only VM109's audited root PV/LV after its disk grew from20 to60GiB."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

DISK = '/dev/sda'
PV = '/dev/sda5'
VG = 'MiWiFi-RD18-srv-vg'
ROOT = '/dev/' + VG + '/root'
OLD_ROOT_BYTES = 19348324352
TARGET_ROOT_BYTES = OLD_ROOT_BYTES + 40 * 1024**3
STATE = Path('/var/lib/ci-worker-storage')


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def table(tool, section, fields):
    return json.loads(run(tool, '--reportformat', 'json', '--units', 'b',
                          '--nosuffix', '-o', fields))['report'][0][section]


def partition_table():
    return json.loads(run('sfdisk', '--json', DISK))['partitiontable']


def main():
    require(socket.gethostname() == 'general-1-worker-1', 'Wrong node')
    require(Path('/etc/machine-id').read_text().strip() == 'b233eaca510b4f35a4a3715cf11fa89d', 'Wrong VM')
    require(int(run('blockdev', '--getsize64', DISK)) == 60 * 1024**3, 'Expected approved60GiB disk')
    require(run('findmnt', '-no', 'FSTYPE', '/') == 'ext4', 'Unexpected filesystem')
    require(Path(run('findmnt', '-no', 'SOURCE', '/')).resolve() == Path(ROOT).resolve(), 'Unexpected root LV')
    require(shutil.which('growpart') is not None, 'growpart missing; no mutation performed')
    require(run('cloud-init', 'status') == 'status: done', 'Cloud-init must finish before resizing')
    pv = next(p for p in table('pvs', 'pv', 'pv_name,pv_uuid,vg_name,vg_uuid') if p['pv_name'] == PV)
    require(pv['pv_uuid'] == 'WjwOZE-RJaa-HZwb-Uh8o-Vn6C-DdZB-weYsr7', 'PV changed')
    require(pv['vg_uuid'] == 'ZdZBWF-AocX-mcr7-xJeq-eD6a-3vL0-LYmJ3a', 'VG changed')
    lv = next(v for v in table('lvs', 'lv', 'lv_name,lv_uuid,lv_size,vg_name') if v['vg_name'] == VG and v['lv_name'] == 'root')
    require(lv['lv_uuid'] == 'o9ZmQc-DJGB-O52w-4JP9-2By7-VCzN-VfMcrD', 'Root LV changed')
    require(int(float(lv['lv_size'])) in {OLD_ROOT_BYTES, TARGET_ROOT_BYTES}, 'Unexpected root size')
    before = partition_table()
    require(before['label'] == 'dos', 'Expected original MBR partition table')
    require({p['node'] for p in before['partitions']} == {'/dev/sda1', '/dev/sda2', PV}, 'Unexpected partitions')
    require(next(p for p in before['partitions'] if p['node'] == '/dev/sda2')['type'] in {'5', 'f'}, 'Expected extended partition')
    require({p['node']: p['start'] for p in before['partitions']} ==
            {'/dev/sda1': 2048, '/dev/sda2': 1982462, PV: 1982464}, 'Partition starts differ from audit')
    STATE.mkdir(mode=0o700, exist_ok=True)
    if not (STATE / 'partition-table.before').exists():
        (STATE / 'partition-table.before').write_text(run('sfdisk', '--dump', DISK) + '\n')
    if not (STATE / 'volume-group.before').exists():
        run('vgcfgbackup', '-f', str(STATE / 'volume-group.before'), VG)
    require((STATE / 'partition-table.before').stat().st_size > 0 and
            (STATE / 'volume-group.before').stat().st_size > 0, 'Both metadata backups are required')
    # The extended container must grow before its logical LVM partition.
    # Debian growpart also pvresizes a changed LVM partition. All identity,
    # capacity and backup guards above intentionally precede either invocation.
    for number in ('2', '5'):
        result = subprocess.run(['growpart', DISK, number], text=True, capture_output=True)
        require(result.returncode == 0 or (result.returncode == 1 and 'NOCHANGE' in result.stdout), 'growpart failed')
    after = partition_table()
    for old in before['partitions']:
        new = next(p for p in after['partitions'] if p['node'] == old['node'])
        require(new['start'] == old['start'] and new['type'] == old['type'], 'Partition identity changed')
        require(new['size'] >= old['size'], 'Partition shrunk')
        if old['node'] == '/dev/sda1':
            require(new == old, 'Boot partition changed')
    require(int(run('blockdev', '--getsize64', PV)) > 58 * 1024**3, 'Kernel has stale partition size; stopping')
    run('pvresize', PV)
    if int(float(lv['lv_size'])) < TARGET_ROOT_BYTES:
        run('lvextend', '--yes', '--size', str(TARGET_ROOT_BYTES) + 'B', ROOT, PV)
    run('resize2fs', ROOT)
    capacity = os.statvfs('/').f_blocks * os.statvfs('/').f_frsize
    require(capacity > 56 * 1024**3, 'Filesystem did not grow')
    print('Verified worker-1: root filesystem expanded; partition starts and swap disks unchanged.')


if __name__ == '__main__':
    main()
