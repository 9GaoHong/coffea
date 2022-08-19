import os
import re
import tempfile
import textwrap
import signal

from os.path import basename, join, getsize

import math
import numpy
import scipy
import random

from tqdm.auto import tqdm

import cloudpickle

from .executor import (
    WorkItem,
    _compression_wrapper,
    _decompress,
)

from .accumulator import (
    accumulate,
)


# The Work Queue object is global b/c we want to
# retain state between runs of the executor, such
# as connections to workers, cached data, etc.
_wq_queue = None

# If set to True, workflow stops processing and outputs only the results that
# have been already processed.
early_terminate = False


# This function, that accumulates results from files does not require wq.
# We declare it before checking for wq so that we do not need to install wq at
# the remote site.
def accumulate_result_files(
    chunks_accum_in_mem, files_to_accumulate, accumulator=None
):
    from coffea.processor import accumulate

    in_memory = []

    # work on local copy of list
    files_to_accumulate = list(files_to_accumulate)
    while files_to_accumulate:
        f = files_to_accumulate.pop()

        # ensure that no files are left unprocessed because lenght of list
        # smaller than desired files in memory.
        chunks_accum_in_mem = min(chunks_accum_in_mem, len(files_to_accumulate))

        with open(f, "rb") as rf:
            result_f = _decompress(result_f)

        if not accumulator:
            accumulator = result_f
            continue

        in_memory.append(result_f)
        if len(in_memory) > chunks_accum_in_mem - 1:
            accumulator = accumulate(in_memory, accumulator)
            while in_memory:
                result = in_memory.pop()  # noqa
                del result
    return accumulator


try:
    from work_queue import WorkQueue, Task
    import work_queue as wq
except ImportError:
    print("work_queue module not available")

    class Task:
        def __init__(self, *args, **kwargs):
            raise ImportError("work_queue not available")

    class WorkQueue:
        def __init__(self, *args, **kwargs):
            raise ImportError("work_queue not available")


class CoffeaWQ(WorkQueue):
    def __init__(
        self,
        executor,
    ):
        self.executor = executor
        self.console = VerbosePrint(executor.status, executor.verbose or executor.print_stdout)
        self.stats_coffea = Stats()

        if not self.executor.port:
            self.executor.port = 0 if self.executor.master_name else 9123

        super().__init__(
            port=self.executor.port,
            name=self.executor.master_name,
            debug_log=self.executor.debug_log,
            stats_log=self.executor.stats_log,
            transactions_log=self.executor.transactions_log,
            status_display_interval=self.executor.status_display_interval,
            ssl=self.executor.ssl,
        )

        # Make use of the stored password file, if enabled.
        if self.executor.password_file:
            self.specify_password_file(self.executor.password_file)

        self.function_wrapper = self._write_fn_wrapper()

        if self.executor.tasks_accum_log:
            with open(self.executor.tasks_accum_log, "w") as f:
                f.write(
                    "id,category,status,dataset,file,range_start,range_stop,accum_parent,time_start,time_end,cpu_time,memory,fin,fout\n"
                )

        self.console.printf(f"Listening for work queue workers on port {self.port}.")
        # perform a wait to print any warnings before progress bars
        self.wait(0)

    def wait(self, timeout=None):
        task = super().wait(timeout)
        if task:
            # Evaluate and display details of the completed task
            if task.successful():
                task.fout_size = getsize(task.outfile_output) / 1e6
                if task.fin_size > 0:
                    # record only if task used any intermediate inputs
                    self.stats_coffea.max("size_max_input", task.fin_size)
                self.stats_coffea.max("size_max_output", task.fout_size)
            task.report(self.executor.print_stdout, self.executor.resource_monitor)
            return task
        return None

    def application_info(self):
        return {
            "application_info": {
                "values": dict(self.stats_coffea),
                "units": {
                    "size_max_output": "MB",
                    "size_max_input": "MB",
                },
            }
        }

    def _write_fn_wrapper(self):
        """Writes a wrapper script to run serialized python functions and arguments.
        The wrapper takes as arguments the name of three files: function, argument, and output.
        The files function and argument have the serialized function and argument, respectively.
        The file output is created (or overwritten), with the serialized result of the function call.
        The wrapper created is created/deleted according to the lifetime of the WorkQueueExecutor."""

        proxy_basename = ""
        if self.executor.x509_proxy:
            proxy_basename = basename(self.executor.x509_proxy)

        contents = textwrap.dedent(
            """\
                        #!/usr/bin/env python3
                        import os
                        import sys
                        import cloudpickle
                        import coffea

                        if "{proxy}":
                            os.environ['X509_USER_PROXY'] = "{proxy}"

                        (fn, args, out) = sys.argv[1], sys.argv[2], sys.argv[3]

                        with open(fn, 'rb') as f:
                            exec_function = cloudpickle.load(f)
                        with open(args, 'rb') as f:
                            exec_args = cloudpickle.load(f)

                        pickled_out = exec_function(*exec_args)
                        with open(out, 'wb') as f:
                            f.write(pickled_out)

                        # Force an OS exit here to avoid a bug in xrootd finalization
                        os._exit(0)
                        """
        )
        with tempfile.NamedTemporaryFile(prefix="fn_wrapper", dir=self.staging_dir, delete=False) as f:
            f.write(contents.format(proxy=proxy_basename).encode())
            return f.name


class CoffeaWQTask(Task):
    tasks_counter = 0

    def __init__(
        self, queue, fn_wrapper, infile_function, item_args, itemid, tmpdir
    ):
        CoffeaWQTask.tasks_counter += 1

        self.itemid = itemid

        self.py_result = ResultUnavailable()
        self._stdout = None

        self.infile_function = infile_function

        self.infile_args = join(tmpdir, "args_{}.p".format(self.itemid))
        self.outfile_output = join(tmpdir, "out_{}.p".format(self.itemid))
        self.outfile_stdout = join(tmpdir, "stdout_{}.p".format(self.itemid))

        with open(self.infile_args, "wb") as wf:
            cloudpickle.dump(item_args, wf)

        executor = queue.executor
        self.retries_to_go = executor.retries

        super().__init__(
            self.remote_command(env_file=executor.environment_file)
        )

        self.specify_input_file(queue.function_wrapper, "fn_wrapper", cache=False)
        self.specify_input_file(infile_function, "function.p", cache=False)
        self.specify_input_file(self.infile_args, "args.p", cache=False)
        self.specify_output_file(self.outfile_output, "output.p", cache=False)
        self.specify_output_file(self.outfile_stdout, "stdout.log", cache=False)

        for f in executor.extra_input_files:
            self.specify_input_file(f, cache=True)

        if executor.x509_proxy:
            self.specify_input_file(executor.x509_proxy, cache=True)

        if executor.wrapper and executor.environment_file:
            self.specify_input_file(executor.wrapper, "py_wrapper", cache=True)
            self.specify_input_file(
                executor.environment_file, "env_file", cache=True
            )

    def __len__(self):
        return self.size

    def __str__(self):
        return str(self.itemid)

    def remote_command(self, env_file=None):
        fn_command = "python fn_wrapper function.p args.p output.p >stdout.log 2>&1"
        command = fn_command

        if env_file:
            wrap = (
                './py_wrapper -d -e env_file -u "$WORK_QUEUE_SANDBOX"/{}-env-{} -- {}'
            )
            command = wrap.format(basename(env_file), os.getpid(), fn_command)

        return command

    @property
    def std_output(self):
        if not self._stdout:
            try:
                with open(self.outfile_stdout, "r") as rf:
                    self._stdout = rf.read()
            except Exception:
                self._stdout = None
        return self._stdout

    def _has_result(self):
        return not (
            self.py_result is None or isinstance(self.py_result, ResultUnavailable)
        )

    # use output to return python result, rathern than stdout as regular wq
    @property
    def output(self):
        if not self._has_result():
            try:
                with open(self.outfile_output, "rb") as rf:
                    result = _decompress(result)
                    self.py_result = result
            except Exception as e:
                self.py_result = ResultUnavailable(e)
        return self.py_result

    def cleanup_inputs(self):
        os.remove(self.infile_args)

    def cleanup_outputs(self):
        os.remove(self.outfile_output)

    def resubmit(self, queue, tmpdir):
        if self.retries_to_go < 1 or not queue.executor.split_on_exhaustion:
            raise RuntimeError(
                "item {} failed permanently. No more retries left.".format(self.itemid)
            )

        resubmissions = []
        if self.result == wq.WORK_QUEUE_RESULT_RESOURCE_EXHAUSTION:
            queue.console("splitting {} to reduce resource consumption.", self.itemid)
            resubmissions = self.split(queue, tmpdir)
        else:
            t = self.clone(queue, tmpdir)
            t.retries_to_go = self.retries_to_go - 1
            resubmissions = [t]

        for t in resubmissions:
            queue.console(
                "resubmitting {} partly as {} with {} events. {} attempt(s) left.",
                self.itemid,
                t.itemid,
                len(t),
                t.retries_to_go,
            )
            queue.submit(t)

    def clone(self, queue, tmpdir):
        raise NotImplementedError

    def split(self, queue, tmpdir):
        raise RuntimeError("task cannot be split any further.")

    def debug_info(self):
        self.output  # load results, if needed

        has_output = "" if self._has_result() else "out"
        msg = "{} with{} result.".format(self.itemid, has_output)
        return msg

    def successful(self):
        return (self.result == 0) and (self.return_status == 0)

    def report(self, queue):
        if (not queue.console.verbose_mode) and self.successful():
            return self.successful()

        queue.console.printf(
            "{} task id {} item {} with {} events completed on {}. return code {}",
            self.category,
            self.id,
            self.itemid,
            len(self),
            self.hostname,
            self.return_status,
        )

        queue.console.printf(
            "    allocated cores: {}, memory: {} MB, disk: {} MB, gpus: {}",
            self.resources_allocated.cores,
            self.resources_allocated.memory,
            self.resources_allocated.disk,
            self.resources_allocated.gpus,
        )

        if resource_mode:
            queue.console.printf(
                "    measured cores: {}, memory: {} MB, disk {} MB, gpus: {}, runtime {}",
                self.resources_measured.cores,
                self.resources_measured.memory,
                self.resources_measured.disk,
                self.resources_measured.gpus,
                (self.cmd_execution_time) / 1e6,
            )

        if queue.executor.print_stdout or (not self.successful()):
            if self.std_output:
                queue.console.print("    output:")
                queue.console.print(self.std_output)

        if not self.successful():
            # Note that WQ already retries internal failures.
            # If we get to this point, it's a badly formed task
            info = self.debug_info()
            queue.console.printf(
                "task id {} item {} failed: {}\n    {}",
                self.id,
                self.itemid,
                self.result_str,
                info,
            )

        return self.successful()

    def task_accum_log(self, log_filename, accum_parent, status):
        # Should call write_task_accum_log with the appropiate arguments
        return NotImplementedError

    def write_task_accum_log(
        self, log_filename, accum_parent, dataset, filename, start, stop, status
    ):
        if not log_filename:
            return

        with open(log_filename, "a") as f:
            f.write(
                "{id},{cat},{status},{set},{file},{start},{stop},{accum},{time_start},{time_end},{cpu},{mem},{fin},{fout}\n".format(
                    id=self.id,
                    cat=self.category,
                    status=status,
                    set=dataset,
                    file=filename,
                    start=start,
                    stop=stop,
                    accum=accum_parent,
                    time_start=self.resources_measured.start,
                    time_end=self.resources_measured.end,
                    cpu=self.resources_measured.cpu_time,
                    mem=self.resources_measured.memory,
                    fin=self.fin_size,
                    fout=self.fout_size,
                )
            )


class PreProcCoffeaWQTask(CoffeaWQTask):
    infile_function = None

    def __init__(
        self, queue, infile_function, item, tmpdir, itemid=None
    ):
        if not itemid:
            itemid = "pre_{}".format(CoffeaWQTask.tasks_counter)

        self.item = item

        self.size = 1
        super().__init__(
            queue, infile_function, [item], itemid, tmpdir
        )

        self.specify_category("preprocessing")

        if re.search("://", item.filename) or os.path.isabs(item.filename):
            # This looks like an URL or an absolute path (assuming shared
            # filesystem). Not transfering file.
            pass
        else:
            self.specify_input_file(
                item.filename, remote_name=item.filename, cache=True
            )

        self.fin_size = 0

    def clone(self, queue, tmpdir):
        return PreProcCoffeaWQTask(
            queue,
            self.infile_function,
            self.item,
            tmpdir,
            self.itemid,
        )

    def debug_info(self):
        i = self.item
        msg = super().debug_info()
        return "{} {}".format((i.dataset, i.filename, i.treename), msg)

    def task_accum_log(self, log_filename, accum_parent, status):
        meta = list(self.output)[0].metadata
        i = self.item
        self.write_task_accum_log(
            log_filename, "", i.dataset, i.filename, 0, meta["numentries"], "done"
        )


class ProcCoffeaWQTask(CoffeaWQTask):
    def __init__(
        self, queue, infile_function, item, tmpdir, itemid=None
    ):
        self.size = len(item)

        if not itemid:
            itemid = "p_{}".format(CoffeaWQTask.tasks_counter)

        self.item = item

        super().__init__(
                queue, infile_function, [item], itemid, tmpdir
        )

        self.specify_category("processing")

        if re.search("://", item.filename) or os.path.isabs(item.filename):
            # This looks like an URL or an absolute path (assuming shared
            # filesystem). Not transfering file.
            pass
        else:
            self.specify_input_file(
                item.filename, remote_name=item.filename, cache=True
            )

        self.fin_size = 0

    def clone(self, queue, tmpdir):
        return ProcCoffeaWQTask(
            queue,
            self.infile_function,
            self.item,
            tmpdir,
            self.itemid,
        )

    def split(self, queue, tmpdir):
        total = len(self.item)

        if total < 2:
            raise RuntimeError("processing task cannot be split any further.")

        # if the chunksize was updated to be less than total, then use that.
        # Otherwise, just partition the task in two.
        target_chunksize = queue.current_chunksize
        if total <= target_chunksize:
            target_chunksize = math.ceil(total / 2)

        n = max(math.ceil(total / target_chunksize), 1)
        actual_chunksize = int(math.ceil(total / n))

        queue.stats_coffea.inc("chunks_split")
        queue.stats_coffea.min("min_chunksize_after_split", actual_chunksize)

        splits = []
        start = self.item.entrystart
        while start < self.item.entrystop:
            stop = min(self.item.entrystop, start + actual_chunksize)

            w = WorkItem(
                self.item.dataset,
                self.item.filename,
                self.item.treename,
                start,
                stop,
                self.item.fileuuid,
                self.item.usermeta,
            )

            t = self.__class__(queue, self.infile_function, w, tmpdir)

            start = stop
            splits.append(t)

        return splits

    def debug_info(self):
        i = self.item
        msg = super().debug_info()
        return "{} {}".format(
            (i.dataset, i.filename, i.treename, i.entrystart, i.entrystop), msg
        )

    def task_accum_log(self, log_filename, accum_parent, status):
        i = self.item
        self.write_task_accum_log(
            log_filename,
            accum_parent,
            i.dataset,
            i.filename,
            i.entrystart,
            i.entrystop,
            status,
        )


class AccumCoffeaWQTask(CoffeaWQTask):
    def __init__(
        self,
        queue,
        infile_function,
        tasks_to_accumulate,
        tmpdir,
        chunks_accum_in_mem,
        itemid=None,
    ):
        if not itemid:
            itemid = "accum_{}".format(CoffeaWQTask.tasks_counter)

        self.tasks_to_accumulate = tasks_to_accumulate
        self.size = sum(len(t) for t in self.tasks_to_accumulate)

        args = [chunks_accum_in_mem]
        args = args + [[basename(t.outfile_output) for t in self.tasks_to_accumulate]]

        super().__init__(
            queue, infile_function, args, itemid, tmpdir
        )

        self.specify_category("accumulating")

        for t in self.tasks_to_accumulate:
            self.specify_input_file(t.outfile_output, cache=False)

        self.fin_size = sum(t.fout_size for t in tasks_to_accumulate)

    def cleanup_inputs(self):
        super().cleanup_inputs()
        # cleanup files associated with results already accumulated
        for t in self.tasks_to_accumulate:
            t.cleanup_outputs()

    def clone(self, queue, tmpdir):
        return AccumCoffeaWQTask(
            queue,
            self.infile_function,
            self.tasks_to_accumulate,
            tmpdir,
            self.itemid,
        )

    def debug_info(self):
        tasks = self.tasks_to_accumulate

        msg = super().debug_info()

        results = [
            CoffeaWQTask.debug_info(t)
            for t in tasks
            if isinstance(t, AccumCoffeaWQTask)
        ]
        results += [
            t.debug_info() for t in tasks if not isinstance(t, AccumCoffeaWQTask)
        ]

        return "{} accumulating: [{}] ".format(msg, "\n".join(results))

    def task_accum_log(self, log_filename, status, accum_parent=None):
        self.write_task_accum_log(
            log_filename, accum_parent, "", "", 0, len(self), status
        )


def work_queue_main(executor, items, function, accumulator):
    """Execute using Work Queue
    For more information, see :ref:`intro-coffea-wq`
    """

    global _wq_queue

    _check_dynamic_chunksize_targets(executor.dynamic_chunksize)

    if executor.environment_file and not executor.environment_file.wrapper:
        raise ValueError(
            "Location of python_package_run could not be determined automatically.\nUse 'wrapper' argument to the work_queue_executor."
        )

    if executor.compression is None:
        self.compression = 1

    function = _compression_wrapper(executor.compression, function)
    accumulate_fn = _compression_wrapper(executor.compression, accumulate_result_files)

    if _wq_queue is None:
        _wq_queue = CoffeaWQ(executor)

    _declare_resources(executor)

    # Working within a custom temporary directory:
    try:
        tmpdir_inst = tempfile.TemporaryDirectory(
            prefix="wq-executor-tmp-", dir=executor.filepath
        )
        tmpdir = tmpdir_inst.name

        infile_function = _function_to_file(
            function, prefix_name=executor.function_name, tmpdir=tmpdir
        )
        infile_accum_fn = _function_to_file(
            accumulate_fn, prefix_name="accum", tmpdir=tmpdir
        )

        if executor.custom_init:
            executor.custom_init(_wq_queue)

        if executor.desc == "Preprocessing":
            result = _work_queue_preprocessing(
                items, accumulator, fn_wrapper, infile_function, tmpdir
            )
            # we do not shutdown queue after preprocessing, as we want to
            # keep the connected workers for processing/accumulation
        else:
            result = _work_queue_processing(
                items,
                accumulator,
                fn_wrapper,
                infile_function,
                infile_accum_fn,
                tmpdir,
            )
            _wq_queue = None
    except Exception as e:
        _wq_queue = None
        raise e
    finally:
        tmpdir_inst.cleanup()
    return result


def _work_queue_processing(
    queue,
    items,
    accumulator,
    fn_wrapper,
    infile_function,
    infile_accum_fn,
    tmpdir,
):

    # Keep track of total tasks in each state.
    items_submitted = 0
    items_done = 0

    # triplets of num of events, wall_time, memory
    task_reports = []

    # tasks with results to accumulate, sorted by the number of events
    tasks_to_accumulate = []

    # ensure items looks like a generator
    if isinstance(items, list):
        items = iter(items)

    executor = queue.executor

    items_total = executor.events_total

    # "chunksize" is the original chunksize passed to the executor. Always used
    # if dynamic_chunksize is not given.
    chunksize = executor.chunksize
    queue.stats_coffea.set("original_chunksize", chunksize)
    queue.stats_coffea.set("current_chunksize", chunksize)

    # keep a record of the latest computed chunksize, if any
    queue.current_chunksize = chunksize

    progress_bars = _make_progress_bars(executor)

    signal.signal(signal.SIGINT, _handle_early_terminate)

    # Main loop of executor
    while (not early_terminate and items_done < items_total) or not queue.empty():
        update_chunksize = items_submitted > 0 and executor.dynamic_chunksize
        if update_chunksize:
            chunksize = _compute_chunksize(executor.chunksize, executor.dynamic_chunksize, task_reports)
            queue.stats_coffea.set("current_chunksize", chunksize)
            queue.console("current chunksize {}", chunksize)
            chunksize = _sample_chunksize(chunksize)

        while (
            items_submitted < items_total and queue.hungry() and not early_terminate
        ):
            task = _submit_proc_task(
                executor,
                fn_wrapper,
                infile_function,
                items,
                chunksize,
                update_chunksize,
                tmpdir,
            )
            items_submitted += len(task)
            progress_bars["submit"].update(len(task))

        # When done submitting, look for completed tasks.
        task = queue.wait(5)

        # refresh progress bars
        for bar in progress_bars.values():
            bar.update(0)

        if task:
            if not task.successful():
                task.resubmit(executor, tmpdir)
            else:
                tasks_to_accumulate.append(task)

                if task.category == "processing":
                    items_done += len(task)
                    progress_bars["process"].update(len(task))

                    # gather statistics for dynamic chunksize
                    task_reports.append(
                        (
                            len(task),
                            (task.cmd_execution_time) / 1e6,
                            task.resources_measured.memory,
                        )
                    )
                else:
                    for t in task.tasks_to_accumulate:
                        t.task_accum_log(
                            queue.executor.tasks_accum_log,
                            status="accumulated",
                            accum_parent=task.id,
                        )
                    progress_bars["accumulate"].update(1)

                force_last_accum = (items_done >= items_total) or early_terminate
                tasks_to_accumulate = _submit_accum_tasks(
                    executor,
                    fn_wrapper,
                    infile_accum_fn,
                    tasks_to_accumulate,
                    force_last_accum,
                    tmpdir,
                )
                acc_sub = queue.stats_category("accumulating").tasks_submitted
                progress_bars["accumulate"].total = math.ceil(
                    1 + (items_total * acc_sub / items_done)
                )

                # Remove input files as we go to avoid unbounded disk
                # we do not remove outputs, as they are used by further accumulate tasks
                task.cleanup_inputs()

    if items_done < items_total:
        queue.console.printf("\nWARNING: Not all items were processed.\n")

    accumulator = _final_accumulation(queue, accumulator, tasks_to_accumulate)
    progress_bars["accumulate"].update(1)
    progress_bars["accumulate"].refresh()

    for bar in progress_bars.values():
        bar.close()

    for t in tasks_to_accumulate:
        t.task_accum_log(
            queue.executor.tasks_accum_log, status="accumulated", accum_parent=0
        )

    if executor.dynamic_chunksize:
        queue.console("final chunksize {}",
                _compute_chunksize(executor.chunksize, executor.dynamic_chunksize, task_reports))
    return accumulator


def _handle_early_terminate(signum, frame):
    global early_terminate

    if early_terminate:
        raise KeyboardInterrupt
    else:
        _wq_queue.console.printf(
            "********************************************************************************"
        )
        _wq_queue.console.printf("Canceling processing tasks for final accumulation.")
        _wq_queue.console.printf("C-c again to immediately terminate.")
        _wq_queue.console.printf(
            "********************************************************************************"
        )
        early_terminate = True
        _wq_queue.cancel_by_category("processing")


def _final_accumulation(queue, accumulator, tasks_to_accumulate):
    if len(tasks_to_accumulate) < 1:
        raise RuntimeError("No results available.")
    elif len(tasks_to_accumulate) > 1:
        _wq_queue.console.printf(
            "Not all results ({}) were accumulated in an accumulation job. Accumulating locally.".format(
                len(tasks_to_accumulate)
            )
        )

    queue.console("Performing final accumulation...")
    accumulator = accumulate_result_files(
        2, [t.outfile_output for t in tasks_to_accumulate], accumulator
    )
    for t in tasks_to_accumulate:
        t.cleanup_outputs()
    queue.console("done")

    return accumulator


def _work_queue_preprocessing(
    queue, items, accumulator, fn_wrapper, infile_function, tmpdir
):
    preprocessing_bar = tqdm(
        desc="Preprocessing",
        total=len(items),
        disable=not queue.executor.status,
        unit=executor.unit,
        bar_format=executor.bar_format,
    )

    for item in items:
        task = PreProcCoffeaWQTask(
            executor, fn_wrapper, infile_function, item, tmpdir
        )
        queue.submit(task)
        queue.console("submitted preprocessing task {}", task.id)

    while not queue.empty():
        task = queue.wait(5)
        if task:
            if task.successful():
                accumulator = accumulate([task.output], accumulator)
                preprocessing_bar.update(1)
                task.cleanup_inputs()
                task.cleanup_outputs()
                task.task_accum_log(queue.executor.tasks_accum_log, "", "done")
            else:
                task.resubmit(queue, tmpdir)

    preprocessing_bar.close()

    return accumulator


def _declare_resources(executor):
    # If explicit resources are given, collect them into default_resources
    default_resources = {}
    if executor.cores:
        default_resources["cores"] = executor.cores
    if executor.memory:
        default_resources["memory"] = executor.memory
    if executor.disk:
        default_resources["disk"] = executor.disk
    if executor.gpus:
        default_resources["gpus"] = executor.gpus

    # Enable monitoring and auto resource consumption, if desired:
    _wq_queue.tune("category-steady-n-tasks", 3)

    # Evenly divide resources in workers per category
    _wq_queue.tune("force-proportional-resources", 1)


    # if resource_monitor is given, and not 'off', then monitoring is activated.
    # anything other than 'measure' is assumed to be 'watchdog' mode, where in
    # addition to measuring resources, tasks are killed if they go over their
    # resources.
    monitor_enabled = True
    watchdog_enabled = True
    if not executor.resource_monitor or executor.resource_monitor == "off":
        monitor_enabled = False
    elif executor.resource_monitor == "measure":
        watchdog_enabled = False

    # activate monitoring if it has not been explicitely activated and we are
    # using an automatic resource allocation.
    if executor.resources_mode != "fixed":
        monitor_enabled = True

    if monitor_enabled:
        _wq_queue.enable_monitoring(watchdog=watchdog_enabled)

    for category in "default preprocessing processing accumulating".split():
        _wq_queue.specify_category_max_resources(category, default_resources)

        if executor.resources_mode != "fixed":
            _wq_queue.specify_category_mode(category, wq.WORK_QUEUE_ALLOCATION_MODE_MAX)

            if (
                category == "processing"
                and executor.resources_mode == "max-throughput"
            ):
                _wq_queue.specify_category_mode(
                    category, wq.WORK_QUEUE_ALLOCATION_MODE_MAX_THROUGHPUT
                )

        # enable fast termination of workers
        if (
            executor.fast_terminate_workers
            and executor.fast_terminate_workers > 1
        ):
            _wq_queue.activate_fast_abort_category(
                category, executor.fast_terminate_workers
            )


def _submit_proc_task(
    executor,
    fn_wrapper,
    infile_function,
    items,
    chunksize,
    update_chunksize,
    tmpdir
):
    if update_chunksize:
        item = items.send(chunksize)
        _wq_queue.current_chunksize = chunksize
    else:
        item = next(items)

    task = ProcCoffeaWQTask(executor, fn_wrapper, infile_function, item, tmpdir)
    task_id = _wq_queue.submit(task)
    _wq_queue.console(
        "submitted processing task id {} item {}, with {} events",
        task_id,
        task.itemid,
        len(task),
    )

    return task


def _submit_accum_tasks(
    executor,
    fn_wrapper,
    infile_function,
    tasks_to_accumulate,
    force_last_accum,
    tmpdir,
):

    chunks_per_accum = executor.chunks_per_accum
    chunks_accum_in_mem = executor.chunks_accum_in_mem

    if chunks_per_accum < 2 or chunks_accum_in_mem < 2:
        raise RuntimeError("A minimum of two chunks should be used when accumulating")

    if len(tasks_to_accumulate) < 2 * chunks_per_accum - 1 and not force_last_accum:
        return tasks_to_accumulate

    tasks_to_accumulate.sort(key=lambda t: t.fout_size)
    for next_to_accum in _group_lst(tasks_to_accumulate, chunks_per_accum):
        # return immediately if not enough for a single accumulation
        if len(next_to_accum) < 2:
            return next_to_accum

        if len(next_to_accum) < chunks_per_accum and not force_last_accum:
            # not enough tasks for a chunks_per_accum, and not all events have
            # been processed.
            return next_to_accum

        accum_task = AccumCoffeaWQTask(
            executor, fn_wrapper, infile_function, next_to_accum, tmpdir
        )
        task_id = _wq_queue.submit(accum_task)
        _wq_queue.console(
            "submitted accumulation task id {} item {}, with {} events",
            task_id,
            accum_task.itemid,
            len(accum_task),
        )

    # if we get here all tasks in tasks_to_accumulate were included in an
    # accumulation.
    return []


def _group_lst(lst, n):
    """Split the lst into sublists of len n."""
    return (lst[i : i + n] for i in range(0, len(lst), n))



def _function_to_file(function, prefix_name=None, tmpdir=None):
    with tempfile.NamedTemporaryFile(
        prefix=prefix_name, suffix="_fn.p", dir=tmpdir, delete=False
    ) as f:
        cloudpickle.dump(function, f)
        return f.name


def _get_x509_proxy(x509_proxy=None):
    if x509_proxy:
        return x509_proxy

    x509_proxy = os.environ.get("X509_USER_PROXY", None)
    if x509_proxy:
        return x509_proxy

    x509_proxy = join(
        os.environ.get("TMPDIR", "/tmp"), "x509up_u{}".format(os.getuid())
    )
    if os.path.exists(x509_proxy):
        return x509_proxy

    return None


def _make_progress_bars(executor):
    items_total = executor.events_total
    status = executor.status
    unit = executor.unit
    bar_format = executor.bar_format
    chunksize = executor.chunksize
    chunks_per_accum = executor.chunks_per_accum

    submit_bar = tqdm(
        total=items_total,
        disable=not status,
        unit=unit,
        desc="Submitted",
        bar_format=bar_format,
        miniters=1,
    )

    processed_bar = tqdm(
        total=items_total,
        disable=not status,
        unit=unit,
        desc="Processing",
        bar_format=bar_format,
    )

    accumulated_bar = tqdm(
        total=1 + int(items_total / (chunksize * chunks_per_accum)),
        disable=not status,
        unit="task",
        desc="Accumulated",
        bar_format=bar_format,
    )

    return {
        "submit": submit_bar,
        "process": processed_bar,
        "accumulate": accumulated_bar,
    }


def _check_dynamic_chunksize_targets(targets):
    if targets:
        for k in targets:
            if k not in ["wall_time", "memory"]:
                raise KeyError("dynamic chunksize resource {} is unknown.".format(k))


class ResultUnavailable(Exception):
    pass


class Stats(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def inc(self, stat, delta=1):
        try:
            self[stat] += delta
        except KeyError:
            self[stat] = delta

    def set(self, stat, value):
        self[stat] = value

    def get(self, stat, default=None):
        return self.setdefault(stat, 0)

    def min(self, stat, value):
        try:
            self[stat] = min(self[stat], value)
        except KeyError:
            self[stat] = value

    def max(self, stat, value):
        try:
            self[stat] = max(self[stat], value)
        except KeyError:
            self[stat] = value


class VerbosePrint:
    def __init__(self, status_mode=True, verbose_mode=True):
        self.status_mode = status_mode
        self.verbose_mode = verbose_mode

    def __call__(self, format_str, *args, **kwargs):
        if self.verbose_mode:
            self.printf(format_str, *args, **kwargs)

    def print(self, msg):
        if self.status_mode:
            tqdm.write(msg)
        else:
            print(msg)

    def printf(self, format_str, *args, **kwargs):
        msg = format_str.format(*args, **kwargs)
        self.print(msg)

def _floor_to_pow2(value):
    if value < 1:
        return 1
    return pow(2, math.floor(math.log2(value)))


def _sample_chunksize(chunksize):
    # sample between value found and half of it, to better explore the
    # space.  we take advantage of the fact that the function that
    # generates chunks tries to have equally sized work units per file.
    # Most files have a different number of events, which is unlikely
    # to be a multiple of the chunsize computed. Just in case all files
    # have the same number of events, we return chunksize/2 10% of the
    # time.
    return int(random.choices([chunksize, max(chunksize / 2, 1)], weights=[90, 10])[0])


def _compute_chunksize(base_chunksize, resource_targets, task_reports):
    chunksize_time = None
    chunksize_memory = None

    if resource_targets is not None and len(task_reports) > 1:
        target_time = resource_targets.get("wall_time", None)
        if target_time:
            chunksize_time = _compute_chunksize_target(
                target_time, [(time, evs) for (evs, time, mem) in task_reports]
            )

        target_memory = resource_targets["memory"]
        if target_memory:
            chunksize_memory = _compute_chunksize_target(
                target_memory, [(mem, evs) for (evs, time, mem) in task_reports]
            )

    candidate_sizes = [c for c in [chunksize_time, chunksize_memory] if c]
    if candidate_sizes:
        chunksize = min(candidate_sizes)
    else:
        chunksize = base_chunksize

    try:
        chunksize = int(_floor_to_pow2(chunksize))
    except ValueError:
        chunksize = base_chunksize

    return chunksize


def _compute_chunksize_target(target, pairs):
    # if no info to compute dynamic chunksize (e.g. they info is -1), return nothing
    if len(pairs) < 1 or pairs[0][0] < 0:
        return None

    avgs = [e / max(1, target) for (target, e) in pairs]
    quantiles = numpy.quantile(avgs, [0.25, 0.5, 0.75], interpolation="nearest")

    # remove outliers below the 25%
    pairs_filtered = []
    for (i, avg) in enumerate(avgs):
        if avg >= quantiles[0]:
            pairs_filtered.append(pairs[i])

    try:
        # separate into time, numevents arrays
        slope, intercept, r_value, p_value, std_err = scipy.stats.linregress(
            [rep[0] for rep in pairs_filtered],
            [rep[1] for rep in pairs_filtered],
        )
    except Exception:
        slope = None

    if (
        slope is None
        or numpy.isnan(slope)
        or numpy.isnan(intercept)
        or slope < 0
        or intercept > 0
    ):
        # we assume that chunksize and target have a positive
        # correlation, with a non-negative overhead (-intercept/slope). If
        # this is not true because noisy data, use the avg chunksize/time.
        # slope and intercept may be nan when data falls in a vertical line
        # (specially at the start)
        slope = quantiles[1]
        intercept = 0

    org = (slope * target) + intercept

    return org
