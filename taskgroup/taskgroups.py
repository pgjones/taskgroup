# backported from cpython 3.12 bceb197947bbaebb11e01195bdce4f240fdf9332
# Copyright © 2001-2022 Python Software Foundation; All Rights Reserved
# modified to support working on 3.10

from __future__ import annotations
from contextvars import Context

__all__ = ["TaskGroup"]

import sys
from asyncio import events
from asyncio import exceptions
from asyncio import tasks
from collections.abc import AsyncGenerator, Coroutine
from typing import Any, TypeVar

from exceptiongroup import BaseExceptionGroup
import contextlib
from .tasks import task_factory as _task_factory, Task
from . import install as _install

from typing_extensions import Self

_T = TypeVar("_T")


class TaskGroup:
    def __init__(self) -> None:
        self._entered = False
        self._exiting = False
        self._aborting = False
        self._loop = None
        self._parent_task = None
        self._parent_cancel_requested = False
        self._tasks = set()
        self._errors = []
        self._base_error = None
        self._on_completed_fut = None
        self._cmgr = self._cmgr_factory()

    def __repr__(self) -> str:
        info = [""]
        if self._tasks:
            info.append(f"tasks={len(self._tasks)}")
        if self._errors:
            info.append(f"errors={len(self._errors)}")
        if self._aborting:
            info.append("cancelling")
        elif self._entered:
            info.append("entered")

        info_str = " ".join(info)
        return f"<TaskGroup{info_str}>"

    @contextlib.asynccontextmanager
    async def _cmgr_factory(self) -> AsyncGenerator[Self, None]:
        if self._entered:
            raise RuntimeError(f"TaskGroup {self!r} has been already entered")
        self._entered = True

        if self._loop is None:
            self._loop = events.get_running_loop()

        async with _install.install_uncancel():
            self._parent_task = tasks.current_task(self._loop)
            if self._parent_task is None:
                raise RuntimeError(
                    f"TaskGroup {self!r} cannot determine the parent task"
                )

            try:
                yield self
            finally:
                et, exc, _ = sys.exc_info()
                self._exiting = True
                propagate_cancellation_error = (
                    exc if et is exceptions.CancelledError else None
                )

            if self._parent_cancel_requested:
                # If this flag is set we *must* call uncancel().
                if self._parent_task.uncancel() == 0:
                    # If there are no pending cancellations left,
                    # don't propagate CancelledError.
                    propagate_cancellation_error = None

                if et is not None:
                    if not self._aborting:
                        # Our parent task is being cancelled:
                        #
                        #    async with TaskGroup() as g:
                        #        g.create_task(...)
                        #        await ...  # <- CancelledError
                        #
                        # or there's an exception in "async with":
                        #
                        #    async with TaskGroup() as g:
                        #        g.create_task(...)
                        #        1 / 0
                        #
                        self._abort()

                # We use while-loop here because "self._on_completed_fut"
                # can be cancelled multiple times if our parent task
                # is being cancelled repeatedly (or even once, when
                # our own cancellation is already in progress)
                while self._tasks:
                    if self._on_completed_fut is None:
                        self._on_completed_fut = self._loop.create_future()

                    try:
                        await self._on_completed_fut
                    except exceptions.CancelledError as ex:
                        if not self._aborting:
                            # Our parent task is being cancelled:
                            #
                            #    async def wrapper():
                            #        async with TaskGroup() as g:
                            #            g.create_task(foo)
                            #
                            # "wrapper" is being cancelled while "foo" is
                            # still running.
                            propagate_cancellation_error = ex
                            self._abort()

                    self._on_completed_fut = None

                assert not self._tasks

                if self._base_error is not None:
                    raise self._base_error

                # Propagate CancelledError if there is one, except if there
                # are other errors -- those have priority.
                if propagate_cancellation_error and not self._errors:
                    # The wrapping task was cancelled; since we're done with
                    # closing all child tasks, just propagate the cancellation
                    # request now.
                    raise propagate_cancellation_error

                if et is not None and et is not exceptions.CancelledError:
                    assert self._errors is not None
                    self._errors.append(exc)

                if self._errors:
                    # Exceptions are heavy objects that can have object
                    # cycles (bad for GC); let's not keep a reference to
                    # a bunch of them.
                    errors = self._errors
                    self._errors = None

                    me = BaseExceptionGroup("unhandled errors in a TaskGroup", errors)
                    raise me from None

    async def __aenter__(self) -> Self:
        return await self._cmgr.__aenter__()

    async def __aexit__(self, *exc_info) -> bool | None:
        return await self._cmgr.__aexit__(*exc_info)  # type: ignore

    def create_task(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        name: str | None = None,
        context: Context | None = None,
    ) -> Task[_T]:
        if not self._entered:
            raise RuntimeError(f"TaskGroup {self!r} has not been entered")
        if self._exiting and not self._tasks:
            raise RuntimeError(f"TaskGroup {self!r} is finished")
        if self._aborting:
            raise RuntimeError(f"TaskGroup {self!r} is shutting down")
        assert self._loop is not None
        if context is None:
            task = _task_factory(self._loop, coro)
        else:
            task = _task_factory(self._loop, coro, context=context)
        tasks._set_task_name(task, name)  # type: ignore
        # optimization: Immediately call the done callback if the task is
        # already done (e.g. if the coro was able to complete eagerly),
        # and skip scheduling a done callback
        if task.done():
            self._on_task_done(task)
        else:
            self._tasks.add(task)
            task.add_done_callback(self._on_task_done)
        return task

    # Since Python 3.8 Tasks propagate all exceptions correctly,
    # except for KeyboardInterrupt and SystemExit which are
    # still considered special.

    def _is_base_error(self, exc: BaseException) -> bool:
        assert isinstance(exc, BaseException)
        return isinstance(exc, (SystemExit, KeyboardInterrupt))

    def _abort(self) -> None:
        self._aborting = True

        for t in self._tasks:
            if not t.done():
                t.cancel()

    def _on_task_done(self, task):
        self._tasks.discard(task)

        if self._on_completed_fut is not None and not self._tasks:
            if not self._on_completed_fut.done():
                self._on_completed_fut.set_result(True)

        if task.cancelled():
            return

        exc = task.exception()
        if exc is None:
            return

        assert self._errors is not None
        self._errors.append(exc)
        if self._is_base_error(exc) and self._base_error is None:
            self._base_error = exc

        assert self._parent_task is not None
        assert self._loop is not None
        if self._parent_task.done():
            # Not sure if this case is possible, but we want to handle
            # it anyways.
            self._loop.call_exception_handler(
                {
                    "message": f"Task {task!r} has errored out but its parent "
                    f"task {self._parent_task} is already completed",
                    "exception": exc,
                    "task": task,
                }
            )
            return

        if not self._aborting and not self._parent_cancel_requested:
            # If parent task *is not* being cancelled, it means that we want
            # to manually cancel it to abort whatever is being run right now
            # in the TaskGroup.  But we want to mark parent task as
            # "not cancelled" later in __aexit__.  Example situation that
            # we need to handle:
            #
            #    async def foo():
            #        try:
            #            async with TaskGroup() as g:
            #                g.create_task(crash_soon())
            #                await something  # <- this needs to be canceled
            #                                 #    by the TaskGroup, e.g.
            #                                 #    foo() needs to be cancelled
            #        except Exception:
            #            # Ignore any exceptions raised in the TaskGroup
            #            pass
            #        await something_else     # this line has to be called
            #                                 # after TaskGroup is finished.
            self._abort()
            self._parent_cancel_requested = True
            self._parent_task.cancel()
