import json
import os
import logging
import re

from cStringIO import StringIO

from . import ansible

from ..config import config as teuth_config
from ..misc import get_scratch_devices
from teuthology import misc as teuthology
from teuthology.orchestra import run
from tasks import setup_installer
log = logging.getLogger(__name__)


class CephAnsible(ansible.Ansible):
    name = 'ceph_ansible'

    _default_playbook = [
        dict(
            hosts='mons',
            become=True,
            roles=['ceph-mon'],
        ),
        dict(
            hosts='osds',
            become=True,
            roles=['ceph-osd'],
        ),
        dict(
            hosts='mdss',
            become=True,
            roles=['ceph-mds'],
        ),
        dict(
            hosts='rgws',
            become=True,
            roles=['ceph-rgw'],
        ),
        dict(
            hosts='restapis',
            become=True,
            roles=['ceph-restapi'],
        ),
    ]

    _default_rh_playbook = [
        dict(
            hosts='mons',
            become=True,
            roles=['ceph-mon'],
        ),
        dict(
            hosts='osds',
            become=True,
            roles=['ceph-osd'],
        ),
        dict(
            hosts='mdss',
            become=True,
            roles=['ceph-mds'],
        ),
        dict(
            hosts='rgws',
            become=True,
            roles=['ceph-rgw'],
        ),
    ]

    __doc__ = """
    A subclass of Ansible that defaults to:

    - ansible:
        repo: {git_base}ceph-ansible.git
        playbook: {playbook}

    It always uses a dynamic inventory.

    It will optionally do the following automatically based on ``vars`` that
    are passed in:
        * Set ``devices`` for each host if ``osd_auto_discovery`` is not True
        * Set ``monitor_interface`` for each host if ``monitor_interface`` is
          unset
        * Set ``public_network`` for each host if ``public_network`` is unset
    """.format(
        git_base=teuth_config.ceph_git_base_url,
        playbook=_default_playbook,
    )

    def __init__(self, ctx, config):
        config = config or dict()
        if 'playbook' not in config:
            if config.get('rhbuild'):
                config['playbook'] = self._default_rh_playbook
                setup_installer.setup_installer(ctx, config)
            else:
                config['playbook'] = self._default_playbook
        if 'repo' not in config:
            config['repo'] = os.path.join(teuth_config.ceph_git_base_url,
                                          'ceph-ansible.git')
        super(CephAnsible, self).__init__(ctx, config)

    def execute_playbook(self, _logfile=None):
        """
        Execute ansible-playbook

        :param _logfile: Use this file-like object instead of a LoggerFile for
                         testing
        """
        environ = os.environ
        environ['ANSIBLE_SSH_PIPELINING'] = '1'
        environ['ANSIBLE_FAILURE_LOG'] = self.failure_log.name
        environ['ANSIBLE_ROLES_PATH'] = "%s/roles" % self.repo_path
        args = self._build_args()
        log.debug("Running %s", args)
        if self.config.get('rhbuild'):
            (ceph_installer,) = self.ctx.cluster.only(
                teuthology.get_first_mon(self.ctx,
                                         self.config)).remotes.iterkeys()
            self.installer_node = ceph_installer
            ceph_installer.put_file(args[5], args[5])
            ceph_installer.put_file(args[8], args[8])
            ceph_installer.run(args=['cp', '-R',
                                     '/usr/share/ceph-ansible', '.'])
            ceph_installer.run(args=('cat', args[8], run.Raw('>'),
                                     'ceph-ansible/site.yml'))
            ceph_installer.run(args=('cat', args[5]))
            ceph_installer.run(args=('cat', 'ceph-ansible/site.yml'))
            args[8] = 'site.yml'
            out = StringIO()
            str_args = ' '.join(args)
            ceph_installer.run(args=['cd', 'ceph-ansible', run.Raw(';'),
                                     run.Raw(str_args)],
                               timeout=4200,
                               check_status=False,
                               stdout=out)
            log.info(out.getvalue())
            if re.search(r'all hosts have already failed',out.getvalue()):
                log.error("Failed during ansible execution")
                raise CephAnsibleError("Failed during ansible execution")
        else:
            super(CephAnsible, self).execute_playbook()

    def teardown(self):
        if self.config.get('rhbuild'):
            self.ctx.cluster.run(args=['sudo', 'systemctl', 'stop',
                                       'ceph.target'],
                                 check_status=False)
            self.ctx.cluster.run(args=['sudo', 'stop', 'ceph-all'],
                                 check_status=False)
            installer_node = self.installer_node
            installer_node.run(args=['rm', '-rf', 'ceph-ansible'])
            if installer_node.os.package_type == 'rpm':
                installer_node.run(args=['sudo', 'yum', 'remove', '-y',
                                         'ceph-ansible'])
                installer_node.run(args=['sudo', 'yum', 'remove', '-y', 'ceph',
                                         'ceph-mon', 'ceph-osd', 'ceph-common',
                                         'librbd1', 'librados2',
                                         'libapache2-mod-fastcgi',
                                         'ceph-selinux', 'python-cephfs',
                                         'ceph-base', 'ceph-mds'
                                         'python-rbd', 'python-rados'])
            else:
                installer_node.run(args=['sudo', 'apt-get', 'remove',
                                         '-y', 'ceph-ansible'])
        else:
            super(CephAnsible, self).teardown()

    def get_inventory(self):
        """
        Stub this method so we always generate the hosts file
        """
        pass

    def generate_hosts_file(self):
        groups_to_roles = dict(
            mons='mon',
            mdss='mds',
            osds='osd',
        )
        hosts_dict = dict()
        for group in sorted(groups_to_roles.keys()):
            role_prefix = groups_to_roles[group]
            want = lambda role: role.startswith(role_prefix)
            for (remote, roles) in self.cluster.only(want).remotes.iteritems():
                hostname = remote.hostname
                host_vars = self.get_host_vars(remote)
                if group not in hosts_dict:
                    hosts_dict[group] = {hostname: host_vars}
                elif hostname not in hosts_dict[group]:
                    hosts_dict[group][hostname] = host_vars

        hosts_stringio = StringIO()
        for group in sorted(hosts_dict.keys()):
            hosts_stringio.write('[%s]\n' % group)
            for hostname in sorted(hosts_dict[group].keys()):
                vars = hosts_dict[group][hostname]
                if vars:
                    vars_list = []
                    for key in sorted(vars.keys()):
                        vars_list.append(
                            "%s='%s'" % (key, json.dumps(vars[key]).strip('"'))
                        )
                    host_line = "{hostname} {vars}".format(
                        hostname=hostname,
                        vars=' '.join(vars_list),
                    )
                else:
                    host_line = hostname
                hosts_stringio.write('%s\n' % host_line)
            hosts_stringio.write('\n')
        hosts_stringio.seek(0)
        self.inventory = self._write_hosts_file(hosts_stringio.read().strip())
        self.generated_inventory = True

    def get_host_vars(self, remote):
        extra_vars = self.config.get('vars', dict())
        host_vars = dict()
        if not extra_vars.get('osd_auto_discovery', False):
            host_vars['devices'] = get_scratch_devices(remote)
        if 'monitor_interface' not in extra_vars:
            host_vars['monitor_interface'] = remote.interface
        if 'public_network' not in extra_vars:
            host_vars['public_network'] = remote.cidr
        return host_vars

class CephAnsibleError(Exception):
    pass

task = CephAnsible
