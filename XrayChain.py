"""
Implements a wrapper for the Amazon Xray serivce using bare boto3 client API
operations.
"""

import os
import sys
import json
import hmac
import time
import uuid
import hashlib
from functools import wraps
from threading import Lock

import boto3


class Chain(object):
    """
    A chain of events that are temporally linked in a single trace ID. Supports
    spawning child traces that will represent a tree of linked trace events.

    If it is run in an environment, such as AWS Lambda, with an
    "_X_AMZN_TRACE_ID" environment variable, then that variable's value is
    parsed and used to fill in the trace ID, parent segment ID, and whether or
    not this segment is sampled (mock).

    In all cases, the provided explicit parameters (parent_id, trace_id, mock)
    take precedence as long as all three are specified. In the event that
    provided parameters are missing or not provided, the environment follows as
    long as the `use_env_trace_params` is True. If it is False, then it falls
    back to generating a fresh trace ID and otherwise default values.
    """
    __client = None if "MOCK_XRAY" in os.environ else boto3.client("xray")

    def __init__(self,
                 backlog=100,
                 parent_id=None,
                 subsegment=False,
                 trace_id=None,
                 mock=None,
                 use_env_trace_params=True):
        # Trace IDs are public, so this ensures that trace IDs can't be guessed
        # by using this key as an HMAC with the name and the timestamp.
        self.trace_id_key = os.urandom(16)

        # Backlog is the number of segments kept in the buffer before being
        # flushed to AWS.
        self.backlog = backlog

        # The array buffer of events.
        self.segments = []

        if use_env_trace_params and "_X_AMZN_TRACE_ID" in os.environ:
            # The assumed format is:
            # - "_X_AMZN_TRACE_ID": "Root=<trace-id>;Parent=<segment-id>;Sampled=<1|0>"
            try:
                env_trace_params = dict([
                    p.split("=")
                    for p in os.environ["_X_AMZN_TRACE_ID"].split(";")
                ])
                env_trace_id = env_trace_params["Root"]
                env_parent_id = env_trace_params["Parent"]
                env_sampled = env_trace_params["Sampled"]
            except:
                env_trace_id = None
                env_parent_id = None
                env_sampled = None
        else:
            env_trace_id = None
            env_parent_id = None
            env_sampled = None

        # If forked from a parent, the trace ID is inherited.
        if trace_id is None:
            if env_trace_id is None:
                # The origin_time is used when constructing the trace ID.
                origin_time = time.time()

                # The identifier is unique to the trace ID
                identifier = hmac.new(self.trace_id_key,
                                      str(uuid.uuid1()), hashlib.sha256)

                # Construct the trace ID using HMAC to ensure that it can't be
                # spoofed.
                self.trace_id = "1-%s-%s" % (hex(int(origin_time))[2:],
                                             identifier.hexdigest()[:24])
            else:
                self.trace_id = env_trace_id
        else:
            self.trace_id = trace_id

        # If a parent ID is included, this is a child chain, and the parent ID
        # is included in all segments logged.
        if parent_id is None:
            self.parent_id = env_parent_id
        else:
            self.parent_id = parent_id

        # if true, then no AWS API calls will be made.
        if mock is not None:
            self.mock = mock
        elif env_sampled is not None:
            self.mock = False if env_sampled == "1" else True
        else:
            self.mock = False

        # last_segment_id is used when forking a child chain from this one and
        # when logging. If an event has been logged from this chain, then the
        # segment ID is stored in last_segment_id and used if a child is forked.
        self.last_segment_id = None

        # If logs for this are intended to be subsegments (contribute to the parent
        # runtime).
        self.subsegment = subsegment

        # Whenever log_start() is called, the trace is added to this dictionary
        # to lookup appropriate information when it is finished with log_end().
        self.in_progress = dict()

        # To ensure that in multithreaded situations if the same chain is being
        # used that the same event isn't flushed multiple times.
        self.flush_lock = Lock()

    def __del__(self):
        """
        Flush the chain before it gets garbage collected.
        """
        self.flush()

    def __segment_id(self):
        """
        Generate a segment ID from a UUID and the trace key.
        """
        return hmac.new(self.trace_id_key,
                        str(uuid.uuid1()).encode("ascii"),
                        hashlib.sha256).hexdigest()[:16]

    def fork_subsegment(self, parent_id=None):
        """
        Fork a subsegment chain that reports with type=subsgement.
        """
        return self.__fork(subsegment=True, parent_id=parent_id)

    def fork_root(self, parent_id=None):
        """
        Fork a chain that is a child of the given chain or parent ID, and will
        be a node in the service map.
        """
        return self.__fork(subsegment=False, parent_id=parent_id)

    def __fork(self, subsegment=True, parent_id=None):
        """
        Return a forked child trace that will have this one as a parent. Useful
        for creating a new chain of events. Because fork evnts depend on knowing
        exactly what segment to use as a parent, an optional parent_id (that can
        use the segment ID returned from a log() or log_start() call) can be
        passed in. If the subsegment value is True, then the fork creates
        independent subsegments, otherwise it creates a new root segment that
        will live as a node in the segment map.
        """
        if subsegment and self.last_segment_id is None:
            raise RuntimeError(
                ("Cannot create a subsegment fork() from a chain "
                 "that has never emitted a segment"))

        if parent_id is None:
            parent_id = self.last_segment_id

        return Chain(
            parent_id=parent_id,
            subsegment=subsegment,
            backlog=self.backlog,
            trace_id=self.trace_id,
            mock=self.mock)

    def log(self,
            start_time,
            end_time,
            name,
            metadata=None,
            annotations=None,
            http=None,
            segment_id=None):
        """
        Log a complete segment.
        """
        # When logging a segment atomically (both start and end specified),
        # there is no need to use the state of the object that is modified by
        # the logging action.
        # - Mutated state: self.segments, self.last_segment_id

        # http://docs.aws.amazon.com/xray/latest/devguide/xray-api-segmentdocuments.html
        # - The "service" key can contain an arbitrary dictionary with app info
        # - When logs are logged by the root logger, they are all roots of a
        #   chain of log messages. They appear as new independent sections of
        #   the trace.
        # - When events are logged with a parent ID and without a type=subsegment
        #   then they are new nodes that are linked hierarchically in the service map
        # - Subsegments don't appear in the service map, but do contribute to the
        #   times represented in the service map.
        if not (isinstance(name, str) or isinstance(name, unicode)):
            raise ValueError("'name' Parameter must be a string")

        segment = {
            "name": name,
            "id": segment_id if segment_id else self.__segment_id(),
            "trace_id": self.trace_id,
            "start_time": start_time,
        }

        if end_time is None:
            segment["in_progress"] = True
        else:
            segment["end_time"] = end_time

        if self.parent_id is not None:
            segment["parent_id"] = self.parent_id

        if self.subsegment:
            segment["type"] = "subsegment"

        if metadata is not None:
            segment["metadata"] = metadata

        if http is not None:
            segment["http"] = http

        if annotations is not None:
            segment["annotations"] = annotations

        self.last_segment_id = segment["id"]

        self.segments.append(json.dumps(segment))
        if len(self.segments) >= self.backlog:
            self.flush()

        return segment["id"]

    def log_start(self, name):
        """
        Log that a segment has started, and let this class handle the timing.
        In progress segments cannot have metadata or other fields.
        """
        start_time = time.time()
        segment_id = self.log(start_time=start_time, end_time=None, name=name)
        self.in_progress[segment_id] = {
            "start_time": start_time,
            "name": name,
        }
        return segment_id

    def log_end(self, segment_id, metadata=None, annotations=None, http=None):
        """
        Log that a previously started segment has ended and let this class handle
        the timing.
        """
        segment = self.in_progress[segment_id]
        del self.in_progress[segment_id]
        return self.log(
            start_time=segment["start_time"],
            end_time=time.time(),
            name=segment["name"],
            metadata=metadata,
            annotations=annotations,
            http=http,
            segment_id=segment_id)

    def trace(self, name):
        """
        Wrap a target function in timing and logging calls, holding the output
        of the wrapping segment until the end. Useful if you don't want to
        associate the inner segments with this wrapper, or if the wrapped call
        does not emit any segments.
        """

        def __decorator(target):
            @wraps(target)
            def __wrapper(*args, **kwargs):
                start = time.time()
                ret = target(*args, **kwargs)
                end = time.time()
                self.log(start, end, name)
                return ret

            return __wrapper

        return __decorator

    def trace_associated(self, name):
        """
        Wrap a target function in timing and logging calls, using log_start()
        to emit an in-progress segment for the wrapping call and a log_end when
        the call completes. Useful if you want to associate segments from within
        the call to the wrapping call.
        """

        def __decorator(target):
            @wraps(target)
            def __wrapper(*args, **kwargs):
                segment_id = self.log_start(name=name)
                ret = target(*args, **kwargs)
                self.log_end(segment_id=segment_id)
                return ret

            return __wrapper

        return __decorator

    def flush(self):
        """
        Flush the segment buffer. Can be called by a client before the backlog is
        filled. Is called when a segment is logged and the backlog is filled.
        """
        self.flush_lock.acquire()
        if len(self.segments) == 0:
            self.flush_lock.release()
            return 0

        nsegments = len(self.segments)
        if Chain.__client is not None and \
            not os.environ.get("SQUELCH_XRAY", "FALSE") == "TRUE":
            for segment in self.segments:
                sys.stderr.write(segment + "\n")
            sys.stderr.write("Submitting %d segments\n" % len(self.segments))
        if Chain.__client is not None and not self.mock:
            resp = Chain.__client.put_trace_segments(
                TraceSegmentDocuments=self.segments)
        else:
            resp = {
                "MockXray": True,
                "NoneClient": Chain.__client is None,
                "ExplicitlyMocked": self.mock
            }
        if Chain.__client is not None and \
            not os.environ.get("SQUELCH_XRAY", "FALSE") == "TRUE":
            sys.stderr.write(json.dumps(resp) + "\n")
        self.segments = []
        self.flush_lock.release()
        return nsegments
