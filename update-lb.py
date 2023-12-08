#!/usr/bin/env python

import csv
import os
import re
import sys
from collections import defaultdict, OrderedDict
from operator import itemgetter

import click
import requests
import yaml
from colorclass import Color
from termcolor import colored
from terminaltables import AsciiTable


STATES = ['ready', 'drain', 'maint']
with open(os.path.join(os.path.dirname(__file__), 'servers.yaml')) as f:
    config = yaml.safe_load(f)
    CLUSTERS = config['haproxy-clusters']
    DOMAIN = config['domain']


def _cformat_sub(m):
    bg = u'on_{}'.format(m.group('bg')) if m.group('bg') else None
    attrs = ['bold'] if m.group('fg_bold') else None
    return colored(u'', m.group('fg'), bg, attrs=attrs)[:-4]


def cformat(string):
    """Replaces %{color} and %{color,bgcolor} with ansi colors.

    Bold foreground can be achieved by suffixing the color with a '!'
    """
    reset = colored(u'')
    string = string.replace(u'%{reset}', reset)
    string = re.sub(r'%\{(?P<fg>[a-z]+)(?P<fg_bold>!?)(?:,(?P<bg>[a-z]+))?}', _cformat_sub, string)
    if not string.endswith(reset):
        string += reset
    return Color(string)


def _get_stats(lb, cluster_config):
    auth = tuple(cluster_config['credentials'])
    backend_name = cluster_config['backend']
    rv = requests.get('https://{}{}/haproxy-stats;csv'.format(lb, DOMAIN), auth=auth).text
    rv = re.sub('^# ', '', rv)
    reader = csv.DictReader(rv.splitlines())
    # a server, in the correct backend, not a backup
    return [{'svname': x['svname'],
             'status': x['status'],
             'check_status': x['check_status'],
             'iid': x['iid']}
            for x in sorted(reader, key=itemgetter('svname'))
            if x['type'] == '2' and x['pxname'] == backend_name and x['bck'] == '0']


def _dump_stats(lbs, cluster_config, title):
    def _format_cell(stats):
        if stats['status'] == 'UP':
            status = click.style(stats['status'], 'green', bold=True)
        elif stats['status'].startswith('UP'):
            status = click.style(stats['status'], 'green')
        elif stats['status'] == 'DRAIN':
            status = click.style(stats['status'], 'yellow')
        elif stats['status'] == 'MAINT':
            status = click.style(stats['status'], 'yellow', bold=True)
        elif stats['status'] == 'DOWN':
            status = click.style(stats['status'], 'red', bold=True)
        else:
            status = click.style(stats['status'], 'red')
        if not stats['check_status']:
            return status
        return '{} ({})'.format(status, stats['check_status'])

    table_data = [['Server'] + lbs]
    server_stats_lb = defaultdict(OrderedDict)
    iids = {}
    for lb in lbs:
        for entry in _get_stats(lb, cluster_config):
            assert iids.setdefault(lb, entry['iid']) == entry['iid']
            server_stats_lb[entry['svname']][lb] = entry
    for svname, data in sorted(server_stats_lb.items()):
        table_data.append([svname] + [_format_cell(x) for x in data.values()])
    click.echo(AsciiTable(table_data, click.style(title, fg='white', bold=True)).table)
    return dict(server_stats_lb), iids


def _resolve_servers(available, requested):
    if not requested:
        return sorted(available), True
    rv = set()
    for name in requested:
        if name in available:
            rv.add(name)
        else:
            candidates = [x for x in available if name in x]
            if len(candidates) == 1:
                rv.add(candidates[0])
            elif not candidates:
                click.secho('Invalid server name: ' + name, fg='red')
                sys.exit(1)
            else:
                click.secho('Ambiguous server name: ' + name, fg='red')
                sys.exit(1)
    return sorted(rv), False


def _update_state(lbs, servers, state, iids, cluster_config):
    state_color = {'ready': 'green!', 'drain': 'yellow', 'maint': 'yellow!'}[state]
    for lb in lbs:
        for server in servers:
            click.echo(cformat('%%{cyan!}{}%%{reset}%%{cyan}: setting %%{cyan!}{}%%{reset}%%{cyan} to '
                               '%%{%s}{}%%{reset}%%{cyan}' % state_color)
                       .format(lb, server, state.upper()))
        payload = {'s': servers, 'b': '#' + iids[lb], 'action': state}
        requests.post('https://{}{}/haproxy-stats'.format(lb, DOMAIN), auth=tuple(cluster_config['credentials']),
                      data=payload)


@click.command()
@click.argument('cluster', type=click.Choice(CLUSTERS))
@click.argument('servers', nargs=-1)
@click.option('--ready', 'set_state', is_flag=True, flag_value='ready', help='Set state to READY')
@click.option('--drain', 'set_state', is_flag=True, flag_value='drain', help='Set state to DRAIN')
@click.option('--maint', 'set_state', is_flag=True, flag_value='maint', help='Set state to MAINT')
def main(cluster, servers, set_state):
    if servers and not set_state:
        click.secho('No state update requested; ignoring server list', fg='yellow')
        click.echo()
    lbs = sorted(CLUSTERS[cluster]['servers'])
    available_servers, iids = _dump_stats(lbs, CLUSTERS[cluster], 'Current status')
    if not set_state:
        sys.exit(0)
    click.echo()
    servers, all_servers = _resolve_servers(available_servers, servers)
    if set_state != 'ready' and all_servers:
        click.confirm(click.style('Really set ALL servers to {}?', fg='yellow', bold=True).format(set_state),
                      abort=True)
    _update_state(lbs, servers, set_state, iids, CLUSTERS[cluster])

    click.echo()
    _dump_stats(lbs, CLUSTERS[cluster], 'New status')


if __name__ == '__main__':
    main()
