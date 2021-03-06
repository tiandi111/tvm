# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# pylint: disable=invalid-name

""" Common utilities for auto_scheduler. """

from typing import Hashable
import multiprocessing
import multiprocessing.pool
import queue
import signal
import threading
import traceback
import os

import numpy as np

try:
    import psutil
except ImportError:
    psutil = None

from tvm import rpc
from tvm.tir import expr
from tvm.tir.transform import Simplify
from tvm.ir.transform import Sequential
from ..te import Tensor, placeholder


def get_func_name(func):
    """Get name of a function.

    Parameters
    ----------
    func: Function
        The input function.

    Returns
    -------
    name: str
        The function name.
    """
    return func.func_name if hasattr(func, "func_name") else func.__qualname__


def get_const_int(exp):
    """Verifies expr is integer and get the constant value.

    Parameters
    ----------
    exp : Union[tvm.tir.expr, int]
        The input expression.

    Returns
    -------
    out_value : int
        The output.
    """
    if isinstance(exp, int):
        return exp
    if not isinstance(exp, expr.IntImm):
        opt = Sequential([Simplify()])
        exp = opt(exp)
    if not isinstance(exp, expr.IntImm):
        raise ValueError("Expect value to be constant int")
    return exp.value


def get_const_tuple(in_tuple):
    """Verifies input tuple is IntImm, returns tuple of int.

    Parameters
    ----------
    in_tuple : Tuple[tvm.tir.expr]
        The input.

    Returns
    -------
    out_tuple : Tuple[int]
        The output.
    """
    return tuple(get_const_int(x) for x in in_tuple)


def list_to_tuple(x):
    """ Convert a list to a tuple recursively. """
    assert isinstance(x, list)
    return tuple(list_to_tuple(y) if isinstance(y, list) else y for y in x)


def serialize_args(args):
    """
    Serialize arguments of a function to a hashable and jsonable tuple.
    Currently this is mainly used for tvm.tensor.Tensor
    """
    ret = []
    for t in args:
        if isinstance(t, Tensor):
            t = ("TENSOR", get_const_tuple(t.shape), t.dtype)
        elif isinstance(t, list):
            t = list_to_tuple(t)

        assert isinstance(t, Hashable), str(t) + " is not hashable"
        ret.append(t)

    return tuple(ret)


def deserialize_args(args):
    """The inverse function of :code:`serialize_args`"""
    ret = []
    for t in args:
        if isinstance(t, (tuple, list)) and t[0] == "TENSOR":
            ret.append(placeholder(shape=t[1], dtype=t[2]))
        else:
            ret.append(t)
    return ret


def kill_child_processes(parent_pid, sig=signal.SIGTERM):
    """kill all child processes recursively"""
    if not psutil:
        raise ImportError("psutil not found, try `pip install psutil` to fix this")

    try:
        parent = psutil.Process(parent_pid)
    except psutil.NoSuchProcess:
        return

    try:
        children = parent.children(recursive=True)
        for process in children:
            process.send_signal(sig)
    except psutil.NoSuchProcess:
        return


# The maximum length of traceback information
MAX_TRACEBACK_INFO_LEN = 512


def make_traceback_info():
    """ Get the error message from traceback. """
    info = str(traceback.format_exc())
    if len(info) > MAX_TRACEBACK_INFO_LEN:
        info = (
            info[: MAX_TRACEBACK_INFO_LEN // 2] + "\n...\n" + info[-MAX_TRACEBACK_INFO_LEN // 2 :]
        )
    return info


class PropagatingThread(threading.Thread):
    """A thread that propagates the exception to the main thread"""

    def run(self):
        self.exc = None
        try:
            self.ret = self._target(*self._args, **self._kwargs)
        except Exception as e:  # pylint: disable=broad-except
            self.exc = e

    def join(self, timeout=None):
        super(PropagatingThread, self).join(timeout)
        if self.exc:
            raise self.exc
        return self.ret


def call_func_with_thread(func, args, kwargs):
    """Call a function within a new thread"""
    res = []

    def wrapper():
        res.append(func(*args, **kwargs))

    t = PropagatingThread(target=wrapper)
    t.start()
    t.join()
    return res[0]


def _func_wrapper(que, func, args, kwargs, add_thread_wrapper):
    """Call function and return the result over the queue."""
    try:
        if add_thread_wrapper:
            # Add a new layer of threadinng to avoid the conflict between
            # python's multiprocessing and tvm's thread pool.
            res = call_func_with_thread(func, args, kwargs)
        else:
            res = func(*args, **kwargs)
        que.put(res)
    except Exception:  # pylint: disable=broad-except
        que.put(Exception(make_traceback_info()))


def call_func_with_timeout(timeout, func, args=(), kwargs=None, add_thread_wrapper=False):
    """Call a function with timeout"""
    que = multiprocessing.Queue(2)
    process = multiprocessing.Process(
        target=_func_wrapper, args=(que, func, args, kwargs or {}, add_thread_wrapper)
    )
    process.start()

    try:
        res = que.get(timeout=timeout)
    except queue.Empty:
        res = TimeoutError()

    # clean queue and process
    kill_child_processes(process.pid)
    process.terminate()
    process.join()
    que.close()
    que.join_thread()
    del process
    del que

    return res


def request_remote(device_key, host=None, port=None, priority=1, timeout=60):
    """Request a remote session.

    Parameters
    ----------
    device_key : str
        The device key of registered device in tracker.
    host : Optional[str]
        The host address of rpc tracker.
        If is none, will use environment variable "TVM_TRACKER_HOST".
    port : Optional[int]
        The port of rpc tracker.
        If is none, will use environment variable "TVM_TRACKER_PORT".
    priority : int = 1
        The priority of this request, larger is more prior.
    timeout : int = 60
        The timeout of this session in second.

    Returns
    -------
    remote : RPCSession
        The connected remote RPCSession.
    """
    # connect to the tracker
    host = host or os.environ["TVM_TRACKER_HOST"]
    port = port or int(os.environ["TVM_TRACKER_PORT"])

    tracker = rpc.connect_tracker(host, port)
    remote = tracker.request(device_key, priority=priority, session_timeout=timeout)
    return remote


def check_remote(device_key, host=None, port=None, priority=100, timeout=10):
    """
    Check the availability of a remote device.

    Parameters
    ----------
    device_key: str
        device key of registered device in tracker.
    host: Optional[str]
        The host address of rpc tracker.
        If is none, will use environment variable "TVM_TRACKER_HOST".
    port: Optional[int]
        The port address of rpc tracker.
        If is none, will use environment variable "TVM_TRACKER_PORT".
    priority: int = 100
        The priority of this request, larger is more prior.
    timeout: int = 10
        The timeout of this check in seconds.

    Returns
    -------
    available: bool
        True if can find available device.
    """

    def _check():
        request_remote(device_key, host, port, priority)

    t = threading.Thread(
        target=_check,
    )
    t.start()
    t.join(timeout)
    return not t.is_alive()


def array_mean(arr):
    """Compute mean of the elments in a TVM Array<PrimExpr>

    Parameters
    ----------
    arr: Array
        A TVM Array<PrimExpr>

    Returns
    -------
    mean: float
        The mean of the elements in the array
    """
    return sum(x.value for x in arr) / len(arr)


def to_str_round(x, decimal=6):
    """Convert an object to str and round float numbers

    Parameters
    ----------
    x: Union[str, list, int, float, np.ndarray]
        The input object
    decimal: int
        The precision of decimal fraction

    Returns
    -------
    ret: str
        The string format of these objects
    """
    if isinstance(x, str):
        return x
    if isinstance(x, (list, tuple, np.ndarray)):
        return "[" + ", ".join([to_str_round(y, decimal=decimal) for y in x]) + "]"
    if isinstance(x, dict):
        return str({k: to_str_round(v) for k, v in x.items()})
    if isinstance(x, int):
        return str(x)
    if isinstance(x, (np.float32, np.float64, float)):
        format_str = "%%.%df" % decimal
        return format_str % x
    raise ValueError("Invalid value: " + str(x) + "\ttype: " + str(type(x)))
