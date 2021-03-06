import os
import datetime
import time
import yaml

import azure.storage.blob
import azure.common

import pypeliner.helpers
import pypeliner.storage
import pypeliner.flyweight

from azure.common.credentials import ServicePrincipalCredentials
from azure.mgmt.storage import StorageManagementClient

def _get_blob_key(accountname):
    blob_credentials = ServicePrincipalCredentials(client_id=os.environ["CLIENT_ID"],
                                                   secret=os.environ["SECRET_KEY"],
                                                   tenant=os.environ["TENANT_ID"])

    storage_client = StorageManagementClient(blob_credentials, os.environ["SUBSCRIPTION_ID"])
    keys = storage_client.storage_accounts.list_keys(os.environ["RESOURCE_GROUP"],
                                                     accountname)
    keys = {v.key_name: v.value for v in keys.keys}

    return keys["key1"]


def _get_blob_name(filename):
    return filename.strip('/')


class BlobMissing(Exception):
    pass


class AzureBlob(object):
    def __init__(self, storage, filename, blob_name, **kwargs):
        self.storage = storage
        self.filename = filename
        self.write_filename = filename
        self.blob_name = blob_name
        self.createtime_cache = storage.create_createtime_cache(blob_name)
    def allocate(self):
        pypeliner.helpers.makedirs(os.path.dirname(self.filename))
    def push(self):
        createtime = datetime.datetime.fromtimestamp(os.path.getmtime(self.filename)).strftime('%Y/%m/%d-%H:%M:%S')
        self.storage.push(self.blob_name, self.filename, createtime)
        self.createtime_cache.set(createtime)
    def pull(self):
        self.storage.pull(self.blob_name, self.filename)
    def get_exists(self):
        createtime = self.get_createtime()
        return createtime is not None and createtime != 'missing'
    def get_createtime(self):
        createtime = self.createtime_cache.get()
        if createtime is None:
            try:
                createtime = self.storage.retrieve_blob_createtime(self.blob_name)
            except BlobMissing:
                createtime = 'missing'
            self.createtime_cache.set(createtime)
        if createtime == 'missing':
            return None
        createtime = datetime.datetime.strptime(createtime, '%Y/%m/%d-%H:%M:%S')
        return time.mktime(createtime.timetuple())
    def touch(self):
        createtime = datetime.datetime.now().strftime('%Y/%m/%d-%H:%M:%S')
        self.storage.update_blob_createtime(self.blob_name, createtime)
        self.createtime_cache.set(createtime)
    def delete(self):
        self.storage.delete_blob(self.blob_name)
        self.createtime_cache.set(None)


class AzureBlobStorage(object):
    def __init__(self, **kwargs):
        self.storage_account_name = os.environ['AZURE_STORAGE_ACCOUNT']
        self.storage_account_key = _get_blob_key(self.storage_account_name)
        self.cached_createtimes = pypeliner.flyweight.FlyweightState()
        self.connect()
    def connect(self):
        self.blob_client = azure.storage.blob.BlockBlobService(
            account_name=self.storage_account_name,
            account_key=self.storage_account_key)
    def create_createtime_cache(self, blob_name):
        return self.cached_createtimes.create_flyweight(blob_name)
    def __enter__(self):
        self.cached_createtimes.__enter__()
        return self
    def __exit__(self, exc_type, exc_value, traceback):
        self.cached_createtimes.__exit__(exc_type, exc_value, traceback)
    def __getstate__(self):
        return (self.storage_account_name, self.storage_account_key, self.cached_createtimes)
    def __setstate__(self, state):
        self.storage_account_name, self.storage_account_key, self.cached_createtimes = state
        self.connect()
    def create_store(self, filename, extension=None, **kwargs):
        if extension is not None:
            filename = filename + extension
        blob_name = _get_blob_name(filename)
        return AzureBlob(self, filename, blob_name, **kwargs)
    def push(self, blob_name, filename, createtime):
        container_name, blob_name = self.unpack_path(blob_name)
        self.blob_client.create_container(container_name)
        self.blob_client.create_blob_from_path(
            container_name,
            blob_name,
            filename,
            metadata={'create_time': createtime})
    def unpack_path(self, filename):
        if filename.startswith('/'):
            filename = filename[1:]
        filename = filename.split('/')
        container_name = filename[0]
        filename = '/'.join(filename[1:])
        return container_name, filename
    def pull(self, blob_name, filename):
        try:
            container_name, blob_name = self.unpack_path(blob_name)
            blob = self.blob_client.get_blob_properties(
                container_name,
                blob_name)
            blob_size = blob.properties.content_length
            blob = self.blob_client.get_blob_to_path(
                container_name,
                blob_name,
                filename)
        except azure.common.AzureMissingResourceHttpError:
            print blob_name, filename
            raise pypeliner.storage.InputMissingException(blob_name)
        filesize = os.path.getsize(filename)
        assert blob_size == blob.properties.content_length
        if filesize != blob.properties.content_length:
            raise Exception('file size mismatch for {}:{} compared to {}:{}'.format(
                blob_name, blob.properties.content_length, filename, filesize))
    def retrieve_blob_createtime(self, blob_name):
        container_name, blob_name = self.unpack_path(blob_name)
        try:
            blob = self.blob_client.get_blob_properties(
                container_name,
                blob_name)
        except azure.common.AzureMissingResourceHttpError:
            raise BlobMissing((container_name, blob_name))
        if 'create_time' in blob.metadata:
            return blob.metadata['create_time']
        return blob.properties.last_modified.strftime('%Y/%m/%d-%H:%M:%S')
    def update_blob_createtime(self, blob_name, createtime):
        container_name, blob_name = self.unpack_path(blob_name)
        self.blob_client.set_blob_metadata(
            container_name,
            blob_name,
            {'create_time': createtime})
    def delete_blob(self, blob_name):
        container_name, blob_name = self.unpack_path(blob_name)
        self.blob_client.delete_blob(
            container_name,
            blob_name)

