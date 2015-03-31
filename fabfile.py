import os
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
        return self._obj.get(env.host, {}).get(attr, getattr(env, attr, None))

    def __getitem__(self, attr):
        return getattr(self, attr)

CONFIG_FILE = "config.py"
CLUSTERS_FILE = os.path.join(os.getcwd(), 'clusters.yaml')
ALL_PROPERTIES = ['hostname', 'branch', 'remote', 'indico_dir', 'virtualenv', 'plugins', 'cern_plugins']

execfile(CONFIG_FILE, {}, env)

env.code_dir = os.path.join(env.src_base_dir, 'indico')
env.plugins_dir = os.path.join(env.src_base_dir, 'indico-plugins')
env.cern_plugins_dir = os.path.join(env.src_base_dir, 'indico-plugins-cern')
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
        env.plugins = set(cluster_info.get('plugins', env.plugins) + cluster_info.get('extra_plugins', []))
        env.cern_plugins = set(cluster_info.get('cern_plugins', env.cern_plugins) +
                               cluster_info.get('extra_cern_plugins', []))
        env.hosts = process_node_properties(cluster_info['machines'])
    else:
        if confirm("Did you mean 'server:{0}'?".format(cluster_name)):
            env.hosts = [cluster_name]
        else:
            sys.exit(-1)


def with_virtualenv(path_elem=''):
    def _decorator(func):
        @wraps(func)
        def _wrapper(*args, **kwargs):
            if env.virtualenv:
                virtualenv_path = os.path.join(env.host_properties.virtualenv, path_elem, '')
            else:
                virtualenv_path = ''

            return func(virtualenv_path, *args, **kwargs)
        return _wrapper
    return _decorator


def print_node_properties(hostname):
    properties = env._host_property_tree.get(hostname, {})
    properties['hostname'] = hostname

    for key in ALL_PROPERTIES:
        # print property from clusters.yaml, or environment default otherwise
        default = getattr(env, key, None)
        value = properties.get(key, default)
        if isinstance(value, (list, set, tuple)):
            if isinstance(value, set):
                value = sorted(value)
            value = ', '.join(value)
        print " * {0}: {1}".format(cyan(key, bold=True), yellow(value))


# Sub-tasks

def _wheel():
    local('rm -rf dist build wheelhouse *.egg-info')
    local('{0} setup.py bdist_wheel'.format(env.PYTHON_EXEC))
    return local("find dist -name '*.whl' | head -1", capture=True)


def _build_plugins():
    plugin_files = []

    for plugin in env.plugins:
        print cyan(" * Plugin {0}".format(plugin))
        plugin_dir = os.path.join(env.plugins_dir, plugin)
        _build_plugin_docs(plugin_dir)
        with lcd(plugin_dir):
            plugin_files.append(('indico_' + plugin, os.path.join(plugin_dir, _wheel())))

    for plugin in env.cern_plugins:
        print cyan(" * CERN plugin {0}".format(plugin))
        plugin_dir = os.path.join(env.cern_plugins_dir, plugin)
        _build_plugin_docs(plugin_dir)
        with lcd(plugin_dir):
            plugin_files.append(('indico_' + plugin, os.path.join(plugin_dir, _wheel())))

    return plugin_files


def _build_plugin_docs(plugin_dir):
    docs_dir = os.path.join(plugin_dir, 'docs')
    if os.path.isdir(docs_dir):
        print green("   .. Generating documentation:")
        local('make -C {} clean install'.format(docs_dir))


def _checkout_sources():
    with lcd(env.code_dir):
        local('git fetch {0}'.format(env.remote))
        local('git checkout {remote}/{branch}'.format(**env.host_properties))


def _checkout_plugins():
    with lcd(env.plugins_dir):
        local('git fetch {0}'.format(env.remote))
        local('git checkout {remote}/{plugin_branch}'.format(**env.host_properties))
    with lcd(env.cern_plugins_dir):
        local('git fetch {0}'.format(env.remote))
        local('git checkout {remote}/{cern_plugin_branch}'.format(**env.host_properties))


def _build_sources():
    with lcd(env.code_dir):
        local('rm -rf build')
        local('fab package_release:no_clean=True,py_versions={0},build_here=t'.format(env.py_version))
        egg_name = local("find dist -name '*.egg' | head -1", capture=True)

    return [('indico', os.path.join(env.code_dir, egg_name))]


@with_virtualenv('lib')
def _fix_permissions(virtualenv_lib):
    sudo('chmod 644 {0}/python2.7/site-packages/zc.queue-*/EGG-INFO/*'.format(virtualenv_lib))


@with_virtualenv('bin')
def _install(virtualenv_bin, files, no_deps=False):
    sudo('mkdir -p {0}'.format(env.remote_tmp_dir))
    sudo('chmod 777 {0}'.format(env.remote_tmp_dir))

    for package, fpath in files:
        remote_fname = os.path.join(env.remote_tmp_dir, os.path.basename(fpath))
        sudo("rm -f '{0}'".format(remote_fname))
        put(fpath, env.remote_tmp_dir)
        if fpath.endswith('.whl'):
            with settings(warn_only=True):
                sudo("{0}pip uninstall -y '{1}'".format(virtualenv_bin, package))
            sudo("{0}pip install {1} '{2}'".format(virtualenv_bin,
                                                   "--no-deps" if no_deps else "",
                                                   remote_fname))
        else:
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
@runs_once
def cluster(name):
    load_cluster(name)


@task
@runs_once
def server(name):
    env.hosts = [name]


@task
@runs_once
def remote(name):
    env.remote = name


@task
@runs_once
def branch(name):
    env.branch = name


@task
@runs_once
def plugin_branch(name):
    env.plugin_branch = name


@task
@runs_once
def cern_plugin_branch(name):
    env.cern_plugin_branch = name


@task
@runs_once
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
@with_virtualenv('bin')
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
    _fix_permissions()
    configure()
    touch_files()
    restart_apache()


# Main tasks

@task
@with_virtualenv('bin')
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
@runs_once
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
    _checkout_plugins()
    files += _build_sources()
    files += _build_plugins()

    print green("File list:")
    print yellow('\n'.join("  * {0} [{1}]".format(pkg, fpath) for pkg, fpath in files))

    execute(install_node, files, no_deps=no_deps)

    _cleanup(files)
