#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Runs integration tests in the tests directory."""

import contextlib
import logging
import os
import secrets
import time
import copy
import glob
import itertools
import logging
import os
import unittest
import uuid
from datetime import datetime
from datetime import timezone

import mock
import yaml

import pytest


import apache_beam as beam
from apache_beam.io import filesystems
from apache_beam.io.gcp.bigquery_tools import BigQueryWrapper
from apache_beam.io.gcp.internal.clients import bigquery
from  apache_beam.io.gcp import bigtableio

from apache_beam.io.gcp.spanner_wrapper import SpannerWrapper
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.utils import python_callable
from apache_beam.yaml import yaml_provider
from apache_beam.yaml import yaml_transform

from apache_beam.testing.test_pipeline import TestPipeline
from apache_beam.testing.util import assert_that
from apache_beam.testing.util import equal_to

from google.cloud.bigtable import client

_LOGGER = logging.getLogger(__name__)

# Protect against environments where bigtable library is not available.
try:
    from apitools.base.py.exceptions import HttpError
    from google.cloud.bigtable import client
    from google.cloud.bigtable.row_filters import TimestampRange
    from google.cloud.bigtable.row import DirectRow, PartialRowData, Cell
    from google.cloud.bigtable.table import Table
    from google.cloud.bigtable_admin_v2.types import instance
except ImportError as e:
    client = None
    HttpError = None


@contextlib.contextmanager
def gcs_temp_dir(bucket):
  gcs_tempdir = bucket + '/yaml-' + str(uuid.uuid4())
  yield gcs_tempdir
  filesystems.FileSystems.delete([gcs_tempdir])


@contextlib.contextmanager
def temp_spanner_table(project, prefix='temp_spanner_db_'):
  spanner_client = SpannerWrapper(project)
  spanner_client._create_database()
  instance = "beam-test"
  database = spanner_client._test_database
  table = 'tmp_table'
  columns = ['UserId', 'Key']
  logging.info("Created Spanner database: %s", database)
  try:
    yield [f'{project}', f'{instance}', f'{database}', f'{table}', columns]
  finally:
    logging.info("Deleting Spanner database: %s", database)
    spanner_client._delete_database()


@contextlib.contextmanager
def temp_bigquery_table(project, prefix='yaml_bq_it_'):
  bigquery_client = BigQueryWrapper()
  dataset_id = '%s_%s' % (prefix, uuid.uuid4().hex)
  bigquery_client.get_or_create_dataset(project, dataset_id)
  logging.info("Created dataset %s in project %s", dataset_id, project)
  yield f'{project}.{dataset_id}.tmp_table'
  request = bigquery.BigqueryDatasetsDeleteRequest(
      projectId=project, datasetId=dataset_id, deleteContents=True)
  logging.info("Deleting dataset %s in project %s", dataset_id, project)
  bigquery_client.client.datasets.Delete(request)

def instance_prefix(instance):
    datestr = "".join(filter(str.isdigit, str(datetime.now(timezone.utc).date())))
    instance_id = '%s-%s-%s' % (instance, datestr, secrets.token_hex(4))
    assert len(instance_id) < 34, "instance id length needs to be within [6, 33]"
    return instance_id

@contextlib.contextmanager
def temp_bigtable_table(project, prefix='yaml_bt_it_'):
    INSTANCE = "bt-read-tests"
    TABLE_ID = "test-table"
    # test_pipeline = TestPipeline(is_integration_test=True)
    # args = test_pipeline.get_full_options_as_args()
    # project = test_pipeline.get_option('project')

    instance_id = instance_prefix(INSTANCE)

    clientT = client.Client(admin=True, project=project)
    # create cluster and instance
    instance = clientT.instance(
        instance_id,
        display_name=INSTANCE,
        instance_type=instance.Instance.Type.DEVELOPMENT)
    cluster = instance.cluster("test-cluster", "us-central1-a")
    operation = instance.create(clusters=[cluster])
    operation.result(timeout=500)
    _LOGGER.info(
        "Created instance [%s] in project [%s]",
        instance.instance_id,
        project)

    # create table inside instance
    table = instance.table(TABLE_ID)
    table.create()
    _LOGGER.info("Created table [%s]", table.table_id)
    if (os.environ.get('TRANSFORM_SERVICE_PORT')):
        _transform_service_address = (
                'localhost:' + os.environ.get('TRANSFORM_SERVICE_PORT'))
    else:
        _transform_service_address = None
    bigquery_client = BigQueryWrapper()
    dataset_id = '%s_%s' % (prefix, uuid.uuid4().hex)
    bigquery_client.get_or_create_dataset(project, dataset_id)
    logging.info("Created dataset %s in project %s", dataset_id, project)
    yield f'{project}.{dataset_id}.tmp_table'
    request = bigquery.BigqueryDatasetsDeleteRequest(
        projectId=project, datasetId=dataset_id, deleteContents=True)
    logging.info("Deleting dataset %s in project %s", dataset_id, project)
    bigquery_client.client.datasets.Delete(request)



def replace_recursive(spec, vars):
  if isinstance(spec, dict):
    return {
        key: replace_recursive(value, vars)
        for (key, value) in spec.items()
    }
  elif isinstance(spec, list):
    return [replace_recursive(value, vars) for value in spec]
  elif isinstance(spec, str) and '{' in spec and '{\n' not in spec:
    try:
      return spec.format(**vars)
    except Exception as exn:
      raise ValueError(f"Error evaluating {spec}: {exn}") from exn
  else:
    return spec


def transform_types(spec):
  if spec.get('type', None) in (None, 'composite', 'chain'):
    if 'source' in spec:
      yield from transform_types(spec['source'])
    for t in spec.get('transforms', []):
      yield from transform_types(t)
    if 'sink' in spec:
      yield from transform_types(spec['sink'])
  else:
    yield spec['type']


def provider_sets(spec, require_available=False):
  """For transforms that are vended by multiple providers, yields all possible
  combinations of providers to use.
  """
  try:
    for p in spec['pipelines']:
      _ = yaml_transform.preprocess(copy.deepcopy(p['pipeline']))
  except Exception as exn:
    print(exn)
    all_transform_types = []
  else:
    all_transform_types = set.union(
        *(
            set(
                transform_types(
                    yaml_transform.preprocess(copy.deepcopy(p['pipeline']))))
            for p in spec['pipelines']))

  def filter_to_available(t, providers):
    if t == 'LogForTesting':
      # Don't fan out to all the (many) possibilities for this one...
      return [
          p for p in providers if isinstance(p, yaml_provider.InlineProvider)
      ][:1]
    elif require_available:
      for p in providers:
        if not p.available():
          raise ValueError("Provider {p} required for {t} is not available.")
      return providers
    else:
      return [p for p in providers if p.available()]

  standard_providers = yaml_provider.standard_providers()
  multiple_providers = {
      t: filter_to_available(t, standard_providers[t])
      for t in all_transform_types
      if len(filter_to_available(t, standard_providers[t])) > 1
  }
  if not multiple_providers:
    yield 'only', standard_providers
  else:
    names, provider_lists = zip(*sorted(multiple_providers.items()))
    for ix, c in enumerate(itertools.product(*provider_lists)):
      yield (
          '_'.join(
              n + '_' + type(p.underlying_provider()).__name__
              for (n, p) in zip(names, c)) + f'_{ix}',
          dict(standard_providers, **{n: [p]
                                      for (n, p) in zip(names, c)}))


def create_test_methods(spec):
  for suffix, providers in provider_sets(spec):

    def test(self, providers=providers):  # default arg to capture loop value
      vars = {}
      with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch(
                'apache_beam.yaml.yaml_provider.standard_providers',
                lambda: providers))
        for fixture in spec.get('fixtures', []):
          vars[fixture['name']] = stack.enter_context(
              python_callable.PythonCallableWithSource.
              load_from_fully_qualified_name(fixture['type'])(
                  **yaml_transform.SafeLineLoader.strip_metadata(
                      fixture.get('config', {}))))
        for pipeline_spec in spec['pipelines']:
          with beam.Pipeline(options=PipelineOptions(
              pickle_library='cloudpickle',
              **yaml_transform.SafeLineLoader.strip_metadata(pipeline_spec.get(
                  'options', {})))) as p:
            yaml_transform.expand_pipeline(
                p, replace_recursive(pipeline_spec, vars))

    yield f'test_{suffix}', test


# Add bigTable, if not big table it skips (temporarily)
def parse_test_files(filepattern):
  for path in glob.glob(filepattern):
    if "bigTable" in path:
        with open(path) as fin:
          suite_name = os.path.splitext(os.path.basename(path))[0].title().replace(
              '-', '') + 'Test'
          print(path, suite_name)
          methods = dict(
              create_test_methods(
                  yaml.load(fin, Loader=yaml_transform.SafeLineLoader)))
          globals()[suite_name] = type(suite_name, (unittest.TestCase, ), methods)


logging.getLogger().setLevel(logging.INFO)
parse_test_files(os.path.join(os.path.dirname(__file__), 'tests', '*.yaml'))

if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  unittest.main()
