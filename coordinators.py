import asyncio
import asyncio.subprocess
import json
import logging
import os
import threading
import traceback
import uuid
import time
import hypercorn.config
from io import StringIO
from hypercorn.asyncio import serve
from contextlib import contextmanager
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from queue import Queue
from threading import Thread
from datetime import datetime
from typing import Any, Callable, Any, Dict, Generic, List, Literal, Optional, ParamSpec, Set, Tuple, TypeVar, TypedDict, Union, cast
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from interpreter import OpenInterpreter
from commands import OpenInterpreterCommand

from modifiers import IdModifier, TaskSetModifier
from runners import BenchmarkRunner, FakeBenchmarkRunner, Recorder
from task import LMC, LoadedTask, ResultStatus, TaskResult, TasksStore, ZeroShotTask


logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.DEBUG)
logger.addHandler(console_handler)


DO_NOTHING = lambda *args, **kwargs: None


Result = TypeVar("Result")
_P = ParamSpec("_P")


class TaskLifecycle(Generic[Result]):
    def __init__(self):
        self._start_fns: List[Callable[[], None]] = []
        self._done_fns: List[Callable[[Result], None]] = []
    
    def add_start_fn(self, fn: Callable[[], None]):
        self._start_fns.append(fn)
    
    def add_done_fn(self, fn: Callable[[Result], None]):
        self._done_fns.append(fn)
    
    def wrap(self, fn: Callable[_P, Result]) -> Callable[_P, Result]:
        def wrapped_fn(*args, **kwargs) -> Result:
            for sfn in self._start_fns: sfn()
            result = fn(*args, **kwargs)
            for dfn in self._done_fns: dfn(result)
            return result
        return wrapped_fn  # type: ignore
    

def run_and_judge(rnnr: BenchmarkRunner, lt: LoadedTask, cmd: OpenInterpreterCommand, write: Callable[[bytes], None], log: Callable[[str], None]) -> Tuple[List[LMC], ResultStatus]:
    messages = rnnr.run(lt, cmd, write, log)
    status = lt.to_result_status(messages)
    return messages, status

    # return rnnr.run_and_judge(lt, cmd, Recorder(log, write))

    # with prepare_runner(rnnr, lt, cmd, Recorder(log, write)) as (env, invoke):
    #     messages = invoke()
    #     status = lt.judge(env, messages)
    #     return messages, status

    # make_env, invoke = rnnr.prepare(lt, cmd, Recorder(log, write))
    # with make_env as env:
    #     messages = invoke()



def run_task(lt: LoadedTask, command: OpenInterpreterCommand, runner: BenchmarkRunner, log) -> TaskResult:
    zstask = lt.to_zero_shot()
    start = datetime.now()
    try:
        messages, status = run_and_judge(runner, lt, command, DO_NOTHING, log)
    except Exception as e:
        log(traceback.format_exc())
        status = "error"
        messages = []
    finally:
        end = datetime.now()
        return {
            "task_id": zstask["id"],
            "command": command,
            "prompt": zstask["prompt"],
            "start": start,
            "end": end,
            "messages": messages,
            "status": status
        }


def run_benchmark_worker_pool(benchmark: TasksStore, mod: TaskSetModifier, command: OpenInterpreterCommand, runner: BenchmarkRunner, n_workers: Optional[int] = None) -> List[TaskResult]:
    all_tasks = [benchmark.load_task(t) for t in mod.modify(benchmark.get_tasks())]
    task_results: List[TaskResult] = []

    extra_ci = benchmark.custom_instructions()
    if extra_ci is not None and "custom_instructions" in command:
        command["custom_instructions"] += f"\n{extra_ci}"

    actual_n_workers = n_workers or os.cpu_count()
    with ThreadPoolExecutor(max_workers=actual_n_workers) as pool:
        logger.debug(f"Running {len(all_tasks)} tasks across {actual_n_workers} threads...")
        zero_shots = [(lt, lt.to_zero_shot()) for lt in all_tasks]

        def make_fs(id: str):
            def start():
                logger.debug(f"  task {id}: RUNNING...")
            def log(s: str):
                logger.debug(f"  task {id} log: {s}")
            def done(r: TaskResult):
                logger.debug(f"  task {r['task_id']}: {r['status']}!")
            return start, log, done

        run_task_args = [(lt, command, runner, make_fs(zs["id"])) for lt, zs in zero_shots]
        apps = []
        for args in run_task_args:
            tlc = TaskLifecycle[TaskResult]()
            start, log, done = make_fs(args[0].to_zero_shot()['id'])
            tlc.add_start_fn(start)
            tlc.add_done_fn(done)
            apps.append((tlc.wrap(run_task), (*args[:-1], log)))
        futures = [pool.submit(fn, *args) for fn, args in apps]
        
        for f in as_completed(futures):
            task_results.append(f.result())

        logger.debug(f"Done!")
    
    return task_results


def judge_result(initial_prompt: str, last_msg: str, expected: str) -> ResultStatus:
    judge = OpenInterpreter()
    judge.llm.model = "gpt-4"
    judge.llm.context_window = 128000  # type: ignore

    judge.system_message = "You are a grading AI. Answer with the single word 'correct' or 'incorrect', and do NOT answer in markdown."
    q = f"""
    
# # # QUESTION:
# # {initial_prompt}
# # # CORRECT ANSWER:
# # {expected}
# # ---
# # # STUDENT'S ANSWER:
# # {last_msg}
# # ---

# # Did the student get the answer correct?

# #     """.strip()
    
    try:
        judge_msgs = cast(List[LMC], judge.chat(q, display=False))
        assert len(judge_msgs) > 0, "the judge is speechless!"

        judge_result = judge_msgs[0]["content"].strip().lower()
        assert judge_result in {"correct", "incorrect", "unknown", "error"}, f"the judge's response was unexpected! response: {judge_result}"
    finally:
        judge.computer.terminate()

    return judge_result  # type: ignore


class WebSocketsManager:
    def __init__(self):
        self._lock = threading.Lock()

        # read and modified by multiple threads.
        self._history: List[bytes] = []

        # read and added to by multiple coroutines, threads.
        self._websockets: Set[WebSocket] = set()
        # read and modified by multiple coroutines, threads.
        self._disconnect_events: Dict[WebSocket, asyncio.Event] = {}
        # read and modified by multiple threads.
        self._is_closed = False

    def _is_connected(self, ws: WebSocket):
        return ws in self._websockets

    async def add(self, ws: WebSocket):
        with self._lock:
            self._websockets.add(ws)
            self._disconnect_events[ws] = asyncio.Event()
            for bs in self._history:
                await self._send_bytes_to(ws, bs)

    def _remove(self, ws: WebSocket):
        """
        Assumes we're inside self._ws_lock's critical section.
        """
        self._websockets.remove(ws)
        self._disconnect_events[ws].set()
        del self._disconnect_events[ws]
    
    def is_closed(self):
        return self._is_closed
    
    def close(self):
        with self._lock:
            for ws in self._websockets:
                self._disconnect_events[ws].set()
            self._is_closed = True
            self._websockets.clear()
    
    async def wait_until_disconnect(self, websocket: WebSocket):
        await self._disconnect_events[websocket].wait()
    
    async def write(self, bs: bytes):
        with self._lock:
            self._history.append(bs)
        
            if len(self._websockets) == 0:
                return

            # the actual broadcast portion.
            to_remove = set()
            for ws in self._websockets:
                should_remove = await self._send_bytes_to(ws, bs)
                if should_remove:
                    to_remove.add(ws)
            for ws in to_remove:
                self._remove(ws)
    
    async def write_json(self, j: Any):
        await self.write(json.dumps(j).encode("utf-8"))

    async def _send_bytes_to(self, ws: WebSocket, b: bytes) -> bool:
        """
        Returns True if the websocket has been disconnected from.
        Returns False otherwise.

        Assumes this thread has access to self._lock.
        """
        try:
            await ws.send_bytes(b)
            return False
        except (WebSocketDisconnect, RuntimeError):
            return True


@contextmanager
def run_background_server(app: FastAPI):
    loop = asyncio.new_event_loop()
    shutdown_event = asyncio.Event()

    def _start_server():
        c = hypercorn.config.Config()
        coroutine = serve(app, c, shutdown_trigger=shutdown_event.wait)  # type: ignore
        loop.run_until_complete(coroutine)  # type: ignore

    th = threading.Thread(target=_start_server)
    th.start()
    try:
        yield
    finally:
        loop.call_soon_threadsafe(shutdown_event.set)
        logger.debug("about to join threads -- this may take a few seconds...")
        th.join()
        logger.debug("joined!")


class TaskStartedPayload(TypedDict):
    tag: Literal["started"]


class TaskDonePayload(TypedDict):
    tag: Literal["done"]
    status: ResultStatus


class TaskLogPayload(TypedDict):
    tag: Literal["log"]
    message: str


TaskUpdatePayload = Union[
    TaskStartedPayload,
    TaskDonePayload,
    TaskLogPayload
]


class TaskUpdate(TypedDict):
    task_id: str
    payload: TaskUpdatePayload


def run_benchmark_worker_pool_with_server(
        tasks: TasksStore,
        mod: TaskSetModifier,
        cmd: OpenInterpreterCommand,
        rnnr: BenchmarkRunner,
        nworkers: int | None = None
    ) -> List[TaskResult]:

    app = FastAPI()
    templates = Jinja2Templates("templates")

    all_tasks = [tasks.load_task(t) for t in mod.modify(tasks.get_tasks())]
    results_lock = threading.Lock()
    task_results: List[TaskResult] = []
    zs_tasks = [(t, t.to_zero_shot()) for t in all_tasks]
    zs_map = {zst["id"]: zst for _, zst in zs_tasks}
    task_managers = {zst["id"]: WebSocketsManager() for _, zst in zs_tasks}
    updates_manager = WebSocketsManager()

    extra_ci = tasks.custom_instructions()
    if extra_ci is not None and "custom_instructions" in cmd:
        cmd["custom_instructions"] += f"\n{extra_ci}"

    @app.get("/view/{task_id}", response_class=HTMLResponse)
    async def view(request: Request, task_id: str):
        if task_id not in zs_map:
            raise HTTPException(status_code=404, detail=f"no task with id '{task_id}'!")
        prompt = zs_map[task_id]["prompt"]
        return templates.TemplateResponse(
            request,
            name="logs.html.j2",
            context={"task_id": task_id, "prompt": prompt, "command": json.dumps(cmd, indent=2)})

    @app.websocket("/logs/{task_id}")
    async def logs(websocket: WebSocket, task_id: str):
        await websocket.accept()
        await task_managers[task_id].add(websocket)
        await task_managers[task_id].wait_until_disconnect(websocket)
        # await websocket.close()
    
    @app.post("/stop/{task_id}")
    async def stop(task_id: str) -> bool:
        task_managers[task_id].close()
        return True

    @app.get("/", response_class=HTMLResponse)
    async def full(request: Request):
        return templates.TemplateResponse(
            request,
            name="full.html.j2",
            context={"tasks": [zs["id"] for _, zs in zs_tasks]}
        )

    @app.websocket("/updates")
    async def updates(websocket: WebSocket):
        await websocket.accept()
        await updates_manager.add(websocket)
        await updates_manager.wait_until_disconnect(websocket)
        # await websocket.close()
   
    def run_task(lt: LoadedTask, zs: ZeroShotTask, ws_manager: WebSocketsManager, log: Callable[[str], None]) -> TaskResult:
        def write(b: bytes):
            asyncio.run(ws_manager.write(b))
        
        start = datetime.now()
        try:
            messages, status = run_and_judge(rnnr, lt, cmd, write, log)
        except Exception as e:
            log(traceback.format_exc())
            status = "error"
            messages = []
        finally:
            end = datetime.now()
            return {
                "task_id": zs["id"],
                "command": cmd,
                "prompt": zs["prompt"],
                "start": start,
                "end": end,
                "messages": messages,
                "status": status
            }
    
    with run_background_server(app), ThreadPoolExecutor(max_workers=nworkers) as pool:
        done_event = threading.Event()

        def make_update_fns(id: str):
            def start():
                asyncio.run(updates_manager.write_json({"task_id": id, "payload": {"tag": "started"}}))

            def log(s: str):
                asyncio.run(updates_manager.write_json({"task_id": id, "payload": {"tag": "log", "message": s}}))

            def done(result: TaskResult):
                asyncio.run(updates_manager.write_json({"task_id": id, "payload": {"tag": "done", "status": result["status"]}}))

                with results_lock:
                    task_results.append(result)
                task_managers[id].close()
                if len(task_results) >= len(all_tasks):
                    done_event.set()

            return start, log, done

        futures: List[Future[TaskResult]] = []
        for lt, zs in zs_tasks:
            start, log, done = make_update_fns(zs["id"])
            tlc = TaskLifecycle[TaskResult]()
            tlc.add_start_fn(start)
            tlc.add_done_fn(done)
            args = (lt, zs, task_managers[zs["id"]], log)
            futures.append(pool.submit(tlc.wrap(run_task), *args))

        done_event.wait()
        # deal with the futures to surface any exceptions here.
        for f in as_completed(futures):
            # the result was already recorded, so just call the thing.
            f.result()

        for manager in task_managers.values():
            manager.close()
        updates_manager.close()

        logger.debug("Hold CTRL+C to close the server.")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            ...

    return task_results


@dataclass
class OIBenchmarks:
    tasks: TasksStore
    command: OpenInterpreterCommand
    runner: BenchmarkRunner = field(default_factory=FakeBenchmarkRunner)
    modifier: TaskSetModifier = field(default_factory=IdModifier)
    nworkers: Optional[int] = None
    server: bool = False

    def run(self) -> List[TaskResult]:
        if self.server:
            results = run_benchmark_worker_pool_with_server(self.tasks, self.modifier, self.command, self.runner, self.nworkers)
        else:
            results = run_benchmark_worker_pool(self.tasks, self.modifier, self.command, self.runner, self.nworkers)
        return results