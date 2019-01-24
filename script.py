#!/usr/bin/env python3
import aiohttp
import asyncio
import json
import logging
import os
import subprocess
import sys
import traceback

import semver
import tarfile
import json
import requests

import scriptworker.client
from scriptworker.artifacts import get_upstream_artifacts_full_paths_per_task_id
from scriptworker.context import Context
from scriptworker.exceptions import ScriptWorkerTaskException
from scriptworker.utils import download_file, retry_async, raise_future_exceptions
from scriptworker.client import sync_main

from taskcluster.queue import Queue

from twine.commands.upload import main as twine_upload

from github import Github, GithubException


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

def get_native_client_final_name(task_id):
    name_mapping = {
        'DeepSpeech OSX AMD64 CPU': 'native_client.amd64.cpu.osx.tar.xz',
        'DeepSpeech Linux AMD64 CPU': 'native_client.amd64.cpu.linux.tar.xz',
        'DeepSpeech Linux AMD64 CUDA': 'native_client.amd64.cuda.linux.tar.xz',
        'DeepSpeech Linux RPi3/ARMv7 CPU': 'native_client.rpi3.cpu.linux.tar.xz',
        'DeepSpeech Linux RPi3/ARMv6 CPU': 'native_client.rpi3.cpu.linux.tar.xz',
        'DeepSpeech Linux ARM64 Cortex-A53 CPU': 'native_client.arm64.cpu.linux.tar.xz',
    }

    task_definition = 'https://queue.taskcluster.net/v1/task/{task_id}'.format(task_id=task_id)
    task_json = json.loads(requests.get(task_definition).text)

    if 'nc_asset_name' in task_json['extra']:
        return task_json['extra']['nc_asset_name']
    else:
        return name_mapping[task_json['metadata']['name']]

def parse_semver(tag=None):
    if tag.startswith('v'):
        tag = tag[1:]

    return semver.parse_version_info(tag)

def get_github_release(repo=None, tag=None, token=None):
    # Should make "https://github.com/mozilla/DeepSpeech.git" into mozilla/DeepSpeech
    log.debug('get_github_release(repo={}, tag={})'.format(repo, tag))
    repo_name = '/'.join(repo.split('/')[3:5]).replace('.git', '')
    gh = Github(token)
    log.debug('get_github_release(repo={}, tag={}): has token'.format(repo, tag))
    ds = gh.get_repo(repo_name)
    log.debug('get_github_release(repo={}, tag={}): has repo'.format(repo, tag))

    try:
        r = ds.get_release(id=tag)
        # Existing (maybe draft?)
        log.debug('get_github_release(repo={}, tag={}) existing tag'.format(repo, tag))
    except GithubException as e:
        # Inexistent, assume non-draft prerelease
        parsed = parse_semver(tag)
        log.debug('get_github_release(repo={}, tag={}) create tag'.format(repo, tag))
        r = ds.create_git_release(tag=tag, name=tag, message='', draft=False, prerelease=(parsed.prerelease is not None))

    log.debug('get_github_release(repo={}, tag={}) finish'.format(repo, tag))
    return r

def get_github_readme(repo=None, tag=None):
    repo_url = os.path.splitext(repo)[0]
    readme_url = '{}/raw/{}/README.md'.format(repo_url, tag)
    return requests.get(readme_url).text

async def async_main(context):
    context.task = scriptworker.client.get_task(context.config)

    decision_task_id = context.task['taskGroupId']
    decision_task = 'https://queue.taskcluster.net/v1/task/{task_id}'.format(task_id=decision_task_id)
    decision_json = json.loads(requests.get(decision_task).text)

    github_repo  = os.environ.get('GITHUB_HEAD_REPO_URL', decision_json['payload']['env']['GITHUB_HEAD_REPO_URL'])
    github_tag   = os.environ.get('GITHUB_HEAD_TAG', decision_json['payload']['env']['GITHUB_HEAD_TAG'])
    github_token = os.environ.get('GITHUB_ACCESS_TOKEN', '')

    assert len(github_repo) > 0
    assert len(github_tag) > 0
    assert len(github_token) > 0

    log.debug('Will upload to Github; {} release {}'.format(github_repo, github_tag))

    def download_pkgs(tasksId=None, pkg_ext=None):
        for taskId in tasksId:
            task_subdir = os.path.join(context.config['work_dir'], taskId)
            artifacts = queue.listLatestArtifacts(taskId)
            if 'artifacts' in artifacts:
                artifacts = [a['name'] for a in artifacts['artifacts']]
                log.debug('all artifacts: {}'.format(artifacts))
                artifacts = filter(lambda x: x.endswith(pkg_ext), artifacts)
                log.debug('filtered artifacts: {}'.format(artifacts))
                urls = [get_artifact_url(taskId, a) for a in artifacts]
                log.debug('urls: {}'.format(urls))
                tasks, files = download_artifacts(context, urls, parent_dir=task_subdir)
                log.debug('files: {}'.format(files))
                downloadTasks.extend(tasks)
                allPackages.extend(files)

    queue = Queue()
    downloadTasks = []
    allPackages = []

    upload_targets = []

    if 'upload_targets' in context.task['payload']:
        upload_targets = context.task['payload']['upload_targets']

    if 'python' in context.task['payload']['artifacts_deps']:
        pythonArtifactTaskIds = context.task['payload']['artifacts_deps']['python']
        download_pkgs(tasksId=pythonArtifactTaskIds, pkg_ext='.whl')

    if 'javascript' in context.task['payload']['artifacts_deps']:
        jsArtifactTaskIds     = context.task['payload']['artifacts_deps']['javascript']
        download_pkgs(tasksId=jsArtifactTaskIds, pkg_ext='.tgz')

    if 'java_aar' in context.task['payload']['artifacts_deps']:
        aarArtifactTaskIds     = context.task['payload']['artifacts_deps']['java_aar']
        download_pkgs(tasksId=aarArtifactTaskIds, pkg_ext='.maven.zip')

    if 'cpp' in context.task['payload']['artifacts_deps']:
        cppArtifactTaskIds    = context.task['payload']['artifacts_deps']['cpp']
        download_pkgs(tasksId=cppArtifactTaskIds, pkg_ext='native_client.tar.xz')

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
            pypi_username=os.environ.get('PYPI_USERNAME'),
            pypi_password=os.environ.get('PYPI_PASSWORD'),
            pypitest_username=os.environ.get('PYPITEST_USERNAME'),
            pypitest_password=os.environ.get('PYPITEST_PASSWORD'),
        ))

    allWheels = list(filter(lambda x: '.whl' in x, allPackages))
    allWheels.extend(['--skip-existing'])

    allNpmPackages = list(filter(lambda x: '.tgz' in x, allPackages))

    allAarPackages = list(filter(lambda x: '.maven.zip' in x, allPackages))

    log.debug('allWheels: {}'.format(allWheels))
    log.debug('allNpmPackages: {}'.format(allNpmPackages))
    log.debug('allAarPackages: {}'.format(allAarPackages))

    allCppPackages = []
    for cpp in filter(lambda x: 'native_client.tar.xz' in x, allPackages):
        task_id = os.path.split(os.path.split(cpp)[0])[1]
        new_nc = get_native_client_final_name(task_id)
        new_cpp = os.path.join(os.path.split(cpp)[0], new_nc)
        log.debug('Moving {} to {}...'.format(cpp, new_cpp))
        assert len(new_cpp) > 0
        os.rename(cpp, new_cpp)
        allCppPackages.extend([ new_cpp ])

    log.debug('allCppPackages: {}'.format(allCppPackages))

    if 'USE_TEST_PYPI' in os.environ and os.environ['USE_TEST_PYPI'] == '1':
        allWheels.extend(['-r', 'pypitest'])

    if 'github' in upload_targets:
        log.debug('Starting GitHub upload ...')
        gh_release = get_github_release(repo=github_repo, tag=github_tag, token=github_token)
        log.debug('GitHub release collected ...')
        all_assets_name = list(map(lambda x: x.name, gh_release.get_assets()))
        log.debug('All GitHub assets {} for {}.'.format(all_assets_name, github_tag))
        for pkg in allCppPackages + allWheels + allNpmPackages + allAarPackages:
            log.debug('Maybe uploading to GitHub {}.'.format(pkg))
            # Ensure path exists, since we can have CLI flags for Twine
            if os.path.basename(pkg) in all_assets_name:
                log.debug('Skipping Github upload for existing asset {} on release {}.'.format(pkg, github_tag))
            else:
                log.debug('Should be uploading to GitHub {}.'.format(pkg))
                if os.path.isfile(pkg):
                    log.debug('Performing Github upload for new asset {} on release {}.'.format(pkg, github_tag))
                    gh_release.upload_asset(path=pkg)

    if 'pypi' in upload_targets:
        try:
            twine_upload(allWheels)
        except Exception as e:
            log.debug('Twine Upload Exception: {}'.format(e))

    if 'npm' in upload_targets:
        assert len(allNpmPackages) == 2, "should only have one CPU and one GPU package"

        subprocess.check_call(['npm-cli-login'])
        for package in allNpmPackages:
            parsed  = parse_semver(github_tag)
            tag     = 'latest' if parsed.prerelease is None else 'prerelease'
            rc = subprocess.call(['npm', 'publish', '--verbose', package, '--tag', tag])
            if rc > 0:
                log.debug('NPM Upload Exception: {}'.format(rc))

    if 'jcenter' in upload_targets:
        bintray_username = os.environ.get('BINTRAY_USERNAME')
        bintray_apikey   = os.environ.get('BINTRAY_APIKEY')
        bintray_repo     = os.environ.get('BINTRAY_REPO')
        bintray_pkg      = os.environ.get('BINTRAY_PKG')

        bintray_version  = github_tag.replace('v', '')

        readme_tag       = get_github_readme(repo=github_repo, tag=github_tag)

        for mavenZip in allAarPackages:
            zipFile = os.path.basename(mavenZip)
            log.debug('Pushing {} to Bintray/JCenter as {}'.format(mavenZip, bintray_username))
            #curl -T libdeepspeech/build/libdeepspeech-0.4.2-alpha.0.maven.zip -uX:Y 'https://api.bintray.com/content/alissy/org.mozilla.deepspeech/libdeepspeech/0.4.2-alpha.0/libdeepspeech-0.4.2-alpha.0.maven.zip;publish=1;override=1;explode=1
            with open(mavenZip, 'rb') as put_data:
                r = requests.put('https://api.bintray.com/content/{}/{}/{}/{}/{}'.format(bintray_username, bintray_repo, bintray_pkg, bintray_version, zipFile), auth = (bintray_username, bintray_apikey), params = { 'publish': 1, 'override': 1, 'explode': 1 }, files = { 'file': put_data })
                log.debug('Pushing {} resulted in {}: {}'.format(mavenZip, r.status_code, r.text))
                assert r.status_code == 200

        r = requests.post('https://api.bintray.com/packages/{}/{}/{}/versions/{}/release_notes'.format(bintray_username, bintray_repo, bintray_pkg, bintray_version), auth = (bintray_username, bintray_apikey), data = {'bintray': { 'syntax': 'markdown', 'content': readme_tag }})
        assert r.status_code == 200


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


def main():
    # Ensure we won't leak credentials details
    logging.getLogger('oauth2client').setLevel(logging.WARNING)
    return scriptworker.client.sync_main(async_main, default_config=get_default_config(), should_validate_task=False)

def craft_logging_config(context):
    return {
        'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        'level': logging.DEBUG if context.config.get('verbose') else logging.INFO
    }

main()
