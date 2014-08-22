import os
import re
import datetime
import sys
import time
import getpass
from functools import wraps

from fabric.api import *
from fabric.contrib.console import confirm
from fabric.api import execute, run
from fabric.colors import green, yellow, cyan

import yaml


class HostPropertyProxy(object):
    """
    Host-specific proxy for property access
    """
    def __init__(self, obj):
        self._obj = obj

    def __getattr__(self, attr):
        return self._obj.get(env.host, {}).get(attr, getattr(env, attr))


CONFIG_FILE = "config.py"
CLUSTERS_FILE = os.path.join(os.getcwd(), 'clusters.yaml')
ALL_PROPERTIES = ['hostname', 'branch', 'remote', 'indico_dir', 'virtualenv', 'install_resources']

execfile(CONFIG_FILE, {}, env)

env.res_dir = os.path.join(env.src_base_dir, 'resources')
env.code_dir = os.path.join(env.src_base_dir, 'indico')
env.datetime = datetime.datetime.now()
env.user = os.environ.get('KRB_REAL_USER', getpass.getuser())

env._host_property_tree = {}
env.host_properties = HostPropertyProxy(env._host_property_tree)


# Utility functions

def process_node_properties(cluster_members):
    host_list = []

    for member in cluster_members:
        if isinstance(member, dict):
            host = member.pop('hostname')
            env._host_property_tree[host] = member
            host_list.append(host)
        else:
            env._host_property_tree[member] = {}
            host_list.append(member)

    return host_list


def load_cluster(cluster_name):
    """
    Loads cluster info from file into environment
    """
    clusters = yaml.load(open(CLUSTERS_FILE, 'r'))

    cluster_info = clusters.get(cluster_name)

    if cluster_info is not None:
        env.branch = cluster_info.get('branch', env.branch)
        env.remote = cluster_info.get('remote', env.remote)
        env.py_version = cluster_info.get('py_version', env.py_version)
        env.virtualenv = cluster_info.get('virtualenv', env.virtualenv)
        env.hosts = process_node_properties(cluster_info['machines'])
    else:
        if confirm("Did you mean 'server:{0}'?".format(cluster_name)):
            env.hosts = [cluster_name]
        else:
            sys.exit(-1)


def with_virtualenv(func):
    @wraps(func)
    def _func(*args, **kwargs):
        if env.virtualenv:
            virtualenv_bin = os.path.join(env.host_properties.virtualenv, 'bin/')
        else:
            virtualenv_bin = ''

        return func(virtualenv_bin, *args, **kwargs)

    return _func


def print_node_properties(hostname):
    properties = env._host_property_tree.get(hostname, {})
    properties['hostname'] = hostname

    for key in ALL_PROPERTIES:
        # print property from clusters.yaml, or environment default otherwise
        default = getattr(env, key, None)
        print " * {0}: {1}".format(cyan(key, bold=True), yellow(properties.get(key, default)))


# Sub-tasks

def _tarball():
    text = local('{0} setup.py sdist'.format(env.PYTHON_EXEC), capture=True)

    text = text.replace('\n', '')
    m = re.match(r".*Writing (.*)/setup\.cfg.*", text)

    if not m:
        abort('*.tar.gz not found in setup.py output')

    return m.group(1)


def _copy_resources():
    """
    Copies CERN resources to the file tree
    """

    with lcd(env.code_dir):
        local('cp -r {res_dir}/images/* {code_dir}/indico/htdocs/images'.format(**env))
        local('cp -r {res_dir}/scripts/FoundationSync {code_dir}/indico/MaKaC/common'.format(**env))


def _build_resources():
    res_files = []
    yp_dir = os.path.join(env.res_dir, 'plugins', 'epayment', 'CERNYellowPay')
    cs_dir = os.path.join(env.res_dir, 'plugins', 'search', 'cern_search')

    with lcd(yp_dir):
        res_files.append(os.path.join(yp_dir, 'dist', _tarball() + ".tar.gz"))

    with lcd(cs_dir):
        res_files.append(os.path.join(cs_dir, 'dist', _tarball() + ".tar.gz"))

    return res_files


def _checkout_sources():
    with lcd(env.code_dir):
        local('git fetch {0}'.format(env.remote))
        local('git checkout {remote}/{branch}'.format(**env.host_properties))


def _build_sources():

    with lcd(env.code_dir):
        local('fab package_release:no_clean=True,py_versions={0},build_here=t'.format(env.py_version))
        egg_name = local("find dist -name '*.egg' | head -1", capture=True)

    return [os.path.join(env.code_dir, egg_name)]


@with_virtualenv
def _install(virtualenv_bin, files, no_deps=False):
    sudo('mkdir -p {0}'.format(env.remote_tmp_dir))
    sudo('chmod 777 {0}'.format(env.remote_tmp_dir))

    for fpath in files if env.host_properties.install_resources else files[:1]:
        remote_fname = os.path.join(env.remote_tmp_dir, os.path.basename(fpath))
        sudo("rm '{0}'".format(remote_fname), warn_only=True)
        put(fpath, env.remote_tmp_dir)
        sudo("{0}{1}{2} --always-unzip '{3}'".format(virtualenv_bin,
                                                     env.EASY_INSTALL_EXEC,
                                                     " --no-deps" if no_deps else "",
                                                     remote_fname))


def _cleanup(files):
    for fpath in files:
        print yellow(" * Deleting {0}".format(fpath))
        local("rm '{0}'".format(fpath))


# Modifiers

@task
def cluster(name):
    load_cluster(name)


@task
def server(name):
    env.hosts = [name]


@task
def remote(name):
    env.remote = name


@task
def branch(name):
    env.branch = name


@task
def venv(path):
    env.virtualenv = path


# Deployment tasks

@task
def touch_files():
    with settings(warn_only=True):
        sudo("find {0}/htdocs/ -type d -name '.webassets-cache' -exec rm -rf {{}} \;".format(
            env.host_properties.indico_dir))
    sudo("find {0}/htdocs -exec touch -d '{1}' {{}} \;".format(
        env.host_properties.indico_dir, env.datetime.strftime('%a, %d %b %Y %X %z')))


@task
@with_virtualenv
def configure(virtualenv_bin):

    sudo('{0}indico_initial_setup --existing-config={1}/etc/indico.conf'.format(
        virtualenv_bin,
        env.host_properties.indico_dir))


@task
def restart_apache(t=0, graceful=True):
    if graceful:
        sudo('touch {0}/htdocs/indico.wsgi'.format(env.host_properties.indico_dir))
        run('sudo -E service httpd graceful')
    else:
        run('sudo -E service httpd restart')
    time.sleep(int(t))


@task
def install_node(files, no_deps=False):

    print cyan("Deploying into node:", bold=True)
    print_node_properties(env.host)
    print

    _install(files, no_deps=no_deps)
    configure()
    touch_files()
    restart_apache()


# Main tasks

@task
@with_virtualenv
def apply_patch(virtualenv_bin, path):
    """
    Applies a 'live' patch to Indico's code
    """
    indicoPkgPath = run("{0}{1} -c 'import os, MaKaC; "
                        "print os.path.split(os.path.split(MaKaC.__file__)[0])[0]'".format(
                            virtualenv_bin, env.PYTHON_EXEC))

    put(path, env.remote_tmp_dir)
    patch_path = os.path.join(env.remote_tmp_dir, os.path.basename(path))

    print yellow("Patching {0}".format(indicoPkgPath))

    with cd(indicoPkgPath):
        sudo('patch -p1 < {0}'.format(patch_path))


@task
def deploy(cluster="dev", no_deps=False, cleanup=True):
    """
    Deploys Indico
    """
    files = []

    # if no cluster/server has been specified through another option
    if not env.hosts:
        load_cluster(cluster)

    confirm('Are you sure you want to install?')

    _checkout_sources()
    _copy_resources()
    files += _build_sources()
    files += _build_resources()

    print green("File list:")
    print yellow('\n'.join("  * {0}".format(fpath) for fpath in files))

    execute(install_node, files, no_deps=no_deps)

    _cleanup(files)
