"""gludb.backends.dynamodb - backend dynamodb database module
"""

import os

from uuid import uuid4

import boto.exception
import boto.dynamodb2  # NOQA

from boto.dynamodb2.layer1 import DynamoDBConnection
from boto.dynamodb2.table import Table
from boto.dynamodb2.fields import HashKey, GlobalIncludeIndex
from boto.dynamodb2.items import Item
from boto.dynamodb2.exceptions import ResourceNotFoundException, ItemNotFound
from boto.exception import JSONResponseError


def uuid():
    return uuid4().hex


def get_conn():
    """Return a connection to DynamoDB (and handle local/debug possibilities)
    """
    if os.environ.get('DEBUG', None):
        # In DEBUG mode - use the local DynamoDB
        conn = DynamoDBConnection(
            host='localhost',
            port=8000,
            aws_access_key_id='TEST',
            aws_secret_access_key='TEST',
            is_secure=False
        )
    elif os.environ.get('travis', None):
        # TODO: we will need some customization for testing on Travis - see
        #       https://github.com/nabeken/goamz-dynamodb
        conn = None
    else:
        # Regular old production
        conn = DynamoDBConnection()

    return conn


def gsi_name(index_name):
    """Standardize how we create a GSI name for DynamoDB from the given index
    name from the class"""
    return index_name + '_gsiindex'


class DynamoMappings(object):
    """DynamoDB has some opinions about what you can store or query in an
    attribute. We're going to use mappings to fix that.
    """

    # Yes, the bracket-ish map vals are illegal in just about any language that
    # uses bracket-type syntax. That's on purpose.
    NONE_VAL = "<{[None>}]"
    EMPTY_STR_VAL = "<{[''>}]"

    @staticmethod
    def map_index_val(index_val):
        """Xform index_val so that it can be stored/queried"""
        if index_val is None:
            return DynamoMappings.NONE_VAL

        index_val = str(index_val)
        if not index_val:
            return DynamoMappings.EMPTY_STR_VAL

        return index_val

    @staticmethod
    def unmap_stored_val(stored_val):
        """Inverse of index_val_mapping. Note that we currently don't use it
        because we don't actually read back index values (since they are
        generated by Python functions)"""
        if stored_val == DynamoMappings.NONE_VAL:
            return None
        elif stored_val == DynamoMappings.EMPTY_STR_VAL:
            return ''
        else:
            return stored_val


def delete_table(table_name):
    """Mainly for testing"""
    Table(table_name, connection=get_conn(), schema=[HashKey('id')]).delete()


class Backend(object):
    def __init__(self, **kwrds):
        pass  # No current keywords needed/used

    def table_schema_call(self, target, cls):
        """Call the callable target with the args and keywords needed for the
        table defined by cls. This is how we centralize the Table.create and
        Table ctor calls
        """
        index_defs = []
        for name in cls.index_names() or []:
            index_defs.append(GlobalIncludeIndex(
                gsi_name(name),
                parts=[HashKey(name)],
                includes=['value']
            ))

        return target(
            cls.get_table_name(),
            connection=get_conn(),
            schema=[HashKey('id')],
            global_indexes=index_defs or None
        )

    def ensure_table(self, cls):
        exists = True
        conn = get_conn()

        try:
            descrip = conn.describe_table(cls.get_table_name())
            assert descrip is not None
        except ResourceNotFoundException:
            # Expected - this is what we get if there is no table
            exists = False
        except JSONResponseError:
            # Also assuming no table
            exists = False

        if not exists:
            table = self.table_schema_call(Table.create, cls)
            assert table is not None

    def get_class_table(self, cls):
        """Return a DynamoDB table object for the given class
        """
        return self.table_schema_call(Table, cls)

    def find_one(self, cls, id):
        try:
            db_result = self.get_class_table(cls).lookup(id)
        except ItemNotFound:
            # according to docs, this shouldn't be required, but it IS
            db_result = None

        if not db_result:
            return None

        obj = cls.from_data(db_result['value'])
        return obj

    def find_all(self, cls):
        final_results = []
        table = self.get_class_table(cls)
        for db_result in table.scan():
            obj = cls.from_data(db_result['value'])
            final_results.append(obj)

        return final_results

    def find_by_index(self, cls, index_name, value):
        query_args = {
            index_name + '__eq': DynamoMappings.map_index_val(value),
            'index': gsi_name(index_name)
        }

        final_results = []
        for db_result in self.get_class_table(cls).query_2(**query_args):
            obj = cls.from_data(db_result['value'])
            final_results.append(obj)

        return final_results

    def save(self, obj):
        if not obj.id:
            obj.id = uuid()

        stored_data = {
            'id': obj.id,
            'value': obj.to_data()
        }

        index_vals = obj.indexes() or {}
        for key in obj.__class__.index_names() or []:
            val = index_vals.get(key, '')
            stored_data[key] = DynamoMappings.map_index_val(val)

        table = self.get_class_table(obj.__class__)
        item = Item(table, data=stored_data)

        item.save(overwrite=True)
