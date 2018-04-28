#!/usr/bin/env python3
import aiohttp
import asyncio
import json
import logging
import os
import subprocess
import sys
import traceback

import scriptworker.client
from scriptworker.artifacts import get_upstream_artifacts_full_paths_per_task_id
from scriptworker.context import Context
from scriptworker.exceptions import ScriptWorkerTaskException
from scriptworker.utils import download_file, retry_async, raise_future_exceptions

from taskcluster.queue import Queue

from twine.commands.upload import main as twine_upload


log = logging.getLogger(__name__)


def download_artifacts(context, file_urls, parent_dir=None, session=None,
                             download_func=download_file):
    parent_dir = parent_dir or context.config['work_dir']
    session = session or context.session

    tasks = []
    files = []
    for file_url in file_urls:
        rel_path = file_url.rsplit('/', 1)[-1]
        abs_file_path = os.path.join(parent_dir, rel_path)
        files.append(abs_file_path)
        tasks.append(
            asyncio.ensure_future(
                retry_async(
                    download_func, args=(context, file_url, abs_file_path),
                    kwargs={'session': session},
                )
            )
        )

    return tasks, files


def get_artifact_url(task_id, artifact_name):
    PATTERN = 'https://queue.taskcluster.net/v1/task/{task_id}/artifacts/{artifact_name}'
    return PATTERN.format(task_id=task_id, artifact_name=artifact_name)


async def async_main(context):
    context.task = scriptworker.client.get_task(context.config)

    pythonArtifactTaskIds = context.task['payload']['artifacts_deps']['python']
    jsArtifactTaskIds = context.task['payload']['artifacts_deps']['javascript']
    queue = Queue()
    downloadTasks = []
    allPackages = []
    for taskId in pythonArtifactTaskIds:
        artifacts = queue.listLatestArtifacts(taskId)
        if 'artifacts' in artifacts:
            artifacts = [a['name'] for a in artifacts['artifacts']]
            log.debug('all artifacts: {}'.format(artifacts))
            artifacts = filter(lambda x: x.endswith('.whl'), artifacts)
            log.debug('filtered artifacts: {}'.format(artifacts))
            urls = [get_artifact_url(taskId, a) for a in artifacts]
            log.debug('urls: {}'.format(urls))
            tasks, files = download_artifacts(context, urls)
            log.debug('files: {}'.format(files))
            downloadTasks.extend(tasks)
            allPackages.extend(files)

    for taskId in jsArtifactTaskIds:
        artifacts = queue.listLatestArtifacts(taskId)
        if 'artifacts' in artifacts:
            artifacts = [a['name'] for a in artifacts['artifacts']]
            log.debug('all artifacts: {}'.format(artifacts))
            artifacts = filter(lambda x: x.endswith('.tgz'), artifacts)
            log.debug('filtered artifacts: {}'.format(artifacts))
            urls = [get_artifact_url(taskId, a) for a in artifacts]
            log.debug('urls: {}'.format(urls))
            tasks, files = download_artifacts(context, urls)
            log.debug('files: {}'.format(files))
            downloadTasks.extend(tasks)
            allPackages.extend(files)

    # Wait on downloads
    await raise_future_exceptions(downloadTasks)

    with open(os.path.expanduser('~/.pypirc'), 'w') as rc:
        rc.write('''
[distutils]
index-servers =
  pypi
  pypitest

[pypi]
username={pypi_username}
password={pypi_password}

[pypitest]
repository=https://test.pypi.org/legacy/
username={pypitest_username}
password={pypitest_password}'''.format(
            pypi_username=os.environ['PYPI_USERNAME'],
            pypi_password=os.environ['PYPI_PASSWORD'],
            pypitest_username=os.environ['PYPITEST_USERNAME'],
            pypitest_password=os.environ['PYPITEST_PASSWORD'],
        ))

    allWheels = list(filter(lambda x: '.whl' in x, allPackages))
    allNpmPackages = list(filter(lambda x: '.tgz' in x, allPackages))

    log.debug('allWheels: {}'.format(allWheels))
    log.debug('allNpmPackages: {}'.format(allNpmPackages))

    assert len(allNpmPackages) == 2, "should only have one CPU and one GPU package"

    if 'USE_TEST_PYPI' in os.environ and os.environ['USE_TEST_PYPI'] == '1':
        allWheels.extend(['-r', 'pypitest'])

    twine_upload(allWheels, skip_existing=True)

    subprocess.check_call(['npm-cli-login'])

    for package in allNpmPackages:
        subprocess.check_call(['npm', 'publish', package])


def get_default_config():
    cwd = os.getcwd()
    parent_dir = os.path.dirname(cwd)

    return {
        'work_dir': os.path.join(parent_dir, 'work_dir'),
        'verbose': False,
    }


def load_json(path):
    with open(path, "r") as fh:
        return json.load(fh)


def usage():
    print("Usage: {} CONFIG_FILE".format(sys.argv[0]), file=sys.stderr)
    sys.exit(1)


def main(name=None, config_path=None, close_loop=True):
    if name not in (None, '__main__'):
        return
    context = Context()
    context.config = get_default_config()
    if config_path is None:
        if len(sys.argv) != 2:
            usage()
        config_path = sys.argv[1]
    context.config.update(load_json(path=config_path))

    logging.basicConfig(**craft_logging_config(context))
    logging.getLogger('taskcluster').setLevel(logging.WARNING)
    logging.getLogger('oauth2client').setLevel(logging.WARNING)

    loop = asyncio.get_event_loop()
    with aiohttp.ClientSession() as session:
        context.session = session
        try:
            loop.run_until_complete(async_main(context))
        except ScriptWorkerTaskException as exc:
            traceback.print_exc()
            sys.exit(exc.exit_code)

    if close_loop:
        # Loop cannot be reopen once closed. Not closing it allows to run several tests on main()
        loop.close()


def craft_logging_config(context):
    return {
        'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        'level': logging.DEBUG if context.config.get('verbose') else logging.INFO
    }


main(name=__name__)
