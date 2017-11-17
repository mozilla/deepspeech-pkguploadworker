#!/usr/bin/env python3
import aiohttp
import asyncio
import json
import logging
import os
import sys
import traceback

import scriptworker.client
from scriptworker.artifacts import get_upstream_artifacts_full_paths_per_task_id
from scriptworker.context import Context
from scriptworker.exceptions import ScriptWorkerTaskException

from twine.commands.upload import main as twine_upload


log = logging.getLogger(__name__)


async def async_main(context):
    context.task = scriptworker.client.get_task(context.config)
    log.info('Hello world!')
    log.info('Hello world!', context.task)

###    log.info('Validating task')
###    validate_task_schema(context)
###
###    log.info('Getting upstream artifacts...')
###    artifacts_per_task_id = get_upstream_artifacts_full_paths_per_task_id(context)
###    all_artifacts = [
###        artifact
###        for artifacts_list in artifacts_per_task_id.values()
###        for artifact in artifacts_list
###    ]
###    wheels_only = list(filter(lambda x: x.contains(".whl"), all_artifacts))
###
###    log.info('Pushing wheels to PyPI...')
###    twine_upload(wheels_only)


def get_default_config():
    cwd = os.getcwd()
    parent_dir = os.path.dirname(cwd)

    return {
        'work_dir': os.path.join(parent_dir, 'work_dir'),
        'schema_file': os.path.join(cwd, 'pkgupload_task_schema.json'),
        'verbose': False,
    }


def load_json(path):
    with open(path, "r") as fh:
        return json.load(fh)


def validate_task_schema(context):
    task_schema = load_json(context.config['schema_file'])
    log.debug(task_schema)
    scriptworker.client.validate_json_schema(context.task, task_schema)


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
