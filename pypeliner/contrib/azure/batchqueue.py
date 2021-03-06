from __future__ import print_function
import datetime
import os
import sys
import time
import random
import string
import pickle
import logging
import uuid
import yaml

import azure.storage.blob as azureblob
import azure.batch.batch_service_client as batch
import azure.batch.batch_auth as batchauth
import azure.batch.models as batchmodels

from azure.common.credentials import ServicePrincipalCredentials
from azure.mgmt.storage import StorageManagementClient

import pypeliner.execqueue.base


def _get_blob_key(accountname):
    blob_credentials = ServicePrincipalCredentials(client_id=os.environ["CLIENT_ID"],
                                                   secret=os.environ["SECRET_KEY"],
                                                   tenant=os.environ["TENANT_ID"])

    storage_client = StorageManagementClient(blob_credentials, os.environ["SUBSCRIPTION_ID"])
    keys = storage_client.storage_accounts.list_keys(os.environ["RESOURCE_GROUP"],
                                                     accountname)
    keys = {v.key_name: v.value for v in keys.keys}

    return keys["key1"]


def unpack_path(filename):
    if filename.startswith('/'):
        filename = filename[1:]
    filename = filename.split('/')
    container_name = filename[0]
    filename = '/'.join(filename[1:])
    return container_name, filename


def print_batch_exception(batch_exception):
    """
    Prints the contents of the specified Batch exception.

    :param batch_exception:
    """
    print('-------------------------------------------')
    print('Exception encountered:')
    if (batch_exception.error and batch_exception.error.message and
            batch_exception.error.message.value):
        print(batch_exception.error.message.value)
        if batch_exception.error.values:
            print()
            for mesg in batch_exception.error.values:
                print('{}:\t{}'.format(mesg.key, mesg.value))
    print('-------------------------------------------')


def wrap_commands_in_shell(ostype, commands):
    """Wrap commands in a shell

    :param list commands: list of commands to wrap
    :param str ostype: OS type, linux or windows
    :rtype: str
    :return: a shell wrapping commands
    """
    if ostype.lower() == 'linux':
        return '/bin/bash -c \'set -e; set -o pipefail; {}; wait\''.format(
            '; '.join(commands))
    elif ostype.lower() == 'windows':
        return 'cmd.exe /c "{}"'.format('&'.join(commands))
    else:
        raise ValueError('unknown ostype: {}'.format(ostype))


def select_latest_verified_vm_image_with_node_agent_sku(
        batch_client, publisher, offer, sku_starts_with):
    """Select the latest verified image that Azure Batch supports given
    a publisher, offer and sku (starts with filter).

    :param batch_client: The batch client to use.
    :type batch_client: `batchserviceclient.BatchServiceClient`
    :param str publisher: vm image publisher
    :param str offer: vm image offer
    :param str sku_starts_with: vm sku starts with filter
    :rtype: tuple
    :return: (node agent sku id to use, vm image ref to use)
    """
    # get verified vm image list and node agent sku ids from service
    node_agent_skus = batch_client.account.list_node_agent_skus()
    # pick the latest supported sku
    skus_to_use = [
        (sku, image_ref) for sku in node_agent_skus for image_ref in sorted(
            sku.verified_image_references, key=lambda item: item.sku)
        if image_ref.publisher.lower() == publisher.lower() and
        image_ref.offer.lower() == offer.lower() and
        image_ref.sku.startswith(sku_starts_with)
    ]
    # skus are listed in reverse order, pick first for latest
    sku_to_use, image_ref_to_use = skus_to_use[0]
    return (sku_to_use.id, image_ref_to_use)


def _create_commands(commands_str):
    return filter(lambda a: len(a) > 0, commands_str.split('\n'))

def generate_blob_url(blob_client, container_name, blob_name):
    return blob_client.make_blob_url(container_name, blob_name)

def create_pool(batch_service_client, blob_client, pool_id, config):
    """
    Creates a pool of compute nodes with the specified OS settings.

    :param batch_service_client: A Batch service client.
    :type batch_service_client: `azure.batch.BatchServiceClient`
    :param str pool_id: An ID for the new pool.
    :param dict config: Configuration details.
    """

    if "IMAGE_ID" in os.environ:
         image_ref_to_use = batchmodels.ImageReference(
                              virtual_machine_image_id=os.environ["IMAGE_ID"])
         sku_to_use = os.environ["SKU"]
    else:
        sku_to_use, image_ref_to_use = \
            select_latest_verified_vm_image_with_node_agent_sku(
                batch_service_client,
                config['node_os_publisher'],
                config['node_os_offer'],
                config['node_os_sku'])
    account_name = os.environ['AZURE_STORAGE_ACCOUNT']
    account_key = _get_blob_key(account_name)

    # Get the node agent SKU and image reference for the virtual machine
    # configuration.
    # For more information about the virtual machine configuration, see:
    # https://azure.microsoft.com/documentation/articles/batch-linux-nodes/

    start_vm_commands = None
    if config.get('create_vm_commands', None):
        start_vm_commands = _create_commands(config['create_vm_commands'])
        start_vm_commands = [command.format(accountname=account_name, accountkey=account_key)
                                for command in start_vm_commands]

    data_disks = None
    if config['data_disk_sizes']:
        data_disks = []
        for i,disk_size in config['data_disk_sizes'].iteritems():
            data_disks.append(batchmodels.DataDisk(i, disk_size))

    resource_files = []

    if config['start_resources']:
        for vm_file_name, blob_path in config['start_resources'].iteritems():
            container_name, blob_name = unpack_path(blob_path)
            res_url = generate_blob_url(blob_client, container_name, blob_name)
            resource_files.append(batch.models.ResourceFile(res_url, vm_file_name))

    user = batchmodels.AutoUserSpecification(
        scope=batchmodels.AutoUserScope.pool,
        elevation_level=batchmodels.ElevationLevel.admin)
    new_pool = batch.models.PoolAddParameter(
        id=pool_id,
        virtual_machine_configuration=batchmodels.VirtualMachineConfiguration(
            image_reference=image_ref_to_use,
            node_agent_sku_id=sku_to_use,
            data_disks = data_disks),
        vm_size=config['pool_vm_size'],
        enable_auto_scale=True,
        auto_scale_formula=config['auto_scale_formula'],
        auto_scale_evaluation_interval=datetime.timedelta(minutes=5),
        start_task=batch.models.StartTask(
            command_line=wrap_commands_in_shell('linux', start_vm_commands),
            user_identity=batchmodels.UserIdentity(auto_user=user),
            resource_files=resource_files,
            wait_for_success=True),
        max_tasks_per_node=config['max_tasks_per_node'],
    )

    try:
        batch_service_client.pool.add(new_pool)
    except batchmodels.batch_error.BatchErrorException as err:
        print_batch_exception(err)
        raise


def create_job(batch_service_client, job_id, pool_id):
    """
    Creates a job with the specified ID, associated with the specified pool.

    :param batch_service_client: A Batch service client.
    :type batch_service_client: `azure.batch.BatchServiceClient`
    :param str job_id: The ID for the job.
    :param str pool_id: The ID for the pool.
    """

    job = batch.models.JobAddParameter(
        job_id,
        batch.models.PoolInformation(pool_id=pool_id))

    try:
        batch_service_client.job.add(job)
    except batchmodels.batch_error.BatchErrorException as err:
        print_batch_exception(err)
        raise


def create_blob_batch_resource(block_blob_client, container_name, file_path, blob_name, job_file_path):
    """
    Uploads a local file to an Azure Blob storage container for use as
    an azure batch resource.

    :param block_blob_client: A blob service client.
    :type block_blob_client: `azure.storage.blob.BlockBlobService`
    :param str container_name: The name of the Azure Blob storage container.
    :param str file_path: The local path to the file.
    :rtype: `azure.batch.models.ResourceFile`
    :return: A ResourceFile initialized with a SAS URL appropriate for Batch
    tasks.
    """

    block_blob_client.create_blob_from_path(container_name,
                                            blob_name,
                                            file_path)

    sas_token = block_blob_client.generate_blob_shared_access_signature(
        container_name,
        blob_name,
        permission=azureblob.BlobPermissions.READ,
        expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=120))

    sas_url = block_blob_client.make_blob_url(container_name,
                                              blob_name,
                                              sas_token=sas_token)

    return batchmodels.ResourceFile(file_path=job_file_path,
                                    blob_source=sas_url)


def delete_from_container(block_blob_client, container_name, blob_name):
    """
    Deletes a blob from an Azure Blob storage container.

    :param block_blob_client: A blob service client.
    :type block_blob_client: `azure.storage.blob.BlockBlobService`
    :param str container_name: The name of the Azure Blob storage container.
    :param str file_path: The local path to the file.
    """
    if block_blob_client.exists(container_name, blob_name):
        block_blob_client.delete_blob(container_name, blob_name)


def get_container_sas_token(block_blob_client,
                            container_name, blob_permissions):
    """
    Obtains a shared access signature granting the specified permissions to the
    container.

    :param block_blob_client: A blob service client.
    :type block_blob_client: `azure.storage.blob.BlockBlobService`
    :param str container_name: The name of the Azure Blob storage container.
    :param BlobPermissions blob_permissions:
    :rtype: str
    :return: A SAS token granting the specified permissions to the container.
    """
    # Obtain the SAS token for the container, setting the expiry time and
    # permissions. In this case, no start time is specified, so the shared
    # access signature becomes valid immediately.
    container_sas_token = \
        block_blob_client.generate_container_shared_access_signature(
            container_name,
            permission=blob_permissions,
            expiry=datetime.datetime.utcnow() + datetime.timedelta(hours=120))

    return container_sas_token


def add_task(batch_service_client, job_id, task_id, input_file,
             job_script_file,
             output_container_name, output_container_sas_token,
             output_blob_prefix, blob_client):
    """
    Adds a task for each input file in the collection to the specified job.

    :param batch_service_client: A Batch service client.
    :type batch_service_client: `azure.batch.BatchServiceClient`
    :param str job_id: The ID of the job to which to add the tasks.
    :param list input_files: A collection of input files. One task will be
     created for each input file.
    :param output_container_name: The ID of an Azure Blob storage container to
    which the tasks will upload their results.
    :param output_container_sas_token: A SAS token granting write access to
    the specified Azure Blob storage container.
    """
    # print('Adding task {} to job [{}]...'.format(task_id, job_id))

    # Test Commmand
    # This command sets up the environment enough to support
    # a simple pypeliner_delegate call
    # commands = [
    #     'wget https://repo.continuum.io/miniconda/Miniconda2-latest-Linux-x86_64.sh',
    #     'bash Miniconda2-latest-Linux-x86_64.sh -b -p $AZ_BATCH_TASK_WORKING_DIR/miniconda2',
    #     'export PATH=$AZ_BATCH_TASK_WORKING_DIR/miniconda2/bin:$PATH',
    #     'pip install --user azure-storage',
    #     'pip install --user networkx',
    #     'pip install --user --upgrade https://bitbucket.org/dranew/pypeliner/get/azure_dev.zip',
    #     './.local/bin/pypeliner_delegate {} {}'.format(input_file.file_path, output_filename),
    #     'ls *',
    # ]

    commands = ['/bin/bash job.sh']

    # WORKAROUND: for some reason there is no function to create an url for a container
    output_sas_url = blob_client.make_blob_url(
        output_container_name, 'null',
        sas_token=output_container_sas_token).replace('/null', '')

    def create_output_file(filename, blobname):
        container = batchmodels.OutputFileBlobContainerDestination(
            output_sas_url, path=blobname)
        destination = batchmodels.OutputFileDestination(container)
        upload_options = batchmodels.OutputFileUploadOptions('taskCompletion')
        return batchmodels.OutputFile(filename, destination, upload_options)


    task = batch.models.TaskAddParameter(
        task_id,
        wrap_commands_in_shell('linux', commands),
        resource_files=[
            input_file,
            job_script_file],
        output_files=[
            create_output_file('../stdout.txt', os.path.join(output_blob_prefix, 'stdout.txt')),
            create_output_file('../stderr.txt', os.path.join(output_blob_prefix, 'stderr.txt')),
            create_output_file('job_result.pickle', os.path.join(output_blob_prefix, 'job_result.pickle')),
        ]
    )

    batch_service_client.task.add(job_id, task)


def download_blobs_from_container(block_blob_client,
                                  container_name,
                                  directory_path,
                                  prefix):
    """
    Downloads all blobs from the specified Azure Blob storage container.

    :param block_blob_client: A blob service client.
    :type block_blob_client: `azure.storage.blob.BlockBlobService`
    :param container_name: The Azure Blob storage container from which to
     download files.
    :param directory_path: The local directory to which to download the files.
    """
    # print('Downloading all files from container [{}]...'.format(
    #     container_name))

    container_blobs = block_blob_client.list_blobs(container_name, prefix=prefix)

    for blob in container_blobs.items:
        destination_filename = blob.name

        # Remove prefix
        assert destination_filename.startswith(prefix)
        destination_filename = destination_filename[(len(prefix)):]
        destination_filename = destination_filename.strip('/')

        destination_file_path = os.path.join(directory_path, destination_filename)

        try:
            os.makedirs(os.path.dirname(destination_file_path))
        except:
            pass

        block_blob_client.get_blob_to_path(container_name,
                                           blob.name,
                                           destination_file_path)

    #     print('  Downloaded blob [{}] from container [{}] to {}'.format(
    #         blob.name,
    #         container_name,
    #         destination_file_path))
    # 
    # print('  Download complete!')


def _random_string(length):
    return ''.join(random.choice(string.lowercase) for i in range(length))


def check_pool_for_failed_nodes(batch_client, pool_id, logger):
    nodes = list(batch_client.compute_node.list(pool_id))

    #pool with 0 nodes
    if not nodes:
        return

    for node in nodes:
        status = batch_client.compute_node.get(pool_id, node.id).state.value
        #node states are inconsistent with lower and upper cases
        status = status.lower()
        if status in ["idle", "running", "preempted", "waitingforstarttask", "leavingpool"]:
            return

    logger.warning("All nodes are in failed state, please fix the issues and try again")


def wait_for_job_deletion(batch_client, job_id, logger):
    while True:
        try:
            job = batch_client.job.get(job_id)
        except batchmodels.batch_error.BatchErrorException:
            break

        logger.info("waiting for job deletion, job is "+job.state.value)
        time.sleep(30)

class AzureJobQueue(object):
    """ Azure batch job queue.
    """
    def __init__(self, config_filename=None, **kwargs):
        self.batch_account_url = os.environ['AZURE_BATCH_URL']
        self.storage_account_name = os.environ['AZURE_STORAGE_ACCOUNT']
        self.client_id = os.environ['CLIENT_ID']
        self.tenant_id = os.environ['TENANT_ID']
        self.secret_key = os.environ['SECRET_KEY']



        self.storage_account_name = os.environ['AZURE_STORAGE_ACCOUNT']
        self.storage_account_key = _get_blob_key(self.storage_account_name)

        with open(config_filename) as f:
            self.config = yaml.load(f)

        self.logger = logging.getLogger('pypeliner.execqueue.azure_batch')

        self.run_id = _random_string(8)

        self.logger.info('creating blob client')

        self.blob_client = azureblob.BlockBlobService(
            account_name=self.storage_account_name,
            account_key=self.storage_account_key)


        self.credentials = ServicePrincipalCredentials(client_id=self.client_id,
                                              secret=self.secret_key,
                                              tenant=self.tenant_id,
                                              resource="https://batch.core.windows.net/")

        self.logger.info('creating batch client')

        self.batch_client = batch.BatchServiceClient(
            self.credentials,
            base_url=self.batch_account_url)

        self.logger.info('creating task container')

        self.container_name = self.config['storage_container_name']
        self.blob_client.create_container(self.container_name)

        self.compute_start_commands = self.config['compute_start_commands']
        self.compute_run_command = self.config['compute_run_command']
        self.compute_finish_commands = self.config['compute_finish_commands']

        self.no_delete_pool = self.config.get('no_delete_pool', False)
        self.no_delete_job = self.config.get('no_delete_job', False)

        self.job_names = {}
        self.job_task_ids = {}
        self.job_temps_dir = {}
        self.job_blobname_prefix = {}

        self.most_recent_transition_time = None
        self.completed_task_ids = set()
        self.running_task_ids = set()

    debug_filenames = {
        'job stderr': 'stderr.txt',
        'job stdout': 'stdout.txt',
        'upload err': 'fileuploaderr.txt',
        'upload out': 'fileuploadout.txt',
    }

    def __enter__(self):
        pools = self.config["pools"]

        self.pool_job_map = {}

        #start all pools
        for pool_id, pool_params in pools.iteritems():
            # Create a new pool if needed
            if not self.batch_client.pool.exists(pool_id):
                self.logger.info("creating pool {}".format(pool_id))
                create_pool(
                    self.batch_client,
                    self.blob_client,
                    pool_id,
                    pool_params)

        for pool_id, pool_params in pools.iteritems():
            check_pool_for_failed_nodes(self.batch_client, pool_id, self.logger)

            # Create a new job if needed
            if 'job_id_override' in pool_params:
                 job_id = pool_params['job_id_override']
            else:
                 job_id = 'pypeliner_{}_{}'.format(pool_id, self.run_id)

            self.pool_job_map[pool_id] = job_id

            job_ids = set([job.id for job in self.batch_client.job.list()])
            if job_id not in job_ids:
                self.logger.info("creating job {}".format(job_id))
                create_job(
                    self.batch_client,
                    job_id,
                    pool_id)
    
            # Delete previous tasks
            for task in self.batch_client.task.list(job_id):
                self.batch_client.task.delete(job_id, task.id)

        return self
    
    def __exit__(self, exc_type, exc_value, traceback):
        self.logger.info('tear down')

        if not self.no_delete_pool:

            for pool_id, _ in self.pool_job_map.iteritems():
                self.logger.info("deleting pool {}".format(pool_id))
                try:
                    self.batch_client.pool.delete(pool_id)
                except Exception as e:
                    print(e)

        if not self.no_delete_job:
            for _, job_id in self.pool_job_map.iteritems():
                self.logger.info("deleting job {}".format(job_id))
                try:
                    self.batch_client.job.delete(job_id)
                except Exception as e:
                    print(e)
                wait_for_job_deletion(self.batch_client,  job_id, self.logger)


    def send(self, ctx, name, sent, temps_dir):
        """ Add a job to the queue.

        Args:
            ctx (dict): context of job, mem etc.
            name (str): unique name for the job
            sent (callable): callable object
            temps_dir (str): unique path for strong job temps

        """

        if "pool_id" not in ctx:
            pool=self.pool_job_map.keys()[0]
        else:
            pool = ctx["pool_id"]


        job_id = self.pool_job_map[pool]


        task_id = str(uuid.uuid1())
        self.logger.info('assigning task_id {} to job {}'.format(task_id, name))

        self.job_names[task_id] = name
        self.job_task_ids[name] = task_id
        self.job_temps_dir[name] = temps_dir
        self.job_blobname_prefix[name] = 'output_' + task_id

        job_before_file_path = 'job.pickle'
        job_before_filename = os.path.join(temps_dir, job_before_file_path)
        job_before_blobname = os.path.join(self.job_blobname_prefix[name], job_before_file_path)

        with open(job_before_filename, 'wb') as before:
            pickle.dump(sent, before)

        job_before_file = create_blob_batch_resource(
            self.blob_client, self.container_name, job_before_filename, job_before_blobname, job_before_file_path)

        job_after_file_path = 'job_result.pickle'
        job_after_filename = os.path.join(temps_dir, job_after_file_path)
        job_after_blobname = os.path.join(self.job_blobname_prefix[name], job_after_file_path)

        # Delete any previous job result file locally and in blob
        delete_from_container(
            self.blob_client, self.container_name, job_after_blobname)
        try:
            os.remove(job_after_filename)
        except OSError:
            pass

        # Delete any previous log files locally
        for debug_type, debug_filename in self.debug_filenames.iteritems():
            debug_filename = os.path.join(temps_dir, debug_filename)
            try:
                os.remove(debug_filename)
            except OSError:
                pass

        # Create a bash script for running the job
        run_script_file_path = 'job.sh'
        run_script_filename = os.path.join(temps_dir, run_script_file_path)
        run_script_blobname = os.path.join(self.job_blobname_prefix[name], run_script_file_path)

        with open(run_script_filename, 'w') as f:
            f.write('set -e\n')
            f.write('set -o pipefail\n\n')
            f.write(self.compute_start_commands + '\n')
            f.write(self.compute_run_command.format(input_filename=job_before_file_path, output_filename=job_after_file_path) + '\n')
            f.write(self.compute_finish_commands + '\n')
            f.write('wait')

        run_script_file = create_blob_batch_resource(
            self.blob_client, self.container_name, run_script_filename, run_script_blobname, run_script_file_path)

        # Obtain a shared access signature that provides write access to the output
        # container to which the tasks will upload their output.
        container_sas_token = get_container_sas_token(
            self.blob_client,
            self.container_name,
            azureblob.BlobPermissions.CREATE | azureblob.BlobPermissions.WRITE)

        # Add the task to the job
        add_task(
            self.batch_client, job_id, task_id,
            job_before_file, run_script_file,
            self.container_name, container_sas_token,
            self.job_blobname_prefix[name], self.blob_client)

        self.running_task_ids.add(task_id)

    def wait(self, immediate=False):
        """ Wait for a job to finish.

        KwArgs:
            immediate (bool): do not wait if no job has finished

        Returns:
            str: job name

        """

        timeout = datetime.timedelta(minutes=10)

        while True:
            timeout_expiration = datetime.datetime.now() + timeout

            while datetime.datetime.now() < timeout_expiration:
                for task_id in self.running_task_ids.intersection(self.completed_task_ids):
                    return self.job_names[task_id]

                if not self._update_task_state():
                    time.sleep(20)

            self.logger.warn("Tasks did not reach 'Completed' state within timeout period of " + str(timeout))

            self.logger.info("Most recent transition: {}".format(self.most_recent_transition_time))
            task_filter = "state eq 'completed'"
            list_options = batchmodels.TaskListOptions(filter=task_filter)

            for pool_id, job_id in self.pool_job_map.iteritems():
                check_pool_for_failed_nodes(self.batch_client, pool_id, self.logger)
                tasks = list(self.batch_client.task.list(job_id, task_list_options=list_options))
                self.logger.info("Received total {} tasks".format(len(tasks)))
                for task in tasks:
                    if task.id not in self.completed_task_ids:
                        self.logger.info("Missed completed task: {}".format(task.serialize()))
                        self.completed_task_ids.add(task.id)

    def _update_task_state(self, latest_transition_time=None):
        """ Query azure and update task state. """

        task_filter = "state eq 'completed'"

        if self.most_recent_transition_time is not None:
            assert self.most_recent_transition_time.tzname() == 'UTC'
            filter_time_string = self.most_recent_transition_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            task_filter += " and stateTransitionTime gt DateTime'{}'".format(filter_time_string)

        if latest_transition_time is not None:
            assert latest_transition_time.tzname() == 'UTC'
            filter_time_string = latest_transition_time.strftime("%Y-%m-%dT%H:%M:%SZ")
            task_filter += " and stateTransitionTime lt DateTime'{}'".format(filter_time_string)

        list_max_results = 1000
        list_options = batchmodels.TaskListOptions(filter=task_filter, max_results=list_max_results)

        tasks = []
        for _, job_id in self.pool_job_map.iteritems():
            tasks += list(self.batch_client.task.list(job_id, task_list_options=list_options))
        sorted_transition_times = sorted([task.state_transition_time for task in tasks])

        if len(tasks) >= list_max_results:
            mid_transition_time = sorted_transition_times[len(sorted_transition_times) / 2]
            return self._update_task_state(latest_transition_time=mid_transition_time)

        elif len(tasks) == 0:
            return False

        self.most_recent_transition_time = sorted_transition_times[-1]

        num_completed_before = len(self.completed_task_ids)
        self.completed_task_ids.update([task.id for task in tasks])
        return len(self.completed_task_ids) > num_completed_before

    def _create_error_text(self, job_temp_dir):
        error_text = ['failed to execute']

        for debug_type, debug_filename in self.debug_filenames.iteritems():
            debug_filename = os.path.join(job_temp_dir, debug_filename)

            if not os.path.exists(debug_filename):
                error_text += [debug_type + ': missing']
            
                continue
            
            with open(debug_filename, 'r') as debug_file:
                error_text += [debug_type + ':']
                
                for line in debug_file:
                    error_text += ['\t' + line.rstrip()]

        return '\n'.join(error_text)

    def receive(self, name):
        """ Receive finished job.

        Args:
            name (str): name of job to retrieve

        Returns:
            object: received object

        """

        task_id = self.job_task_ids.pop(name)
        self.job_names.pop(task_id)
        temps_dir = self.job_temps_dir.pop(name)
        blobname_prefix = self.job_blobname_prefix.pop(name)
        self.running_task_ids.remove(task_id)


        download_blobs_from_container(
            self.blob_client,
            self.container_name,
            temps_dir,
            blobname_prefix)

        job_after_filename = os.path.join(temps_dir, 'job_result.pickle')

        if not os.path.exists(job_after_filename):
            raise pypeliner.execqueue.base.ReceiveError(self._create_error_text(temps_dir))

        with open(job_after_filename, 'rb') as job_after_file:
            received = pickle.load(job_after_file)

        if received is None:
            raise pypeliner.execqueue.base.ReceiveError(self._create_error_text(temps_dir))

        return received

    @property
    def length(self):
        """ Number of jobs in the queue. """
        return len(self.job_names)

    @property
    def empty(self):
        """ Queue is empty. """
        return self.length == 0


import pypeliner.tests.jobs
import pypeliner.helpers

if __name__ == "__main__":
    exc_dir = 'tempazure'

    logging.basicConfig(level=logging.DEBUG)

    with AzureJobQueue() as queue:
        job = pypeliner.tests.jobs.TestJob()
        pypeliner.helpers.makedirs(exc_dir)

        queue.send({}, 'testjob1', job, exc_dir)

        finished_name = queue.wait()
        print(finished_name)

        job2 = queue.receive(finished_name)
        print(job2.called)

