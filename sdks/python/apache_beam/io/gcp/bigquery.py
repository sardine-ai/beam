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

"""BigQuery sources and sinks.

This module implements reading from and writing to BigQuery tables. It relies
on several classes exposed by the BigQuery API: TableSchema, TableFieldSchema,
TableRow, and TableCell. The default mode is to return table rows read from a
BigQuery source as dictionaries. Similarly a Write transform to a BigQuerySink
accepts PCollections of dictionaries. This is done for more convenient
programming.  If desired, the native TableRow objects can be used throughout to
represent rows (use an instance of TableRowJsonCoder as a coder argument when
creating the sources or sinks respectively).

Also, for programming convenience, instances of TableReference and TableSchema
have a string representation that can be used for the corresponding arguments:

  - TableReference can be a PROJECT:DATASET.TABLE or DATASET.TABLE string.
  - TableSchema can be a NAME:TYPE{,NAME:TYPE}* string
    (e.g. 'month:STRING,event_count:INTEGER').

The syntax supported is described here:
https://cloud.google.com/bigquery/bq-command-line-tool-quickstart

BigQuery sources can be used as main inputs or side inputs. A main input
(common case) is expected to be massive and will be split into manageable chunks
and processed in parallel. Side inputs are expected to be small and will be read
completely every time a ParDo DoFn gets executed. In the example below the
lambda function implementing the DoFn for the Map transform will get on each
call *one* row of the main table and *all* rows of the side table. The runner
may use some caching techniques to share the side inputs between calls in order
to avoid excessive reading:::

  main_table = pipeline | 'VeryBig' >> beam.io.ReadFromBigQuery(...)
  side_table = pipeline | 'NotBig' >> beam.io.ReadFromBigQuery(...)
  results = (
      main_table
      | 'ProcessData' >> beam.Map(
          lambda element, side_input: ..., AsList(side_table)))

There is no difference in how main and side inputs are read. What makes the
side_table a 'side input' is the AsList wrapper used when passing the table
as a parameter to the Map transform. AsList signals to the execution framework
that its input should be made available whole.

The main and side inputs are implemented differently. Reading a BigQuery table
as main input entails exporting the table to a set of GCS files (in AVRO or in
JSON format) and then processing those files.

Users may provide a query to read from rather than reading all of a BigQuery
table. If specified, the result obtained by executing the specified query will
be used as the data of the input transform.::

  query_results = pipeline | beam.io.gcp.bigquery.ReadFromBigQuery(
      query='SELECT year, mean_temp FROM samples.weather_stations')

When creating a BigQuery input transform, users should provide either a query
or a table. Pipeline construction will fail with a validation error if neither
or both are specified.

When reading via `ReadFromBigQuery`, bytes are returned decoded as bytes.
This is due to the fact that ReadFromBigQuery uses Avro exports by default.
When reading from BigQuery using `apache_beam.io.BigQuerySource`, bytes are
returned as base64-encoded bytes. To get base64-encoded bytes using
`ReadFromBigQuery`, you can use the flag `use_json_exports` to export
data as JSON, and receive base64-encoded bytes.

ReadAllFromBigQuery
-------------------
Beam 2.27.0 introduces a new transform called `ReadAllFromBigQuery` which
allows you to define table and query reads from BigQuery at pipeline
runtime.:::

  read_requests = p | beam.Create([
      ReadFromBigQueryRequest(query='SELECT * FROM mydataset.mytable'),
      ReadFromBigQueryRequest(table='myproject.mydataset.mytable')])
  results = read_requests | ReadAllFromBigQuery()

A good application for this transform is in streaming pipelines to
refresh a side input coming from BigQuery. This would work like so:::

  side_input = (
      p
      | 'PeriodicImpulse' >> PeriodicImpulse(
          first_timestamp, last_timestamp, interval, True)
      | 'MapToReadRequest' >> beam.Map(
          lambda x: ReadFromBigQueryRequest(table='dataset.table'))
      | beam.io.ReadAllFromBigQuery())
  main_input = (
      p
      | 'MpImpulse' >> beam.Create(sample_main_input_elements)
      |
      'MapMpToTimestamped' >> beam.Map(lambda src: TimestampedValue(src, src))
      | 'WindowMpInto' >> beam.WindowInto(
          window.FixedWindows(main_input_windowing_interval)))
  result = (
      main_input
      | 'ApplyCrossJoin' >> beam.FlatMap(
          cross_join, rights=beam.pvalue.AsIter(side_input)))

**Note**: This transform is supported on Portable and Dataflow v2 runners.

**Note**: This transform does not currently clean up temporary datasets
created for its execution. (BEAM-11359)

Writing Data to BigQuery
========================

The `WriteToBigQuery` transform is the recommended way of writing data to
BigQuery. It supports a large set of parameters to customize how you'd like to
write to BigQuery.

Table References
----------------

This transform allows you to provide static `project`, `dataset` and `table`
parameters which point to a specific BigQuery table to be created. The `table`
parameter can also be a dynamic parameter (i.e. a callable), which receives an
element to be written to BigQuery, and returns the table that that element
should be sent to.

You may also provide a tuple of PCollectionView elements to be passed as side
inputs to your callable. For example, suppose that one wishes to send
events of different types to different tables, and the table names are
computed at pipeline runtime, one may do something like the following::

    with Pipeline() as p:
      elements = (p | beam.Create([
        {'type': 'error', 'timestamp': '12:34:56', 'message': 'bad'},
        {'type': 'user_log', 'timestamp': '12:34:59', 'query': 'flu symptom'},
      ]))

      table_names = (p | beam.Create([
        ('error', 'my_project:dataset1.error_table_for_today'),
        ('user_log', 'my_project:dataset1.query_table_for_today'),
      ])

      table_names_dict = beam.pvalue.AsDict(table_names)

      elements | beam.io.gcp.bigquery.WriteToBigQuery(
        table=lambda row, table_dict: table_dict[row['type']],
        table_side_inputs=(table_names_dict,))

In the example above, the `table_dict` argument passed to the function in
`table_dict` is the side input coming from `table_names_dict`, which is passed
as part of the `table_side_inputs` argument.

Schemas
---------

This transform also allows you to provide a static or dynamic `schema`
parameter (i.e. a callable).

If providing a callable, this should take in a table reference (as returned by
the `table` parameter), and return the corresponding schema for that table.
This allows to provide different schemas for different tables::

    def compute_table_name(row):
      ...

    errors_schema = {'fields': [
      {'name': 'type', 'type': 'STRING', 'mode': 'NULLABLE'},
      {'name': 'message', 'type': 'STRING', 'mode': 'NULLABLE'}]}
    queries_schema = {'fields': [
      {'name': 'type', 'type': 'STRING', 'mode': 'NULLABLE'},
      {'name': 'query', 'type': 'STRING', 'mode': 'NULLABLE'}]}

    with Pipeline() as p:
      elements = (p | beam.Create([
        {'type': 'error', 'timestamp': '12:34:56', 'message': 'bad'},
        {'type': 'user_log', 'timestamp': '12:34:59', 'query': 'flu symptom'},
      ]))

      elements | beam.io.gcp.bigquery.WriteToBigQuery(
        table=compute_table_name,
        schema=lambda table: (errors_schema
                              if 'errors' in table
                              else queries_schema))

It may be the case that schemas are computed at pipeline runtime. In cases
like these, one can also provide a `schema_side_inputs` parameter, which is
a tuple of PCollectionViews to be passed to the schema callable (much like
the `table_side_inputs` parameter).

Additional Parameters for BigQuery Tables
-----------------------------------------

This sink is able to create tables in BigQuery if they don't already exist. It
also relies on creating temporary tables when performing file loads.

The WriteToBigQuery transform creates tables using the BigQuery API by
inserting a load job (see the API reference [1]), or by inserting a new table
(see the API reference for that [2][3]).

When creating a new BigQuery table, there are a number of extra parameters
that one may need to specify. For example, clustering, partitioning, data
encoding, etc. It is possible to provide these additional parameters by
passing a Python dictionary as `additional_bq_parameters` to the transform.
As an example, to create a table that has specific partitioning, and
clustering properties, one would do the following::

    additional_bq_parameters = {
      'timePartitioning': {'type': 'DAY'},
      'clustering': {'fields': ['country']}}
    with Pipeline() as p:
      elements = (p | beam.Create([
        {'country': 'mexico', 'timestamp': '12:34:56', 'query': 'acapulco'},
        {'country': 'canada', 'timestamp': '12:34:59', 'query': 'influenza'},
      ]))

      elements | beam.io.gcp.bigquery.WriteToBigQuery(
        table='project_name1:dataset_2.query_events_table',
        additional_bq_parameters=additional_bq_parameters)

Much like the schema case, the parameter with `additional_bq_parameters` can
also take a callable that receives a table reference.


[1] https://cloud.google.com/bigquery/docs/reference/rest/v2/jobs#\
configuration.load
[2] https://cloud.google.com/bigquery/docs/reference/rest/v2/tables/insert
[3] https://cloud.google.com/bigquery/docs/reference/rest/v2/tables#resource


*** Short introduction to BigQuery concepts ***
Tables have rows (TableRow) and each row has cells (TableCell).
A table has a schema (TableSchema), which in turn describes the schema of each
cell (TableFieldSchema). The terms field and cell are used interchangeably.

TableSchema: Describes the schema (types and order) for values in each row.
  Has one attribute, 'field', which is list of TableFieldSchema objects.

TableFieldSchema: Describes the schema (type, name) for one field.
  Has several attributes, including 'name' and 'type'. Common values for
  the type attribute are: 'STRING', 'INTEGER', 'FLOAT', 'BOOLEAN', 'NUMERIC',
  'GEOGRAPHY'.
  All possible values are described at:
  https://cloud.google.com/bigquery/docs/reference/standard-sql/data-types

TableRow: Holds all values in a table row. Has one attribute, 'f', which is a
  list of TableCell instances.

TableCell: Holds the value for one cell (or field).  Has one attribute,
  'v', which is a JsonValue instance. This class is defined in
  apitools.base.py.extra_types.py module.

As of Beam 2.7.0, the NUMERIC data type is supported. This data type supports
high-precision decimal numbers (precision of 38 digits, scale of 9 digits).
The GEOGRAPHY data type works with Well-Known Text (See
https://en.wikipedia.org/wiki/Well-known_text) format for reading and writing
to BigQuery.
BigQuery IO requires values of BYTES datatype to be encoded using base64
encoding when writing to BigQuery.
"""

# pytype: skip-file

import collections
import itertools
import json
import logging
import random
import time
import uuid
from typing import Dict
from typing import Union

import apache_beam as beam
from apache_beam import coders
from apache_beam import pvalue
from apache_beam.internal.gcp.json_value import from_json_value
from apache_beam.internal.gcp.json_value import to_json_value
from apache_beam.io.avroio import _create_avro_source as create_avro_source
from apache_beam.io.filesystems import CompressionTypes
from apache_beam.io.filesystems import FileSystems
from apache_beam.io.gcp import bigquery_tools
from apache_beam.io.gcp.bigquery_io_metadata import create_bigquery_io_metadata
from apache_beam.io.gcp.bigquery_read_internal import _BigQueryReadSplit
from apache_beam.io.gcp.bigquery_read_internal import _JsonToDictCoder
from apache_beam.io.gcp.bigquery_read_internal import _PassThroughThenCleanup
from apache_beam.io.gcp.bigquery_read_internal import bigquery_export_destination_uri
from apache_beam.io.gcp.bigquery_tools import RetryStrategy
from apache_beam.io.gcp.internal.clients import bigquery
from apache_beam.io.iobase import BoundedSource
from apache_beam.io.iobase import RangeTracker
from apache_beam.io.iobase import SDFBoundedSourceReader
from apache_beam.io.iobase import SourceBundle
from apache_beam.io.textio import _TextSource as TextSource
from apache_beam.metrics import Metrics
from apache_beam.options import value_provider as vp
from apache_beam.options.pipeline_options import DebugOptions
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.options.pipeline_options import StandardOptions
from apache_beam.options.value_provider import StaticValueProvider
from apache_beam.options.value_provider import ValueProvider
from apache_beam.options.value_provider import check_accessible
from apache_beam.runners.dataflow.native_io import iobase as dataflow_io
from apache_beam.transforms import DoFn
from apache_beam.transforms import ParDo
from apache_beam.transforms import PTransform
from apache_beam.transforms.display import DisplayDataItem
from apache_beam.transforms.sideinputs import SIDE_INPUT_PREFIX
from apache_beam.transforms.sideinputs import get_sideinput_index
from apache_beam.transforms.util import ReshufflePerKey
from apache_beam.transforms.window import GlobalWindows
from apache_beam.utils import retry
from apache_beam.utils.annotations import deprecated
from apache_beam.utils.annotations import experimental

try:
  from apache_beam.io.gcp.internal.clients.bigquery import DatasetReference
  from apache_beam.io.gcp.internal.clients.bigquery import TableReference
except ImportError:
  DatasetReference = None
  TableReference = None

__all__ = [
    'TableRowJsonCoder',
    'BigQueryDisposition',
    'BigQuerySource',
    'BigQuerySink',
    'WriteToBigQuery',
    'ReadFromBigQuery',
    'ReadFromBigQueryRequest',
    'ReadAllFromBigQuery',
    'SCHEMA_AUTODETECT',
]

_LOGGER = logging.getLogger(__name__)
"""
Template for BigQuery jobs created by BigQueryIO. This template is:
`"beam_bq_job_{job_type}_{job_id}_{step_id}_{random}"`, where:

- `job_type` represents the BigQuery job type (e.g. extract / copy / load /
    query).
- `job_id` is the Beam job name.
- `step_id` is a UUID representing the the Dataflow step that created the
    BQ job.
- `random` is a random string.

NOTE: This job name template does not have backwards compatibility guarantees.
"""
BQ_JOB_NAME_TEMPLATE = "beam_bq_job_{job_type}_{job_id}_{step_id}{random}"


@deprecated(since='2.11.0', current="bigquery_tools.parse_table_reference")
def _parse_table_reference(table, dataset=None, project=None):
  return bigquery_tools.parse_table_reference(table, dataset, project)


@deprecated(
    since='2.11.0', current="bigquery_tools.parse_table_schema_from_json")
def parse_table_schema_from_json(schema_string):
  return bigquery_tools.parse_table_schema_from_json(schema_string)


@deprecated(since='2.11.0', current="bigquery_tools.default_encoder")
def default_encoder(obj):
  return bigquery_tools.default_encoder(obj)


@deprecated(since='2.11.0', current="bigquery_tools.RowAsDictJsonCoder")
def RowAsDictJsonCoder(*args, **kwargs):
  return bigquery_tools.RowAsDictJsonCoder(*args, **kwargs)


@deprecated(since='2.11.0', current="bigquery_tools.BigQueryReader")
def BigQueryReader(*args, **kwargs):
  return bigquery_tools.BigQueryReader(*args, **kwargs)


@deprecated(since='2.11.0', current="bigquery_tools.BigQueryWriter")
def BigQueryWriter(*args, **kwargs):
  return bigquery_tools.BigQueryWriter(*args, **kwargs)


@deprecated(since='2.11.0', current="bigquery_tools.BigQueryWrapper")
def BigQueryWrapper(*args, **kwargs):
  return bigquery_tools.BigQueryWrapper(*args, **kwargs)


class TableRowJsonCoder(coders.Coder):
  """A coder for a TableRow instance to/from a JSON string.

  Note that the encoding operation (used when writing to sinks) requires the
  table schema in order to obtain the ordered list of field names. Reading from
  sources on the other hand does not need the table schema.
  """
  def __init__(self, table_schema=None):
    # The table schema is needed for encoding TableRows as JSON (writing to
    # sinks) because the ordered list of field names is used in the JSON
    # representation.
    self.table_schema = table_schema
    # Precompute field names since we need them for row encoding.
    if self.table_schema:
      self.field_names = tuple(fs.name for fs in self.table_schema.fields)
      self.field_types = tuple(fs.type for fs in self.table_schema.fields)

  def encode(self, table_row):
    if self.table_schema is None:
      raise AttributeError(
          'The TableRowJsonCoder requires a table schema for '
          'encoding operations. Please specify a table_schema argument.')
    try:
      return json.dumps(
          collections.OrderedDict(
              zip(
                  self.field_names,
                  [from_json_value(f.v) for f in table_row.f])),
          allow_nan=False,
          default=bigquery_tools.default_encoder)
    except ValueError as e:
      raise ValueError('%s. %s' % (e, bigquery_tools.JSON_COMPLIANCE_ERROR))

  def decode(self, encoded_table_row):
    od = json.loads(
        encoded_table_row, object_pairs_hook=collections.OrderedDict)
    return bigquery.TableRow(
        f=[bigquery.TableCell(v=to_json_value(e)) for e in od.values()])


class BigQueryDisposition(object):
  """Class holding standard strings used for create and write dispositions."""

  CREATE_NEVER = 'CREATE_NEVER'
  CREATE_IF_NEEDED = 'CREATE_IF_NEEDED'
  WRITE_TRUNCATE = 'WRITE_TRUNCATE'
  WRITE_APPEND = 'WRITE_APPEND'
  WRITE_EMPTY = 'WRITE_EMPTY'

  @staticmethod
  def validate_create(disposition):
    values = (
        BigQueryDisposition.CREATE_NEVER, BigQueryDisposition.CREATE_IF_NEEDED)
    if disposition not in values:
      raise ValueError(
          'Invalid create disposition %s. Expecting %s' % (disposition, values))
    return disposition

  @staticmethod
  def validate_write(disposition):
    values = (
        BigQueryDisposition.WRITE_TRUNCATE,
        BigQueryDisposition.WRITE_APPEND,
        BigQueryDisposition.WRITE_EMPTY)
    if disposition not in values:
      raise ValueError(
          'Invalid write disposition %s. Expecting %s' % (disposition, values))
    return disposition


# -----------------------------------------------------------------------------
# BigQuerySource, BigQuerySink.


@deprecated(since='2.25.0', current="ReadFromBigQuery")
def BigQuerySource(
    table=None,
    dataset=None,
    project=None,
    query=None,
    validate=False,
    coder=None,
    use_standard_sql=False,
    flatten_results=True,
    kms_key=None,
    use_dataflow_native_source=False):
  if use_dataflow_native_source:
    return _BigQuerySource(
        table,
        dataset,
        project,
        query,
        validate,
        coder,
        use_standard_sql,
        flatten_results,
        kms_key)
  else:
    return ReadFromBigQuery(
        table=table,
        dataset=dataset,
        project=project,
        query=query,
        validate=validate,
        coder=coder,
        use_standard_sql=use_standard_sql,
        flatten_results=flatten_results,
        use_json_exports=True,
        kms_key=kms_key)


@deprecated(since='2.25.0', current="ReadFromBigQuery")
class _BigQuerySource(dataflow_io.NativeSource):
  """A source based on a BigQuery table."""
  def __init__(
      self,
      table=None,
      dataset=None,
      project=None,
      query=None,
      validate=False,
      coder=None,
      use_standard_sql=False,
      flatten_results=True,
      kms_key=None,
      temp_dataset=None):
    """Initialize a :class:`BigQuerySource`.

    Args:
      table (str): The ID of a BigQuery table. If specified all data of the
        table will be used as input of the current source. The ID must contain
        only letters ``a-z``, ``A-Z``, numbers ``0-9``, or underscores
        ``_``. If dataset and query arguments are :data:`None` then the table
        argument must contain the entire table reference specified as:
        ``'DATASET.TABLE'`` or ``'PROJECT:DATASET.TABLE'``.
      dataset (str): The ID of the dataset containing this table or
        :data:`None` if the table reference is specified entirely by the table
        argument or a query is specified.
      project (str): The ID of the project containing this table or
        :data:`None` if the table reference is specified entirely by the table
        argument or a query is specified.
      query (str): A query to be used instead of arguments table, dataset, and
        project.
      validate (bool): If :data:`True`, various checks will be done when source
        gets initialized (e.g., is table present?). This should be
        :data:`True` for most scenarios in order to catch errors as early as
        possible (pipeline construction instead of pipeline execution). It
        should be :data:`False` if the table is created during pipeline
        execution by a previous step.
      coder (~apache_beam.coders.coders.Coder): The coder for the table
        rows if serialized to disk. If :data:`None`, then the default coder is
        :class:`~apache_beam.io.gcp.bigquery_tools.RowAsDictJsonCoder`,
        which will interpret every line in a file as a JSON serialized
        dictionary. This argument needs a value only in special cases when
        returning table rows as dictionaries is not desirable.
      use_standard_sql (bool): Specifies whether to use BigQuery's standard SQL
        dialect for this query. The default value is :data:`False`.
        If set to :data:`True`, the query will use BigQuery's updated SQL
        dialect with improved standards compliance.
        This parameter is ignored for table inputs.
      flatten_results (bool): Flattens all nested and repeated fields in the
        query results. The default value is :data:`True`.
      kms_key (str): Optional Cloud KMS key name for use when creating new
        tables.
      temp_dataset (``google.cloud.bigquery.dataset.DatasetReference``):
        The dataset in which to create temporary tables when performing file
        loads. By default, a new dataset is created in the execution project for
        temporary tables.

    Raises:
      ValueError: if any of the following is true:

        1) the table reference as a string does not match the expected format
        2) neither a table nor a query is specified
        3) both a table and a query is specified.
    """

    # Import here to avoid adding the dependency for local running scenarios.
    try:
      # pylint: disable=wrong-import-order, wrong-import-position
      from apitools.base import py  # pylint: disable=unused-import
    except ImportError:
      raise ImportError(
          'Google Cloud IO not available, '
          'please install apache_beam[gcp]')

    if table is not None and query is not None:
      raise ValueError(
          'Both a BigQuery table and a query were specified.'
          ' Please specify only one of these.')
    elif table is None and query is None:
      raise ValueError('A BigQuery table or a query must be specified')
    elif table is not None:
      self.table_reference = bigquery_tools.parse_table_reference(
          table, dataset, project)
      self.query = None
      self.use_legacy_sql = True
    else:
      self.query = query
      # TODO(BEAM-1082): Change the internal flag to be standard_sql
      self.use_legacy_sql = not use_standard_sql
      self.table_reference = None

    self.validate = validate
    self.flatten_results = flatten_results
    self.coder = coder or bigquery_tools.RowAsDictJsonCoder()
    self.kms_key = kms_key
    self.temp_dataset = temp_dataset

  def display_data(self):
    if self.query is not None:
      res = {'query': DisplayDataItem(self.query, label='Query')}
    else:
      if self.table_reference.projectId is not None:
        tableSpec = '{}:{}.{}'.format(
            self.table_reference.projectId,
            self.table_reference.datasetId,
            self.table_reference.tableId)
      else:
        tableSpec = '{}.{}'.format(
            self.table_reference.datasetId, self.table_reference.tableId)
      res = {'table': DisplayDataItem(tableSpec, label='Table')}

    res['validation'] = DisplayDataItem(
        self.validate, label='Validation Enabled')
    return res

  @property
  def format(self):
    """Source format name required for remote execution."""
    return 'bigquery'

  def reader(self, test_bigquery_client=None):
    return bigquery_tools.BigQueryReader(
        source=self,
        test_bigquery_client=test_bigquery_client,
        use_legacy_sql=self.use_legacy_sql,
        flatten_results=self.flatten_results,
        kms_key=self.kms_key)


class _CustomBigQuerySource(BoundedSource):
  def __init__(
      self,
      gcs_location=None,
      table=None,
      dataset=None,
      project=None,
      query=None,
      validate=False,
      pipeline_options=None,
      coder=None,
      use_standard_sql=False,
      flatten_results=True,
      kms_key=None,
      bigquery_job_labels=None,
      use_json_exports=False,
      job_name=None,
      step_name=None,
      unique_id=None,
      temp_dataset=None):
    if table is not None and query is not None:
      raise ValueError(
          'Both a BigQuery table and a query were specified.'
          ' Please specify only one of these.')
    elif table is None and query is None:
      raise ValueError('A BigQuery table or a query must be specified')
    elif table is not None:
      self.table_reference = bigquery_tools.parse_table_reference(
          table, dataset, project)
      self.query = None
      self.use_legacy_sql = True
    else:
      if isinstance(query, str):
        query = StaticValueProvider(str, query)
      self.query = query
      # TODO(BEAM-1082): Change the internal flag to be standard_sql
      self.use_legacy_sql = not use_standard_sql
      self.table_reference = None

    self.gcs_location = gcs_location
    self.project = project
    self.validate = validate
    self.flatten_results = flatten_results
    self.coder = coder or _JsonToDictCoder
    self.kms_key = kms_key
    self.split_result = None
    self.options = pipeline_options
    self.bq_io_metadata = None  # Populate in setup, as it may make an RPC
    self.bigquery_job_labels = bigquery_job_labels or {}
    self.use_json_exports = use_json_exports
    self.temp_dataset = temp_dataset
    self._job_name = job_name or 'BQ_EXPORT_JOB'
    self._step_name = step_name
    self._source_uuid = unique_id

  def _get_bq_metadata(self):
    if not self.bq_io_metadata:
      self.bq_io_metadata = create_bigquery_io_metadata(self._step_name)
    return self.bq_io_metadata

  def display_data(self):
    export_format = 'JSON' if self.use_json_exports else 'AVRO'
    return {
        'table': str(self.table_reference),
        'query': str(self.query),
        'project': str(self.project),
        'use_legacy_sql': self.use_legacy_sql,
        'bigquery_job_labels': json.dumps(self.bigquery_job_labels),
        'export_file_format': export_format,
        'launchesBigQueryJobs': DisplayDataItem(
            True, label="This Dataflow job launches bigquery jobs."),
    }

  def estimate_size(self):
    bq = bigquery_tools.BigQueryWrapper()
    if self.table_reference is not None:
      table_ref = self.table_reference
      if (isinstance(self.table_reference, vp.ValueProvider) and
          self.table_reference.is_accessible()):
        table_ref = bigquery_tools.parse_table_reference(
            table_ref, project=self._get_project())
      elif isinstance(self.table_reference, vp.ValueProvider):
        # Size estimation is best effort. We return None as we have
        # no access to the table that we're querying.
        return None
      if not table_ref.projectId:
        table_ref.projectId = self._get_project()
      table = bq.get_table(
          table_ref.projectId, table_ref.datasetId, table_ref.tableId)
      return int(table.numBytes)
    elif self.query is not None and self.query.is_accessible():
      project = self._get_project()
      query_job_name = bigquery_tools.generate_bq_job_name(
          self._job_name,
          self._source_uuid,
          bigquery_tools.BigQueryJobTypes.QUERY,
          '%s_%s' % (int(time.time()), random.randint(0, 1000)))
      job = bq._start_query_job(
          project,
          self.query.get(),
          self.use_legacy_sql,
          self.flatten_results,
          job_id=query_job_name,
          dry_run=True,
          kms_key=self.kms_key,
          job_labels=self._get_bq_metadata().add_additional_bq_job_labels(
              self.bigquery_job_labels))
      size = int(job.statistics.totalBytesProcessed)
      return size
    else:
      # Size estimation is best effort. We return None as we have
      # no access to the query that we're running.
      return None

  def _get_project(self):
    """Returns the project that queries and exports will be billed to."""

    project = self.options.view_as(GoogleCloudOptions).project
    if isinstance(project, vp.ValueProvider):
      project = project.get()
    if self.temp_dataset:
      return self.temp_dataset.projectId
    if not project:
      project = self.project
    return project

  def _create_source(self, path, schema):
    if not self.use_json_exports:
      return create_avro_source(path, use_fastavro=True)
    else:
      return TextSource(
          path,
          min_bundle_size=0,
          compression_type=CompressionTypes.UNCOMPRESSED,
          strip_trailing_newlines=True,
          coder=self.coder(schema))

  def split(self, desired_bundle_size, start_position=None, stop_position=None):
    if self.split_result is None:
      bq = bigquery_tools.BigQueryWrapper(
          temp_dataset_id=(
              self.temp_dataset.datasetId if self.temp_dataset else None))

      if self.query is not None:
        self._setup_temporary_dataset(bq)
        self.table_reference = self._execute_query(bq)

      if not self.table_reference.projectId:
        self.table_reference.projectId = self._get_project()

      schema, metadata_list = self._export_files(bq)
      self.split_result = [
          self._create_source(metadata.path, schema)
          for metadata in metadata_list
      ]

      if self.query is not None:
        bq.clean_up_temporary_dataset(self._get_project())

    for source in self.split_result:
      yield SourceBundle(1.0, source, None, None)

  def get_range_tracker(self, start_position, stop_position):
    class CustomBigQuerySourceRangeTracker(RangeTracker):
      """A RangeTracker that always returns positions as None."""
      def start_position(self):
        return None

      def stop_position(self):
        return None

    return CustomBigQuerySourceRangeTracker()

  def read(self, range_tracker):
    raise NotImplementedError('BigQuery source must be split before being read')

  @check_accessible(['query'])
  def _setup_temporary_dataset(self, bq):
    location = bq.get_query_location(
        self._get_project(), self.query.get(), self.use_legacy_sql)
    bq.create_temporary_dataset(self._get_project(), location)

  @check_accessible(['query'])
  def _execute_query(self, bq):
    query_job_name = bigquery_tools.generate_bq_job_name(
        self._job_name,
        self._source_uuid,
        bigquery_tools.BigQueryJobTypes.QUERY,
        '%s_%s' % (int(time.time()), random.randint(0, 1000)))
    job = bq._start_query_job(
        self._get_project(),
        self.query.get(),
        self.use_legacy_sql,
        self.flatten_results,
        job_id=query_job_name,
        kms_key=self.kms_key,
        job_labels=self._get_bq_metadata().add_additional_bq_job_labels(
            self.bigquery_job_labels))
    job_ref = job.jobReference
    bq.wait_for_bq_job(job_ref, max_retries=0)
    return bq._get_temp_table(self._get_project())

  def _export_files(self, bq):
    """Runs a BigQuery export job.

    Returns:
      bigquery.TableSchema instance, a list of FileMetadata instances
    """
    job_labels = self._get_bq_metadata().add_additional_bq_job_labels(
        self.bigquery_job_labels)
    export_job_name = bigquery_tools.generate_bq_job_name(
        self._job_name,
        self._source_uuid,
        bigquery_tools.BigQueryJobTypes.EXPORT,
        '%s_%s' % (int(time.time()), random.randint(0, 1000)))
    temp_location = self.options.view_as(GoogleCloudOptions).temp_location
    gcs_location = bigquery_export_destination_uri(
        self.gcs_location, temp_location, self._source_uuid)
    if self.use_json_exports:
      job_ref = bq.perform_extract_job([gcs_location],
                                       export_job_name,
                                       self.table_reference,
                                       bigquery_tools.FileFormat.JSON,
                                       project=self._get_project(),
                                       job_labels=job_labels,
                                       include_header=False)
    else:
      job_ref = bq.perform_extract_job([gcs_location],
                                       export_job_name,
                                       self.table_reference,
                                       bigquery_tools.FileFormat.AVRO,
                                       project=self._get_project(),
                                       include_header=False,
                                       job_labels=job_labels,
                                       use_avro_logical_types=True)
    bq.wait_for_bq_job(job_ref)
    metadata_list = FileSystems.match([gcs_location])[0].metadata_list

    if isinstance(self.table_reference, vp.ValueProvider):
      table_ref = bigquery_tools.parse_table_reference(
          self.table_reference.get(), project=self.project)
    else:
      table_ref = self.table_reference
    table = bq.get_table(
        table_ref.projectId, table_ref.datasetId, table_ref.tableId)

    return table.schema, metadata_list


@deprecated(since='2.11.0', current="WriteToBigQuery")
class BigQuerySink(dataflow_io.NativeSink):
  """A sink based on a BigQuery table.

  This BigQuery sink triggers a Dataflow native sink for BigQuery
  that only supports batch pipelines.
  Instead of using this sink directly, please use WriteToBigQuery
  transform that works for both batch and streaming pipelines.
  """
  def __init__(
      self,
      table,
      dataset=None,
      project=None,
      schema=None,
      create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
      write_disposition=BigQueryDisposition.WRITE_EMPTY,
      validate=False,
      coder=None,
      kms_key=None):
    """Initialize a BigQuerySink.

    Args:
      table (str): The ID of the table. The ID must contain only letters
        ``a-z``, ``A-Z``, numbers ``0-9``, or underscores ``_``. If
        **dataset** argument is :data:`None` then the table argument must
        contain the entire table reference specified as: ``'DATASET.TABLE'`` or
        ``'PROJECT:DATASET.TABLE'``.
      dataset (str): The ID of the dataset containing this table or
        :data:`None` if the table reference is specified entirely by the table
        argument.
      project (str): The ID of the project containing this table or
        :data:`None` if the table reference is specified entirely by the table
        argument.
      schema (str): The schema to be used if the BigQuery table to write has
        to be created. This can be either specified as a
        :class:`~apache_beam.io.gcp.internal.clients.bigquery.\
bigquery_v2_messages.TableSchema` object or a single string  of the form
        ``'field1:type1,field2:type2,field3:type3'`` that defines a comma
        separated list of fields. Here ``'type'`` should specify the BigQuery
        type of the field. Single string based schemas do not support nested
        fields, repeated fields, or specifying a BigQuery mode for fields (mode
        will always be set to ``'NULLABLE'``).
      create_disposition (BigQueryDisposition): A string describing what
        happens if the table does not exist. Possible values are:

          * :attr:`BigQueryDisposition.CREATE_IF_NEEDED`: create if does not
            exist.
          * :attr:`BigQueryDisposition.CREATE_NEVER`: fail the write if does not
            exist.

      write_disposition (BigQueryDisposition): A string describing what
        happens if the table has already some data. Possible values are:

          * :attr:`BigQueryDisposition.WRITE_TRUNCATE`: delete existing rows.
          * :attr:`BigQueryDisposition.WRITE_APPEND`: add to existing rows.
          * :attr:`BigQueryDisposition.WRITE_EMPTY`: fail the write if table not
            empty.

      validate (bool): If :data:`True`, various checks will be done when sink
        gets initialized (e.g., is table present given the disposition
        arguments?). This should be :data:`True` for most scenarios in order to
        catch errors as early as possible (pipeline construction instead of
        pipeline execution). It should be :data:`False` if the table is created
        during pipeline execution by a previous step.
      coder (~apache_beam.coders.coders.Coder): The coder for the
        table rows if serialized to disk. If :data:`None`, then the default
        coder is :class:`~apache_beam.io.gcp.bigquery_tools.RowAsDictJsonCoder`,
        which will interpret every element written to the sink as a dictionary
        that will be JSON serialized as a line in a file. This argument needs a
        value only in special cases when writing table rows as dictionaries is
        not desirable.
      kms_key (str): Optional Cloud KMS key name for use when creating new
        tables.

    Raises:
      TypeError: if the schema argument is not a :class:`str` or a
        :class:`~apache_beam.io.gcp.internal.clients.bigquery.\
bigquery_v2_messages.TableSchema` object.
      ValueError: if the table reference as a string does not
        match the expected format.
    """
    # Import here to avoid adding the dependency for local running scenarios.
    try:
      # pylint: disable=wrong-import-order, wrong-import-position
      from apitools.base import py  # pylint: disable=unused-import
    except ImportError:
      raise ImportError(
          'Google Cloud IO not available, '
          'please install apache_beam[gcp]')

    self.table_reference = bigquery_tools.parse_table_reference(
        table, dataset, project)
    # Transform the table schema into a bigquery.TableSchema instance.
    if isinstance(schema, str):
      # TODO(silviuc): Should add a regex-based validation of the format.
      table_schema = bigquery.TableSchema()
      schema_list = [s.strip(' ') for s in schema.split(',')]
      for field_and_type in schema_list:
        field_name, field_type = field_and_type.split(':')
        field_schema = bigquery.TableFieldSchema()
        field_schema.name = field_name
        field_schema.type = field_type
        field_schema.mode = 'NULLABLE'
        table_schema.fields.append(field_schema)
      self.table_schema = table_schema
    elif schema is None:
      # TODO(silviuc): Should check that table exists if no schema specified.
      self.table_schema = schema
    elif isinstance(schema, bigquery.TableSchema):
      self.table_schema = schema
    else:
      raise TypeError('Unexpected schema argument: %s.' % schema)

    self.create_disposition = BigQueryDisposition.validate_create(
        create_disposition)
    self.write_disposition = BigQueryDisposition.validate_write(
        write_disposition)
    self.validate = validate
    self.coder = coder or bigquery_tools.RowAsDictJsonCoder()
    self.kms_key = kms_key

  def display_data(self):
    res = {}
    if self.table_reference is not None:
      tableSpec = '{}.{}'.format(
          self.table_reference.datasetId, self.table_reference.tableId)
      if self.table_reference.projectId is not None:
        tableSpec = '{}:{}'.format(self.table_reference.projectId, tableSpec)
      res['table'] = DisplayDataItem(tableSpec, label='Table')

    res['validation'] = DisplayDataItem(
        self.validate, label="Validation Enabled")
    return res

  def schema_as_json(self):
    """Returns the TableSchema associated with the sink as a JSON string."""
    def schema_list_as_object(schema_list):
      """Returns a list of TableFieldSchema objects as a list of dicts."""
      fields = []
      for f in schema_list:
        fs = {'name': f.name, 'type': f.type}
        if f.description is not None:
          fs['description'] = f.description
        if f.mode is not None:
          fs['mode'] = f.mode
        if f.type.lower() == 'record':
          fs['fields'] = schema_list_as_object(f.fields)
        fields.append(fs)
      return fields

    return json.dumps(
        {'fields': schema_list_as_object(self.table_schema.fields)})

  @property
  def format(self):
    """Sink format name required for remote execution."""
    return 'bigquery'

  def writer(self, test_bigquery_client=None, buffer_size=None):
    return bigquery_tools.BigQueryWriter(
        sink=self,
        test_bigquery_client=test_bigquery_client,
        buffer_size=buffer_size)


_KNOWN_TABLES = set()


class BigQueryWriteFn(DoFn):
  """A ``DoFn`` that streams writes to BigQuery once the table is created."""

  DEFAULT_MAX_BUFFERED_ROWS = 2000
  DEFAULT_MAX_BATCH_SIZE = 500

  FAILED_ROWS = 'FailedRows'
  STREAMING_API_LOGGING_FREQUENCY_SEC = 300

  def __init__(
      self,
      batch_size,
      schema=None,
      create_disposition=None,
      write_disposition=None,
      kms_key=None,
      test_client=None,
      max_buffered_rows=None,
      retry_strategy=None,
      additional_bq_parameters=None,
      ignore_insert_ids=False,
      with_batched_input=False):
    """Initialize a WriteToBigQuery transform.

    Args:
      batch_size: Number of rows to be written to BQ per streaming API insert.
      schema: The schema to be used if the BigQuery table to write has to be
        created. This can be either specified as a 'bigquery.TableSchema' object
        or a single string  of the form 'field1:type1,field2:type2,field3:type3'
        that defines a comma separated list of fields. Here 'type' should
        specify the BigQuery type of the field. Single string based schemas do
        not support nested fields, repeated fields, or specifying a BigQuery
        mode for fields (mode will always be set to 'NULLABLE').
      create_disposition: A string describing what happens if the table does not
        exist. Possible values are:
        - BigQueryDisposition.CREATE_IF_NEEDED: create if does not exist.
        - BigQueryDisposition.CREATE_NEVER: fail the write if does not exist.
      write_disposition: A string describing what happens if the table has
        already some data. Possible values are:
        -  BigQueryDisposition.WRITE_TRUNCATE: delete existing rows.
        -  BigQueryDisposition.WRITE_APPEND: add to existing rows.
        -  BigQueryDisposition.WRITE_EMPTY: fail the write if table not empty.
        For streaming pipelines WriteTruncate can not be used.
      kms_key: Optional Cloud KMS key name for use when creating new tables.
      test_client: Override the default bigquery client used for testing.

      max_buffered_rows: The maximum number of rows that are allowed to stay
        buffered when running dynamic destinations. When destinations are
        dynamic, it is important to keep caches small even when a single
        batch has not been completely filled up.
      retry_strategy: The strategy to use when retrying streaming inserts
        into BigQuery. Options are shown in bigquery_tools.RetryStrategy attrs.
      additional_bq_parameters (dict, callable): A set of additional parameters
        to be passed when creating a BigQuery table. These are passed when
        triggering a load job for FILE_LOADS, and when creating a new table for
        STREAMING_INSERTS.
      ignore_insert_ids: When using the STREAMING_INSERTS method to write data
        to BigQuery, `insert_ids` are a feature of BigQuery that support
        deduplication of events. If your use case is not sensitive to
        duplication of data inserted to BigQuery, set `ignore_insert_ids`
        to True to increase the throughput for BQ writing. See:
        https://cloud.google.com/bigquery/streaming-data-into-bigquery#disabling_best_effort_de-duplication
      with_batched_input: Whether the input has already been batched per
        destination. If not, perform best-effort batching per destination within
        a bunble.
    """
    self.schema = schema
    self.test_client = test_client
    self.create_disposition = create_disposition
    self.write_disposition = write_disposition
    if write_disposition in (BigQueryDisposition.WRITE_EMPTY,
                             BigQueryDisposition.WRITE_TRUNCATE):
      raise ValueError(
          'Write disposition %s is not supported for'
          ' streaming inserts to BigQuery' % write_disposition)
    self._rows_buffer = []
    self._reset_rows_buffer()

    self._total_buffered_rows = 0
    self.kms_key = kms_key
    self._max_batch_size = batch_size or BigQueryWriteFn.DEFAULT_MAX_BATCH_SIZE
    self._max_buffered_rows = (
        max_buffered_rows or BigQueryWriteFn.DEFAULT_MAX_BUFFERED_ROWS)
    self._retry_strategy = retry_strategy or RetryStrategy.RETRY_ALWAYS
    self.ignore_insert_ids = ignore_insert_ids
    self.with_batched_input = with_batched_input

    self.additional_bq_parameters = additional_bq_parameters or {}

    # accumulate the total time spent in exponential backoff
    self._throttled_secs = Metrics.counter(
        BigQueryWriteFn, "cumulativeThrottlingSeconds")
    self.batch_size_metric = Metrics.distribution(self.__class__, "batch_size")
    self.batch_latency_metric = Metrics.distribution(
        self.__class__, "batch_latency_ms")
    self.failed_rows_metric = Metrics.distribution(
        self.__class__, "rows_failed_per_batch")
    self.bigquery_wrapper = None
    self.streaming_api_logging_frequency_sec = (
        BigQueryWriteFn.STREAMING_API_LOGGING_FREQUENCY_SEC)

  def display_data(self):
    return {
        'max_batch_size': self._max_batch_size,
        'max_buffered_rows': self._max_buffered_rows,
        'retry_strategy': self._retry_strategy,
        'create_disposition': str(self.create_disposition),
        'write_disposition': str(self.write_disposition),
        'additional_bq_parameters': str(self.additional_bq_parameters),
        'ignore_insert_ids': str(self.ignore_insert_ids)
    }

  def _reset_rows_buffer(self):
    self._rows_buffer = collections.defaultdict(lambda: [])

  @staticmethod
  def get_table_schema(schema):
    """Transform the table schema into a bigquery.TableSchema instance.

    Args:
      schema: The schema to be used if the BigQuery table to write has to be
        created. This is a dictionary object created in the WriteToBigQuery
        transform.
    Returns:
      table_schema: The schema to be used if the BigQuery table to write has
         to be created but in the bigquery.TableSchema format.
    """
    if schema is None:
      return schema
    elif isinstance(schema, str):
      return bigquery_tools.parse_table_schema_from_json(schema)
    elif isinstance(schema, dict):
      return bigquery_tools.parse_table_schema_from_json(json.dumps(schema))
    else:
      raise TypeError('Unexpected schema argument: %s.' % schema)

  def start_bundle(self):
    self._reset_rows_buffer()

    if not self.bigquery_wrapper:
      self.bigquery_wrapper = bigquery_tools.BigQueryWrapper(
          client=self.test_client)

    (
        bigquery_tools.BigQueryWrapper.HISTOGRAM_METRIC_LOGGER.
        minimum_logging_frequency_msec
    ) = self.streaming_api_logging_frequency_sec * 1000

    self._backoff_calculator = iter(
        retry.FuzzedExponentialIntervals(
            initial_delay_secs=0.2, num_retries=10000, max_delay_secs=1500))

  def _create_table_if_needed(self, table_reference, schema=None):
    str_table_reference = '%s:%s.%s' % (
        table_reference.projectId,
        table_reference.datasetId,
        table_reference.tableId)
    if str_table_reference in _KNOWN_TABLES:
      return

    if self.create_disposition == BigQueryDisposition.CREATE_NEVER:
      # If we never want to create the table, we assume it already exists,
      # and avoid the get-or-create step.
      return

    _LOGGER.debug(
        'Creating or getting table %s with schema %s.', table_reference, schema)

    table_schema = self.get_table_schema(schema)

    if table_reference.projectId is None:
      table_reference.projectId = vp.RuntimeValueProvider.get_value(
          'project', str, '')
    self.bigquery_wrapper.get_or_create_table(
        table_reference.projectId,
        table_reference.datasetId,
        table_reference.tableId,
        table_schema,
        self.create_disposition,
        self.write_disposition,
        additional_create_parameters=self.additional_bq_parameters)
    _KNOWN_TABLES.add(str_table_reference)

  def process(self, element, *schema_side_inputs):
    destination = bigquery_tools.get_hashable_destination(element[0])

    if callable(self.schema):
      schema = self.schema(destination, *schema_side_inputs)
    elif isinstance(self.schema, vp.ValueProvider):
      schema = self.schema.get()
    else:
      schema = self.schema

    self._create_table_if_needed(
        bigquery_tools.parse_table_reference(destination), schema)

    if not self.with_batched_input:
      row_and_insert_id = element[1]
      self._rows_buffer[destination].append(row_and_insert_id)
      self._total_buffered_rows += 1
      if len(self._rows_buffer[destination]) >= self._max_batch_size:
        return self._flush_batch(destination)
      elif self._total_buffered_rows >= self._max_buffered_rows:
        return self._flush_all_batches()
    else:
      # The input is already batched per destination, flush the rows now.
      batched_rows = element[1]
      self._rows_buffer[destination].extend(batched_rows)
      return self._flush_batch(destination)

  def finish_bundle(self):
    bigquery_tools.BigQueryWrapper.HISTOGRAM_METRIC_LOGGER.log_metrics(
        reset_after_logging=True)
    return self._flush_all_batches()

  def _flush_all_batches(self):
    _LOGGER.debug(
        'Attempting to flush to all destinations. Total buffered: %s',
        self._total_buffered_rows)

    return itertools.chain(
        *[
            self._flush_batch(destination)
            for destination in list(self._rows_buffer.keys())
            if self._rows_buffer[destination]
        ])

  def _flush_batch(self, destination):

    # Flush the current batch of rows to BigQuery.
    rows_and_insert_ids = self._rows_buffer[destination]
    table_reference = bigquery_tools.parse_table_reference(destination)

    if table_reference.projectId is None:
      table_reference.projectId = vp.RuntimeValueProvider.get_value(
          'project', str, '')

    _LOGGER.debug(
        'Flushing data to %s. Total %s rows.',
        destination,
        len(rows_and_insert_ids))
    self.batch_size_metric.update(len(rows_and_insert_ids))

    rows = [r[0] for r in rows_and_insert_ids]
    if self.ignore_insert_ids:
      insert_ids = [None for r in rows_and_insert_ids]
    else:
      insert_ids = [r[1] for r in rows_and_insert_ids]

    while True:
      start = time.time()
      passed, errors = self.bigquery_wrapper.insert_rows(
          project_id=table_reference.projectId,
          dataset_id=table_reference.datasetId,
          table_id=table_reference.tableId,
          rows=rows,
          insert_ids=insert_ids,
          skip_invalid_rows=True)
      self.batch_latency_metric.update((time.time() - start) * 1000)

      failed_rows = [rows[entry.index] for entry in errors]
      should_retry = any(
          RetryStrategy.should_retry(
              self._retry_strategy, entry.errors[0].reason) for entry in errors)
      if not passed:
        self.failed_rows_metric.update(len(failed_rows))
        message = (
            'There were errors inserting to BigQuery. Will{} retry. '
            'Errors were {}'.format(("" if should_retry else " not"), errors))
        if should_retry:
          _LOGGER.warning(message)
        else:
          _LOGGER.error(message)

      rows = failed_rows

      if not should_retry:
        break
      else:
        retry_backoff = next(self._backoff_calculator)
        _LOGGER.info(
            'Sleeping %s seconds before retrying insertion.', retry_backoff)
        time.sleep(retry_backoff)
        self._throttled_secs.inc(retry_backoff)

    self._total_buffered_rows -= len(self._rows_buffer[destination])
    del self._rows_buffer[destination]

    return [
        pvalue.TaggedOutput(
            BigQueryWriteFn.FAILED_ROWS,
            GlobalWindows.windowed_value((destination, row)))
        for row in failed_rows
    ]


# The number of shards per destination when writing via streaming inserts.
DEFAULT_SHARDS_PER_DESTINATION = 500
# The max duration a batch of elements is allowed to be buffered before being
# flushed to BigQuery.
DEFAULT_BATCH_BUFFERING_DURATION_LIMIT_SEC = 0.2


class _StreamToBigQuery(PTransform):
  def __init__(
      self,
      table_reference,
      table_side_inputs,
      schema_side_inputs,
      schema,
      batch_size,
      create_disposition,
      write_disposition,
      kms_key,
      retry_strategy,
      additional_bq_parameters,
      ignore_insert_ids,
      with_auto_sharding,
      test_client=None):
    self.table_reference = table_reference
    self.table_side_inputs = table_side_inputs
    self.schema_side_inputs = schema_side_inputs
    self.schema = schema
    self.batch_size = batch_size
    self.create_disposition = create_disposition
    self.write_disposition = write_disposition
    self.kms_key = kms_key
    self.retry_strategy = retry_strategy
    self.test_client = test_client
    self.additional_bq_parameters = additional_bq_parameters
    self.ignore_insert_ids = ignore_insert_ids
    self.with_auto_sharding = with_auto_sharding

  class InsertIdPrefixFn(DoFn):
    def start_bundle(self):
      self.prefix = str(uuid.uuid4())
      self._row_count = 0

    def process(self, element):
      key = element[0]
      value = element[1]
      insert_id = '%s-%s' % (self.prefix, self._row_count)
      self._row_count += 1
      yield (key, (value, insert_id))

  def expand(self, input):
    bigquery_write_fn = BigQueryWriteFn(
        schema=self.schema,
        batch_size=self.batch_size,
        create_disposition=self.create_disposition,
        write_disposition=self.write_disposition,
        kms_key=self.kms_key,
        retry_strategy=self.retry_strategy,
        test_client=self.test_client,
        additional_bq_parameters=self.additional_bq_parameters,
        ignore_insert_ids=self.ignore_insert_ids,
        with_batched_input=self.with_auto_sharding)

    def _add_random_shard(element):
      key = element[0]
      value = element[1]
      return ((key, random.randint(0, DEFAULT_SHARDS_PER_DESTINATION)), value)

    def _to_hashable_table_ref(table_ref_elem_kv):
      table_ref = table_ref_elem_kv[0]
      hashable_table_ref = bigquery_tools.get_hashable_destination(table_ref)
      return (hashable_table_ref, table_ref_elem_kv[1])

    def _restore_table_ref(sharded_table_ref_elems_kv):
      sharded_table_ref = sharded_table_ref_elems_kv[0]
      table_ref = bigquery_tools.parse_table_reference(sharded_table_ref)
      return (table_ref, sharded_table_ref_elems_kv[1])

    tagged_data = (
        input
        | 'AppendDestination' >> beam.ParDo(
            bigquery_tools.AppendDestinationsFn(self.table_reference),
            *self.table_side_inputs)
        | 'AddInsertIds' >> beam.ParDo(_StreamToBigQuery.InsertIdPrefixFn())
        | 'ToHashableTableRef' >> beam.Map(_to_hashable_table_ref))

    if not self.with_auto_sharding:
      tagged_data = (
          tagged_data
          | 'WithFixedSharding' >> beam.Map(_add_random_shard)
          | 'CommitInsertIds' >> ReshufflePerKey()
          | 'DropShard' >> beam.Map(lambda kv: (kv[0][0], kv[1])))
    else:
      # Auto-sharding is achieved via GroupIntoBatches.WithShardedKey
      # transform which shards, groups and at the same time batches the table
      # rows to be inserted to BigQuery.

      # Firstly the keys of tagged_data (table references) are converted to a
      # hashable format. This is needed to work with the keyed states used by
      # GroupIntoBatches. After grouping and batching is done, original table
      # references are restored.
      tagged_data = (
          tagged_data
          | 'WithAutoSharding' >> beam.GroupIntoBatches.WithShardedKey(
              (self.batch_size or BigQueryWriteFn.DEFAULT_MAX_BUFFERED_ROWS),
              DEFAULT_BATCH_BUFFERING_DURATION_LIMIT_SEC)
          | 'DropShard' >> beam.Map(lambda kv: (kv[0].key, kv[1])))

    return (
        tagged_data
        | 'FromHashableTableRef' >> beam.Map(_restore_table_ref)
        | 'StreamInsertRows' >> ParDo(
            bigquery_write_fn, *self.schema_side_inputs).with_outputs(
                BigQueryWriteFn.FAILED_ROWS, main='main'))


# Flag to be passed to WriteToBigQuery to force schema autodetection
SCHEMA_AUTODETECT = 'SCHEMA_AUTODETECT'


class WriteToBigQuery(PTransform):
  """Write data to BigQuery.

  This transform receives a PCollection of elements to be inserted into BigQuery
  tables. The elements would come in as Python dictionaries, or as `TableRow`
  instances.
  """
  class Method(object):
    DEFAULT = 'DEFAULT'
    STREAMING_INSERTS = 'STREAMING_INSERTS'
    FILE_LOADS = 'FILE_LOADS'

  def __init__(
      self,
      table,
      dataset=None,
      project=None,
      schema=None,
      create_disposition=BigQueryDisposition.CREATE_IF_NEEDED,
      write_disposition=BigQueryDisposition.WRITE_APPEND,
      kms_key=None,
      batch_size=None,
      max_file_size=None,
      max_files_per_bundle=None,
      test_client=None,
      custom_gcs_temp_location=None,
      method=None,
      insert_retry_strategy=None,
      additional_bq_parameters=None,
      table_side_inputs=None,
      schema_side_inputs=None,
      triggering_frequency=None,
      validate=True,
      temp_file_format=None,
      ignore_insert_ids=False,
      # TODO(BEAM-11857): Switch the default when the feature is mature.
      with_auto_sharding=False):
    """Initialize a WriteToBigQuery transform.

    Args:
      table (str, callable, ValueProvider): The ID of the table, or a callable
         that returns it. The ID must contain only letters ``a-z``, ``A-Z``,
         numbers ``0-9``, or connectors ``-_``. If dataset argument is
         :data:`None` then the table argument must contain the entire table
         reference specified as: ``'DATASET.TABLE'``
         or ``'PROJECT:DATASET.TABLE'``. If it's a callable, it must receive one
         argument representing an element to be written to BigQuery, and return
         a TableReference, or a string table name as specified above.
      dataset (str): The ID of the dataset containing this table or
        :data:`None` if the table reference is specified entirely by the table
        argument.
      project (str): The ID of the project containing this table or
        :data:`None` if the table reference is specified entirely by the table
        argument.
      schema (str,dict,ValueProvider,callable): The schema to be used if the
        BigQuery table to write has to be created. This can be either specified
        as a :class:`~apache_beam.io.gcp.internal.clients.bigquery.\
bigquery_v2_messages.TableSchema`. or a `ValueProvider` that has a JSON string,
        or a python dictionary, or the string or dictionary itself,
        object or a single string  of the form
        ``'field1:type1,field2:type2,field3:type3'`` that defines a comma
        separated list of fields. Here ``'type'`` should specify the BigQuery
        type of the field. Single string based schemas do not support nested
        fields, repeated fields, or specifying a BigQuery mode for fields
        (mode will always be set to ``'NULLABLE'``).
        If a callable, then it should receive a destination (in the form of
        a str, and return a str, dict or TableSchema).
        One may also pass ``SCHEMA_AUTODETECT`` here when using JSON-based
        file loads, and BigQuery will try to infer the schema for the files
        that are being loaded.
      create_disposition (BigQueryDisposition): A string describing what
        happens if the table does not exist. Possible values are:

        * :attr:`BigQueryDisposition.CREATE_IF_NEEDED`: create if does not
          exist.
        * :attr:`BigQueryDisposition.CREATE_NEVER`: fail the write if does not
          exist.

      write_disposition (BigQueryDisposition): A string describing what happens
        if the table has already some data. Possible values are:

        * :attr:`BigQueryDisposition.WRITE_TRUNCATE`: delete existing rows.
        * :attr:`BigQueryDisposition.WRITE_APPEND`: add to existing rows.
        * :attr:`BigQueryDisposition.WRITE_EMPTY`: fail the write if table not
          empty.

        For streaming pipelines WriteTruncate can not be used.
      kms_key (str): Optional Cloud KMS key name for use when creating new
        tables.
      batch_size (int): Number of rows to be written to BQ per streaming API
        insert. The default is 500.
      test_client: Override the default bigquery client used for testing.
      max_file_size (int): The maximum size for a file to be written and then
        loaded into BigQuery. The default value is 4TB, which is 80% of the
        limit of 5TB for BigQuery to load any file.
      max_files_per_bundle(int): The maximum number of files to be concurrently
        written by a worker. The default here is 20. Larger values will allow
        writing to multiple destinations without having to reshard - but they
        increase the memory burden on the workers.
      custom_gcs_temp_location (str): A GCS location to store files to be used
        for file loads into BigQuery. By default, this will use the pipeline's
        temp_location, but for pipelines whose temp_location is not appropriate
        for BQ File Loads, users should pass a specific one.
      method: The method to use to write to BigQuery. It may be
        STREAMING_INSERTS, FILE_LOADS, or DEFAULT. An introduction on loading
        data to BigQuery: https://cloud.google.com/bigquery/docs/loading-data.
        DEFAULT will use STREAMING_INSERTS on Streaming pipelines and
        FILE_LOADS on Batch pipelines.
      insert_retry_strategy: The strategy to use when retrying streaming inserts
        into BigQuery. Options are shown in bigquery_tools.RetryStrategy attrs.
        Default is to retry always. This means that whenever there are rows
        that fail to be inserted to BigQuery, they will be retried indefinitely.
        Other retry strategy settings will produce a deadletter PCollection
        as output. Appropriate values are:

        * `RetryStrategy.RETRY_ALWAYS`: retry all rows if
          there are any kind of errors. Note that this will hold your pipeline
          back if there are errors until you cancel or update it.
        * `RetryStrategy.RETRY_NEVER`: rows with errors
          will not be retried. Instead they will be output to a dead letter
          queue under the `'FailedRows'` tag.
        * `RetryStrategy.RETRY_ON_TRANSIENT_ERROR`: retry
          rows with transient errors (e.g. timeouts). Rows with permanent errors
          will be output to dead letter queue under `'FailedRows'` tag.

      additional_bq_parameters (callable): A function that returns a dictionary
        with additional parameters to pass to BQ when creating / loading data
        into a table. These can be 'timePartitioning', 'clustering', etc. They
        are passed directly to the job load configuration. See
        https://cloud.google.com/bigquery/docs/reference/rest/v2/jobs#configuration.load
      table_side_inputs (tuple): A tuple with ``AsSideInput`` PCollections to be
        passed to the table callable (if one is provided).
      schema_side_inputs: A tuple with ``AsSideInput`` PCollections to be
        passed to the schema callable (if one is provided).
      triggering_frequency (int): Every triggering_frequency duration, a
        BigQuery load job will be triggered for all the data written since
        the last load job. BigQuery has limits on how many load jobs can be
        triggered per day, so be careful not to set this duration too low, or
        you may exceed daily quota. Often this is set to 5 or 10 minutes to
        ensure that the project stays well under the BigQuery quota.
        See https://cloud.google.com/bigquery/quota-policy for more information
        about BigQuery quotas.
      validate: Indicates whether to perform validation checks on
        inputs. This parameter is primarily used for testing.
      temp_file_format: The format to use for file loads into BigQuery. The
        options are NEWLINE_DELIMITED_JSON or AVRO, with NEWLINE_DELIMITED_JSON
        being used by default. For advantages and limitations of the two
        formats, see
        https://cloud.google.com/bigquery/docs/loading-data-cloud-storage-avro
        and
        https://cloud.google.com/bigquery/docs/loading-data-cloud-storage-json.
      ignore_insert_ids: When using the STREAMING_INSERTS method to write data
        to BigQuery, `insert_ids` are a feature of BigQuery that support
        deduplication of events. If your use case is not sensitive to
        duplication of data inserted to BigQuery, set `ignore_insert_ids`
        to True to increase the throughput for BQ writing. See:
        https://cloud.google.com/bigquery/streaming-data-into-bigquery#disabling_best_effort_de-duplication
      with_auto_sharding: Experimental. If true, enables using a dynamically
        determined number of shards to write to BigQuery. This can be used for
        both FILE_LOADS and STREAMING_INSERTS. Only applicable to unbounded
        input.
    """
    self._table = table
    self._dataset = dataset
    self._project = project
    self.table_reference = bigquery_tools.parse_table_reference(
        table, dataset, project)
    self.create_disposition = BigQueryDisposition.validate_create(
        create_disposition)
    self.write_disposition = BigQueryDisposition.validate_write(
        write_disposition)
    if schema == SCHEMA_AUTODETECT:
      self.schema = schema
    else:
      self.schema = bigquery_tools.get_dict_table_schema(schema)
    self.batch_size = batch_size
    self.kms_key = kms_key
    self.test_client = test_client

    # TODO(pabloem): Consider handling ValueProvider for this location.
    self.custom_gcs_temp_location = custom_gcs_temp_location
    self.max_file_size = max_file_size
    self.max_files_per_bundle = max_files_per_bundle
    self.method = method or WriteToBigQuery.Method.DEFAULT
    self.triggering_frequency = triggering_frequency
    self.with_auto_sharding = with_auto_sharding
    self.insert_retry_strategy = insert_retry_strategy
    self._validate = validate
    self._temp_file_format = temp_file_format or bigquery_tools.FileFormat.JSON

    self.additional_bq_parameters = additional_bq_parameters or {}
    self.table_side_inputs = table_side_inputs or ()
    self.schema_side_inputs = schema_side_inputs or ()
    self._ignore_insert_ids = ignore_insert_ids

  # Dict/schema methods were moved to bigquery_tools, but keep references
  # here for backward compatibility.
  get_table_schema_from_string = \
      staticmethod(bigquery_tools.get_table_schema_from_string)
  table_schema_to_dict = staticmethod(bigquery_tools.table_schema_to_dict)
  get_dict_table_schema = staticmethod(bigquery_tools.get_dict_table_schema)

  def _compute_method(self, experiments, is_streaming_pipeline):
    # If the new BQ sink is not activated for experiment flags, then we use
    # streaming inserts by default (it gets overridden in dataflow_runner.py).
    if self.method == self.Method.DEFAULT and is_streaming_pipeline:
      return self.Method.STREAMING_INSERTS
    elif self.method == self.Method.DEFAULT and not is_streaming_pipeline:
      return self.Method.FILE_LOADS
    else:
      return self.method

  def expand(self, pcoll):
    p = pcoll.pipeline

    if (isinstance(self.table_reference, TableReference) and
        self.table_reference.projectId is None):
      self.table_reference.projectId = pcoll.pipeline.options.view_as(
          GoogleCloudOptions).project

    # TODO(pabloem): Use a different method to determine if streaming or batch.
    is_streaming_pipeline = p.options.view_as(StandardOptions).streaming

    if not is_streaming_pipeline and self.with_auto_sharding:
      raise ValueError(
          'with_auto_sharding is not applicable to batch pipelines.')

    experiments = p.options.view_as(DebugOptions).experiments or []
    method_to_use = self._compute_method(experiments, is_streaming_pipeline)

    if method_to_use == WriteToBigQuery.Method.STREAMING_INSERTS:
      if self.schema == SCHEMA_AUTODETECT:
        raise ValueError(
            'Schema auto-detection is not supported for streaming '
            'inserts into BigQuery. Only for File Loads.')

      if self.triggering_frequency:
        raise ValueError(
            'triggering_frequency can only be used with '
            'FILE_LOADS method of writing to BigQuery.')

      outputs = pcoll | _StreamToBigQuery(
          table_reference=self.table_reference,
          table_side_inputs=self.table_side_inputs,
          schema_side_inputs=self.schema_side_inputs,
          schema=self.schema,
          batch_size=self.batch_size,
          create_disposition=self.create_disposition,
          write_disposition=self.write_disposition,
          kms_key=self.kms_key,
          retry_strategy=self.insert_retry_strategy,
          additional_bq_parameters=self.additional_bq_parameters,
          ignore_insert_ids=self._ignore_insert_ids,
          with_auto_sharding=self.with_auto_sharding,
          test_client=self.test_client)

      return {BigQueryWriteFn.FAILED_ROWS: outputs[BigQueryWriteFn.FAILED_ROWS]}
    else:
      if self._temp_file_format == bigquery_tools.FileFormat.AVRO:
        if self.schema == SCHEMA_AUTODETECT:
          raise ValueError(
              'Schema auto-detection is not supported when using Avro based '
              'file loads into BigQuery. Please specify a schema or set '
              'temp_file_format="NEWLINE_DELIMITED_JSON"')
        if self.schema is None:
          raise ValueError(
              'A schema must be provided when writing to BigQuery using '
              'Avro based file loads')

      from apache_beam.io.gcp import bigquery_file_loads

      return pcoll | bigquery_file_loads.BigQueryBatchFileLoads(
          destination=self.table_reference,
          schema=self.schema,
          create_disposition=self.create_disposition,
          write_disposition=self.write_disposition,
          triggering_frequency=self.triggering_frequency,
          with_auto_sharding=self.with_auto_sharding,
          temp_file_format=self._temp_file_format,
          max_file_size=self.max_file_size,
          max_files_per_bundle=self.max_files_per_bundle,
          custom_gcs_temp_location=self.custom_gcs_temp_location,
          test_client=self.test_client,
          table_side_inputs=self.table_side_inputs,
          schema_side_inputs=self.schema_side_inputs,
          additional_bq_parameters=self.additional_bq_parameters,
          validate=self._validate,
          is_streaming_pipeline=is_streaming_pipeline)

  def display_data(self):
    res = {}
    if self.table_reference is not None:
      tableSpec = '{}.{}'.format(
          self.table_reference.datasetId, self.table_reference.tableId)
      if self.table_reference.projectId is not None:
        tableSpec = '{}:{}'.format(self.table_reference.projectId, tableSpec)
      res['table'] = DisplayDataItem(tableSpec, label='Table')
    return res

  def to_runner_api_parameter(self, context):
    from apache_beam.internal import pickler

    # It'd be nice to name these according to their actual
    # names/positions in the orignal argument list, but such a
    # transformation is currently irreversible given how
    # remove_objects_from_args and insert_values_in_args
    # are currently implemented.
    def serialize(side_inputs):
      return {(SIDE_INPUT_PREFIX + '%s') % ix:
              si.to_runner_api(context).SerializeToString()
              for ix,
              si in enumerate(side_inputs)}

    table_side_inputs = serialize(self.table_side_inputs)
    schema_side_inputs = serialize(self.schema_side_inputs)

    config = {
        'table': self._table,
        'dataset': self._dataset,
        'project': self._project,
        'schema': self.schema,
        'create_disposition': self.create_disposition,
        'write_disposition': self.write_disposition,
        'kms_key': self.kms_key,
        'batch_size': self.batch_size,
        'max_file_size': self.max_file_size,
        'max_files_per_bundle': self.max_files_per_bundle,
        'custom_gcs_temp_location': self.custom_gcs_temp_location,
        'method': self.method,
        'insert_retry_strategy': self.insert_retry_strategy,
        'additional_bq_parameters': self.additional_bq_parameters,
        'table_side_inputs': table_side_inputs,
        'schema_side_inputs': schema_side_inputs,
        'triggering_frequency': self.triggering_frequency,
        'validate': self._validate,
        'temp_file_format': self._temp_file_format,
        'ignore_insert_ids': self._ignore_insert_ids,
        'with_auto_sharding': self.with_auto_sharding,
    }
    return 'beam:transform:write_to_big_query:v0', pickler.dumps(config)

  @PTransform.register_urn('beam:transform:write_to_big_query:v0', bytes)
  def from_runner_api(unused_ptransform, payload, context):
    from apache_beam.internal import pickler
    from apache_beam.portability.api.beam_runner_api_pb2 import SideInput

    config = pickler.loads(payload)

    def deserialize(side_inputs):
      deserialized_side_inputs = {}
      for k, v in side_inputs.items():
        side_input = SideInput()
        side_input.ParseFromString(v)
        deserialized_side_inputs[k] = side_input

      # This is an ordered list stored as a dict (see the comments in
      # to_runner_api_parameter above).
      indexed_side_inputs = [(
          get_sideinput_index(tag),
          pvalue.AsSideInput.from_runner_api(si, context)) for tag,
                             si in deserialized_side_inputs.items()]
      return [si for _, si in sorted(indexed_side_inputs)]

    config['table_side_inputs'] = deserialize(config['table_side_inputs'])
    config['schema_side_inputs'] = deserialize(config['schema_side_inputs'])

    return WriteToBigQuery(**config)


class ReadFromBigQuery(PTransform):
  """Read data from BigQuery.

    This PTransform uses a BigQuery export job to take a snapshot of the table
    on GCS, and then reads from each produced file. File format is Avro by
    default.

  Args:
    table (str, callable, ValueProvider): The ID of the table, or a callable
      that returns it. The ID must contain only letters ``a-z``, ``A-Z``,
      numbers ``0-9``, or underscores ``_``. If dataset argument is
      :data:`None` then the table argument must contain the entire table
      reference specified as: ``'DATASET.TABLE'``
      or ``'PROJECT:DATASET.TABLE'``. If it's a callable, it must receive one
      argument representing an element to be written to BigQuery, and return
      a TableReference, or a string table name as specified above.
    dataset (str): The ID of the dataset containing this table or
      :data:`None` if the table reference is specified entirely by the table
      argument.
    project (str): The ID of the project containing this table.
    query (str, ValueProvider): A query to be used instead of arguments
      table, dataset, and project.
    validate (bool): If :data:`True`, various checks will be done when source
      gets initialized (e.g., is table present?). This should be
      :data:`True` for most scenarios in order to catch errors as early as
      possible (pipeline construction instead of pipeline execution). It
      should be :data:`False` if the table is created during pipeline
      execution by a previous step.
    coder (~apache_beam.coders.coders.Coder): The coder for the table
      rows. If :data:`None`, then the default coder is
      _JsonToDictCoder, which will interpret every row as a JSON
      serialized dictionary.
    use_standard_sql (bool): Specifies whether to use BigQuery's standard SQL
      dialect for this query. The default value is :data:`False`.
      If set to :data:`True`, the query will use BigQuery's updated SQL
      dialect with improved standards compliance.
      This parameter is ignored for table inputs.
    flatten_results (bool): Flattens all nested and repeated fields in the
      query results. The default value is :data:`True`.
    kms_key (str): Optional Cloud KMS key name for use when creating new
      temporary tables.
    gcs_location (str, ValueProvider): The name of the Google Cloud Storage
      bucket where the extracted table should be written as a string or
      a :class:`~apache_beam.options.value_provider.ValueProvider`. If
      :data:`None`, then the temp_location parameter is used.
    bigquery_job_labels (dict): A dictionary with string labels to be passed
      to BigQuery export and query jobs created by this transform. See:
      https://cloud.google.com/bigquery/docs/reference/rest/v2/\
              Job#JobConfiguration
    use_json_exports (bool): By default, this transform works by exporting
      BigQuery data into Avro files, and reading those files. With this
      parameter, the transform will instead export to JSON files. JSON files
      are slower to read due to their larger size.
      When using JSON exports, the BigQuery types for DATE, DATETIME, TIME, and
      TIMESTAMP will be exported as strings. This behavior is consistent with
      BigQuerySource.
      When using Avro exports, these fields will be exported as native Python
      types (datetime.date, datetime.datetime, datetime.datetime,
      and datetime.datetime respectively). Avro exports are recommended.
      To learn more about BigQuery types, and Time-related type
      representations, see: https://cloud.google.com/bigquery/docs/reference/\
              standard-sql/data-types
      To learn more about type conversions between BigQuery and Avro, see:
      https://cloud.google.com/bigquery/docs/loading-data-cloud-storage-avro\
              #avro_conversions
    temp_dataset (``google.cloud.bigquery.dataset.DatasetReference``):
        The dataset in which to create temporary tables when performing file
        loads. By default, a new dataset is created in the execution project for
        temporary tables.
   """

  COUNTER = 0

  def __init__(self, gcs_location=None, *args, **kwargs):
    if gcs_location:
      if not isinstance(gcs_location, (str, ValueProvider)):
        raise TypeError(
            '%s: gcs_location must be of type string'
            ' or ValueProvider; got %r instead' %
            (self.__class__.__name__, type(gcs_location)))

      if isinstance(gcs_location, str):
        gcs_location = StaticValueProvider(str, gcs_location)

    self.gcs_location = gcs_location

    self._args = args
    self._kwargs = kwargs

  def expand(self, pcoll):
    # TODO(BEAM-11115): Make ReadFromBQ rely on ReadAllFromBQ implementation.
    temp_location = pcoll.pipeline.options.view_as(
        GoogleCloudOptions).temp_location
    job_name = pcoll.pipeline.options.view_as(GoogleCloudOptions).job_name
    gcs_location_vp = self.gcs_location
    unique_id = str(uuid.uuid4())[0:10]

    def file_path_to_remove(unused_elm):
      gcs_location = bigquery_export_destination_uri(
          gcs_location_vp, temp_location, unique_id, True)
      return gcs_location + '/'

    files_to_remove_pcoll = beam.pvalue.AsList(
        pcoll.pipeline
        | 'FilesToRemoveImpulse' >> beam.Create([None])
        | 'MapFilesToRemove' >> beam.Map(file_path_to_remove))

    try:
      step_name = self.label
    except AttributeError:
      step_name = 'ReadFromBigQuery_%d' % ReadFromBigQuery.COUNTER
      ReadFromBigQuery.COUNTER += 1
    return (
        pcoll
        | beam.io.Read(
            _CustomBigQuerySource(
                gcs_location=self.gcs_location,
                pipeline_options=pcoll.pipeline.options,
                job_name=job_name,
                step_name=step_name,
                unique_id=unique_id,
                *self._args,
                **self._kwargs))
        | _PassThroughThenCleanup(files_to_remove_pcoll))


class ReadFromBigQueryRequest:
  """
  Class that defines data to read from BQ.
  """
  def __init__(
      self,
      query: str = None,
      use_standard_sql: bool = True,
      table: Union[str, TableReference] = None,
      flatten_results: bool = False):
    """
    Only one of query or table should be specified.

    :param query: SQL query to fetch data.
    :param use_standard_sql:
      Specifies whether to use BigQuery's standard SQL dialect for this query.
      The default value is :data:`True`. If set to :data:`False`,
      the query will use BigQuery's legacy SQL dialect.
      This parameter is ignored for table inputs.
    :param table:
      The ID of the table to read. The ID must contain only letters
      ``a-z``, ``A-Z``, numbers ``0-9``, or underscores ``_``. Table should
      define project and dataset (ex.: ``'PROJECT:DATASET.TABLE'``).
    :param flatten_results:
      Flattens all nested and repeated fields in the query results.
      The default value is :data:`False`.
    """
    self.flatten_results = flatten_results
    self.query = query
    self.use_standard_sql = use_standard_sql
    self.table = table
    self.validate()

    # We use this internal object ID to generate BigQuery export directories.
    self.obj_id = random.randint(0, 100000)

  def validate(self):
    if self.table is not None and self.query is not None:
      raise ValueError(
          'Both a BigQuery table and a query were specified.'
          ' Please specify only one of these.')
    elif self.table is None and self.query is None:
      raise ValueError('A BigQuery table or a query must be specified')
    if self.table is not None:
      if isinstance(self.table, str):
        assert self.table.find('.'), (
            'Expected a table reference '
            '(PROJECT:DATASET.TABLE or DATASET.TABLE) instead of %s'
            % self.table)


@experimental()
class ReadAllFromBigQuery(PTransform):
  """Read data from BigQuery.

    PTransform:ReadFromBigQueryRequest->Rows

    This PTransform uses a BigQuery export job to take a snapshot of the table
    on GCS, and then reads from each produced file. Data is exported into
    a new subdirectory for each export using UUIDs generated in
    `ReadFromBigQueryRequest` objects.

    It is recommended not to use this PTransform for streaming jobs on
    GlobalWindow, since it will not be able to cleanup snapshots.

  Args:
    gcs_location (str): The name of the Google Cloud Storage
      bucket where the extracted table should be written as a string. If
      :data:`None`, then the temp_location parameter is used.
    validate (bool): If :data:`True`, various checks will be done when source
      gets initialized (e.g., is table present?).
    kms_key (str): Experimental. Optional Cloud KMS key name for use when
      creating new temporary tables.
   """
  COUNTER = 0

  def __init__(
      self,
      gcs_location: Union[str, ValueProvider] = None,
      validate: bool = False,
      kms_key: str = None,
      temp_dataset: Union[str, DatasetReference] = None,
      bigquery_job_labels: Dict[str, str] = None):
    if gcs_location:
      if not isinstance(gcs_location, (str, ValueProvider)):
        raise TypeError(
            '%s: gcs_location must be of type string'
            ' or ValueProvider; got %r instead' %
            (self.__class__.__name__, type(gcs_location)))

    self.gcs_location = gcs_location
    self.validate = validate
    self.kms_key = kms_key
    self.bigquery_job_labels = bigquery_job_labels
    self.temp_dataset = temp_dataset

  def expand(self, pcoll):
    job_name = pcoll.pipeline.options.view_as(GoogleCloudOptions).job_name
    project = pcoll.pipeline.options.view_as(GoogleCloudOptions).project
    unique_id = str(uuid.uuid4())[0:10]

    try:
      step_name = self.label
    except AttributeError:
      step_name = 'ReadAllFromBigQuery_%d' % ReadAllFromBigQuery.COUNTER
      ReadAllFromBigQuery.COUNTER += 1

    sources_to_read, cleanup_locations = (
        pcoll
        | beam.ParDo(
        _BigQueryReadSplit(
            options=pcoll.pipeline.options,
            gcs_location=self.gcs_location,
            bigquery_job_labels=self.bigquery_job_labels,
            job_name=job_name,
            step_name=step_name,
            unique_id=unique_id,
            kms_key=self.kms_key,
            project=project,
            temp_dataset=self.temp_dataset)).with_outputs(
        "location_to_cleanup", main="files_to_read")
    )

    return (
        sources_to_read
        | SDFBoundedSourceReader()
        | _PassThroughThenCleanup(beam.pvalue.AsIter(cleanup_locations)))
