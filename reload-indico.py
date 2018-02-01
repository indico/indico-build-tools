#!/usr/bin/env python

import glob
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime

import click
import requests


def _get_celery_units():
    return map(os.path.basename, glob.glob('/etc/systemd/system/indico-celery*.service'))


def _get_uwsgi_file():
    path = '/opt/indico/web/indico.wsgi'
    return path if os.path.exists(path) else None


@click.command()
@click.option('--uwsgi', 'reload_uwsgi', is_flag=True, help='Reload uWSGI and sent HTTP requests')
@click.option('--celery', 'restart_celery', is_flag=True, help='Restart Celery')
def main(reload_uwsgi, restart_celery):
    uwsgi_file = _get_uwsgi_file()
    celery_units = _get_celery_units()
    if reload_uwsgi and not uwsgi_file:
        click.secho('No uWSGI config found', fg='red')
        sys.exit(1)
    if restart_celery and not celery_units:
        click.secho('No celery systemd units found', fg='red')
        sys.exit(1)
    if not reload_uwsgi and not restart_celery:
        click.secho('Nothing to do', fg='red')
        sys.exit(1)

    if restart_celery:
        for unit in celery_units:
            click.echo('Restarting {}'.format(unit))
            subprocess.call(['sudo', 'systemctl', 'restart', unit])

    if reload_uwsgi:
        click.echo('Touching {}'.format(uwsgi_file))
        os.utime(uwsgi_file, None)
        time.sleep(2)
        url = 'https://{}/contact'.format(socket.getfqdn())
        click.echo('Waiting for HTTP response on {}'.format(url))
        now = datetime.now()
        num_ok = 0
        while num_ok < 2:
            try:
                resp = requests.get(url, timeout=5)
            except requests.Timeout:
                status = 'Timeout'
                status_ok = False
                num_ok = 0
            except requests.RequestException as exc:
                status = str(exc)
                status_ok = False
                num_ok = 0
            else:
                status = '{} {}'.format(resp.status_code, resp.reason)
                status_ok = resp.status_code == 200
                num_ok += 1
            delay = int((datetime.now() - now).total_seconds())
            delay_pretty = '{:02}:{:02}'.format(delay // 60, delay % 60)
            if status_ok:
                click.secho('[{}] {}'.format(delay_pretty, status), fg='green')
                match = re.search(r'Duration \(req\):\s+([0-9.]+s)', resp.text)
                if match:
                    click.echo('Indico page rendered in {}'.format(match.group(1)))
            else:
                click.secho('[{}] {}'.format(delay_pretty, status), fg='yellow')

    click.echo('Done')


if __name__ == '__main__':
    main()
