"""Watch a JSON task CLI and deliver its final result through the host Bot.

This is the Hermes process-watcher pattern adapted to VikingBot's sandbox API.
The external runner owns processes, timeouts, log limits and exact session resume.
We own only origin binding, bounded polling, an outbox and queued user additions.
Never use the short-lived launcher's PID as evidence that the task has finished.
"""

import asyncio
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import time
import uuid
from pathlib import Path

from loguru import logger

from vikingbot.agent.tools.base import Tool, ToolContext
from vikingbot.bus.events import InboundMessage, OutboundMessage
from vikingbot.config.schema import SandboxBackend, SessionKey
from vikingbot.extensions.background_tasks.segments import numbered_parts
from vikingbot.extensions.runtime import RuntimeContext

ACTIVE = {"starting", "running", "stopping"}
TERMINAL = {"succeeded", "failed", "stopped", "timed_out", "log_limit", "interrupted"}
# Channel-owned delivery fields only. In particular, never persist a connection,
# authentication material or a user-provided 'source' as trusted runtime context.
DELIVERY_KEYS = {
    "reply_to", "message_id", "reply_to_message_id", "chat_type", "chat_mode",
    "root_id", "sender_id",
}
SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
MAX_JSON = 1024 * 1024
PAGE_BYTES = 65536
RESULT_LIMIT = 64 * 1024 * 1024


class BackgroundTaskError(ValueError):
    """Safe local error text; never use raw backend exceptions as model output."""


class BackgroundTasks:
    """One instance per gateway. Journal writes are atomic and owner-private.

    Journal storage must be on a persistent host workspace. One gateway owns a
    journal; an advisory lock rejects accidental concurrent gateway consumers.
    Sandbox teardown may kill an external job. Restart only reconciles status;
    it never relaunches or resumes a job (which could repeat external writes).
    """

    def __init__(self, host: RuntimeContext, options: dict):
        import fcntl  # macOS/Linux, same platform contract as the external runner

        self.host = host
        if host.sandbox_manager is None:
            raise BackgroundTaskError("background_tasks requires a sandbox manager")
        if (host.config.sandbox.backend == SandboxBackend.DIRECT
                and not host.config.sandbox.backends.direct.allow_exec):
            raise BackgroundTaskError("background_tasks requires direct.allow_exec or a sandbox backend")
        self.interval = self._finite(options.get("poll_interval", 5), 0.05, 300)
        self.retry_interval = self._finite(options.get("delivery_retry_seconds", 30), 0.05, 3600)
        self.command_timeout = self._finite(options.get("command_timeout", 20), 1, 60)
        self.max_errors = int(options.get("max_probe_errors", 5))
        self.max_attempts = int(options.get("max_delivery_attempts", 5))
        self.max_tasks = int(options.get("max_tasks", 100))
        self.segment_bytes = int(options.get("segment_bytes", 4096))
        if not 1024 <= self.segment_bytes <= 8192:
            raise BackgroundTaskError("segment_bytes must be 1024..8192")
        if not (1 <= self.max_errors <= 100 and 1 <= self.max_attempts <= 100
                and 1 <= self.max_tasks <= 10000):
            raise BackgroundTaskError("Invalid background_tasks limits")
        self.directory = Path(options.get(
            "state_dir", host.workspace / "tmp" / "wangoon" / "background_tasks"
        )).expanduser().absolute()
        if self.directory.is_symlink():
            raise BackgroundTaskError("Background journal cannot be a symlink")
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BackgroundTaskError("Background journal must be owned by this user with mode 0700")
        self._lock_fd = None
        self._fcntl = fcntl
        self.items = {}
        self.locks = {}
        self.inflight = set()
        self.running = False
        self.worker = None
        self.wakeup = asyncio.Event()
        self._task_wakeups = {}
        # A remote receipt whose journal commit failed stays in memory until a
        # bounded persistence retry succeeds. It must not advance the cursor or
        # become an ordinary send timeout while the commit is pending.
        self._pending_receipts = {}
        self.semaphore = asyncio.Semaphore(4)
        if host.tools.has("background_task"):
            raise BackgroundTaskError("background_task is already registered")
        host.tools.register(BackgroundTaskTool(self))

    @staticmethod
    def _finite(value, minimum, maximum):
        value = float(value)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise BackgroundTaskError("Invalid finite background_tasks interval")
        return value

    def _save(self, item):
        """Persist before enqueue/send. No raw tool output or credentials here."""
        path = self.directory / (item["id"] + ".json")
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                json.dump(item, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _update(self, item, **changes):
        """Commit a candidate before exposing a successful state transition.

        Callers replacing mutable fields must pass new containers. In-place
        append/clear would also change the live state when persistence fails.
        The original dict identity stays stable for queued events and receipts.
        """
        proposed = {**item, **changes}
        self._save(proposed)
        item.update(proposed)

    async def start(self):
        """Acquire ownership and reload pending results, without replaying work."""
        if self.running:
            return
        descriptor = os.open(self.directory / ".owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            self._fcntl.flock(descriptor, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
            items = {}
            for path in self.directory.glob("*.json"):
                if path.is_symlink() or path.stat().st_size > 2 * MAX_JSON:
                    raise BackgroundTaskError("Invalid background journal entry")
                item = json.loads(path.read_text())
                if path.stem != item["id"] or not re.fullmatch(r"[a-f0-9]{64}", item["id"]):
                    raise BackgroundTaskError("Invalid background journal identity")
                SessionKey.model_validate(item["session"])
                # Older journals contained one inline result. Convert lazily on
                # the first tick, preserving delivery/continuation decisions.
                item.setdefault("generation", 0)
                item.setdefault("part_count", 0)
                item.setdefault("next_part", 0)
                item.setdefault("delivery_epoch", None)
                item.setdefault("delivery_error", None)
                item.setdefault("watch_error", None)
                items[item["id"]] = item
            self.items = items
        except BaseException:
            os.close(descriptor)
            raise
        self._lock_fd = descriptor
        self.running = True
        self.worker = asyncio.create_task(self._watch_loop(), name="background-task-watcher")

    async def close(self):
        """Stop polling before sandbox teardown; never silently stop/resume jobs."""
        self.running = False
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        self.inflight.clear()
        self._pending_receipts.clear()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _key(self, item):
        return SessionKey.model_validate(item["session"])

    async def _invoke(self, session, argv, probe_id, action):
        """Run argv through the original sandbox; shell-quote every argument.

        The sandbox API accepts a command string, unlike the runner's Popen API.
        shlex.join preserves spaces/metacharacters as literals. Never execute a
        host subprocess to bypass a remote sandbox or disabled exec policy.

        execute() formats/truncates terminal output (Direct caps it at 10000
        characters). Transport JSON through a bounded workspace file instead;
        otherwise a valid long final answer would become a false probe failure.
        Fixed per-watch/action files are overwritten, not accumulated per poll.
        """
        sandbox = await self.host.sandbox_manager.get_sandbox(session)
        path = f"tmp/wangoon/background_tasks/probes/{probe_id}/{action}.json"
        nonce = uuid.uuid4().hex
        # POSIX shell setup is static; every variable part remains shell-quoted.
        # Do not depend on a remote file API creating missing parent directories.
        command = (
            "umask 077 && mkdir -p -- " + shlex.quote(str(Path(path).parent))
            + " && " + shlex.join(argv) + " > " + shlex.quote(path)
            + " && printf '%s' " + shlex.quote(nonce)
        )
        async with self.semaphore:
            acknowledgement = await asyncio.wait_for(
                sandbox.execute(command, timeout=self.command_timeout),
                timeout=self.command_timeout + 2,
            )
            # The marker is produced only after exit 0, and changes per call.
            # A failed redirection/mkdir must never let an older probe file
            # masquerade as fresh evidence. Stderr/warnings also fail closed.
            if not isinstance(acknowledgement, str) or acknowledgement.strip() != nonce:
                raise BackgroundTaskError("Runner probe command was not acknowledged")
            raw = await sandbox.read_file_bytes(path, max_bytes=MAX_JSON)
        if len(raw) > MAX_JSON:
            raise BackgroundTaskError("Runner output exceeds the JSON bound")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise BackgroundTaskError("Runner output must be a JSON object")
        return result

    async def _call(self, item, action, offset=0):
        """Require the saved task/run pair, including after restart or resume."""
        argv = [*item["runner"], action, item["task_id"]]
        if action == "stop":
            # Identity must be checked by the runner BEFORE it signals a
            # process. Checking the reply after stop is already too late.
            argv += ["--expected-run-id", item["run_id"]]
        else:
            argv += ["--run-id", item["run_id"]]
        if action == "logs":
            argv += ["--lines", "100", "--max-bytes", "65536"]
        elif action == "result":
            argv += ["--format", "page", "--offset", str(offset), "--max-bytes", str(PAGE_BYTES)]
        # Warnings, exit-code suffixes and malformed JSON are probe failures,
        # never evidence of task success. Do not echo potentially secret output.
        result = await asyncio.wait_for(
            self._invoke(self._key(item), argv, item["id"], action), self.command_timeout + 5,
        )
        if result.get("task_id") != item["task_id"]:
            raise BackgroundTaskError("Runner task identity mismatch")
        if result.get("run_id") != item["run_id"]:
            raise BackgroundTaskError("Runner run changed; register the new run explicitly")
        if result.get("protocol_version", 0) < 2:
            raise BackgroundTaskError("Runner protocol v2 required; upgrade runner before watching")
        return result

    async def watch(self, context, task_id, runner):
        """Bind the actual task/run to trusted current-channel routing once."""
        if not self.running:
            raise BackgroundTaskError("Watcher requires a live gateway; use runner status/result in one-shot mode")
        if context.session_key is None or not context.sender_id:
            raise BackgroundTaskError("Missing trusted session context")
        if not isinstance(task_id, str) or not SAFE_ID.fullmatch(task_id):
            raise BackgroundTaskError("Invalid task_id")
        if (not isinstance(runner, list) or not 1 <= len(runner) <= 32
                or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or "\0" in arg for arg in runner)):
            raise BackgroundTaskError("runner must be a bounded argv prefix, without credentials")
        # Discover run_id using the same sandbox as every subsequent probe.
        seed = hashlib.sha256((context.session_key.model_dump_json() + task_id).encode()).hexdigest()
        status = await asyncio.wait_for(
            self._invoke(context.session_key, [*runner, "status", task_id], seed, "status"),
            self.command_timeout + 5,
        )
        if status.get("protocol_version", 0) < 2:
            raise BackgroundTaskError("Runner protocol v2 required; upgrade runner before watching")
        if (not isinstance(status, dict) or status.get("task_id") != task_id
                or not isinstance(status.get("run_id"), str)
                or not SAFE_ID.fullmatch(status["run_id"])
                or status.get("state") not in ACTIVE | TERMINAL):
            raise BackgroundTaskError("Runner did not identify a supported task/run")
        identity = json.dumps([context.session_key.model_dump(), task_id, status["run_id"]], sort_keys=True)
        key = hashlib.sha256(identity.encode()).hexdigest()
        if key in self.items:
            return self.view(self.owned(context, key))
        # A task registered by another chat cannot be rebound or controlled.
        if any(i["task_id"] == task_id and i["session"] != context.session_key.model_dump()
               for i in self.items.values()):
            raise BackgroundTaskError("Task is bound to a different session")
        if len(self.items) >= self.max_tasks:
            raise BackgroundTaskError("Watcher journal is full; forget acknowledged finished tasks")
        metadata = {key: value for key, value in (context.channel_metadata or {}).items()
                    if key in DELIVERY_KEYS and isinstance(value, (str, int, bool))}
        item = {
            "id": key, "task_id": task_id, "run_id": status["run_id"], "runner": runner,
            "session": context.session_key.model_dump(), "sender_id": context.sender_id,
            "actor_peer_id": context.actor_peer_id, "metadata": metadata,
            "state": status["state"], "probe_errors": 0, "paused": False,
            "delivery": "pending", "attempts": 0, "next_delivery": 0,
            "text": None, "history_recorded": False, "queued": [],
            "continuation": "pending", "receipt_deadline": 0, "history_revision": 0,
            "generation": 0, "part_count": 0, "next_part": 0, "delivery_epoch": None,
            "delivery_error": None, "watch_error": None,
        }
        self._save(item)
        self.items[key] = item
        self._wake(item)
        return self.view(item)

    def owned(self, context, watch_id):
        item = self.items.get(watch_id)
        if item is None or item["session"] != context.session_key.model_dump():
            raise BackgroundTaskError("Unknown watch in this session")
        if (item.get("actor_peer_id") and item["actor_peer_id"] != context.actor_peer_id
                or not item.get("actor_peer_id") and item["sender_id"] != context.sender_id):
            raise BackgroundTaskError("Task controls belong to the originating user")
        return item

    @staticmethod
    def view(item):
        return {key: item[key] for key in (
            "id", "task_id", "run_id", "state", "paused", "delivery", "attempts", "queued", "continuation",
            "part_count", "next_part", "delivery_error", "watch_error",
        )}

    def _wake(self, item):
        """Wake this watch only; the supervisor also discovers new watches."""
        self._task_wakeups.setdefault(item["id"], asyncio.Event()).set()
        self.wakeup.set()

    def _pause_watch(self, item, error):
        """Fail one monitor closed; leave the gateway and recovery tools alive.

        This is an error marker, not a successful operation. If the journal is
        unavailable it can only live in memory; restart uses the last durable
        state. Preserve delivery cursors and any outstanding receipt so retry
        cannot resend confirmed parts or discard a valid acknowledgement.
        Call while holding the watch lock (or from a serialized test).
        """
        proposed = {**item, "watch_error": "watch_failed"}
        try:
            self._save(proposed)
        except Exception:
            logger.error("Background watch failure marker could not be saved for {}", item["id"])
        item.update(proposed)
        logger.error("Background watch paused for {} ({})", item["id"], type(error).__name__)
        self._wake(item)

    async def _watch_loop(self):
        """Supervise independent watch loops without a batch completion barrier.

        Each loop owns its poll interval and part wakeup. Slow status/result I/O
        cannot hold back another watch's confirmed next part, and delivery
        wakeups never re-poll unrelated running jobs. The shared semaphore still
        bounds sandbox commands; main-queue delivery still serializes history.
        """
        workers = {}
        try:
            while True:
                self.wakeup.clear()
                for key, worker in list(workers.items()):
                    if worker.done():
                        del workers[key]
                        self._task_wakeups.pop(key, None)
                        try:
                            worker.result()
                        except (Exception, asyncio.CancelledError) as error:
                            # Journal/spool faults belong to one watch. Do not
                            # shut down unrelated jobs or disable list/retry.
                            item = self.items.get(key)
                            if item is not None:
                                async with self.locks.setdefault(key, asyncio.Lock()):
                                    if self.items.get(key) is item:
                                        self._pause_watch(item, error)
                for key, item in list(self.items.items()):
                    if key not in workers and not item.get("watch_error"):
                        worker = asyncio.create_task(self._watch_item(item), name="background-watch-" + key)
                        worker.add_done_callback(lambda _: self.wakeup.set())
                        workers[key] = worker
                await self.wakeup.wait()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.running = False
            logger.error("Background watcher stopped ({})", type(error).__name__)
        finally:
            # Cancellation of the supervisor must reclaim every pending probe
            # before the journal lock or sandbox is released, including startup
            # failures and tests that drive ticks manually after cancelling it.
            for worker in workers.values():
                worker.cancel()
            await asyncio.gather(*workers.values(), return_exceptions=True)
            self._task_wakeups.clear()

    async def _watch_item(self, item):
        wakeup = self._task_wakeups.setdefault(item["id"], asyncio.Event())
        while self.items.get(item["id"]) is item and not item.get("watch_error"):
            wakeup.clear()
            await self.tick(item)
            timeout = self.interval
            pending = self._pending_receipts.get(item["id"])
            due = pending["next_retry"] if pending else 0
            if not due and item["delivery"] == "pending":
                due = item["receipt_deadline"]
                if not due and item["id"] not in self.inflight:
                    due = item["next_delivery"]
            if due:
                timeout = min(timeout, max(0.01, due - time.time()))
            try:
                await asyncio.wait_for(wakeup.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    def _commit_receipt(self, item, pending):
        """Persist a proposed receipt before publishing its in-memory cursor.

        Disk failures retain the last durable state and the original part/epoch.
        Retry the acknowledgement (not the message) at most max_attempts times.
        Exhaustion marks the live delivery failed and releases inflight so an
        explicit retry can resend the still-unconfirmed part after storage repair.
        A restart before commit may repeat only that part, as with a lost receipt.
        """
        proposed = dict(item)
        proposed.update(delivery_epoch=None, receipt_deadline=0, delivery_error=None,
                        next_delivery=time.time() + (0 if pending["success"] else self.retry_interval))
        if pending["success"]:
            proposed["next_part"] += 1
            proposed["attempts"] = 0
            if proposed["next_part"] == proposed["part_count"]:
                proposed["delivery"] = "sent"
        elif proposed["attempts"] >= self.max_attempts:
            proposed["delivery"] = "failed"
        try:
            self._save(proposed)
        except Exception as error:
            pending["errors"] += 1
            pending["next_retry"] = time.time() + self.retry_interval
            if pending["errors"] >= self.max_attempts:
                # Storage is unavailable, so do not pretend this failure marker
                # is durable. The saved cursor still identifies the unconfirmed
                # part; restart uses its persisted send-attempt budget.
                item.update(delivery="failed", delivery_error="receipt_journal_failed",
                            delivery_epoch=None, receipt_deadline=0)
                self._pending_receipts.pop(item["id"], None)
                self.inflight.discard(item["id"])
            else:
                self._pending_receipts[item["id"]] = pending
            logger.error("Background receipt commit failed for {} (attempt {}, {})",
                         item["id"], pending["errors"], type(error).__name__)
        else:
            item.update(proposed)
            self._pending_receipts.pop(item["id"], None)
            self.inflight.discard(item["id"])
        self._wake(item)

    async def tick(self, item):
        lock = self.locks.setdefault(item["id"], asyncio.Lock())
        async with lock:
            if self.items.get(item["id"]) is not item or item.get("watch_error"):
                return
            pending = self._pending_receipts.get(item["id"])
            if pending is not None:
                # Persist the known receipt before any timeout/re-send path.
                # Build the proposal from the latest item so concurrent stop or
                # queue changes are preserved, not overwritten by an old copy.
                if time.time() >= pending["next_retry"]:
                    self._commit_receipt(item, pending)
                return
            if item.get("receipt_deadline", 0) and time.time() >= item["receipt_deadline"]:
                self._update(item, receipt_deadline=0, delivery_epoch=None,
                             delivery="failed" if item["attempts"] >= self.max_attempts else item["delivery"])
                self.inflight.discard(item["id"])
            status = {}
            try:
                if item["text"] is None and not item["paused"]:
                    status = await self._call(item, "status")
                    if status.get("state") not in ACTIVE | TERMINAL:
                        raise BackgroundTaskError("Unknown runner state")
                    if status["state"] in TERMINAL:
                        proposed = {**item, "state": status["state"], "probe_errors": 0}
                        # _fetch_result commits its spool references. Publish
                        # that state only after the complete result is saved.
                        await self._fetch_result(proposed)
                        item.update(proposed)
                    else:
                        self._update(item, state=status["state"], probe_errors=0)
            except Exception:
                changes = {"probe_errors": item["probe_errors"] + 1}
                if status.get("state") in ACTIVE | TERMINAL:
                    changes["state"] = status["state"]
                if changes["probe_errors"] >= self.max_errors:
                    changes["paused"] = True
                    changes["text"] = (
                        "任务已结束，但最终答复连续读取失败，已暂停自动监测。"
                        if status.get("state") in TERMINAL or item["state"] in TERMINAL else
                        "任务的状态连续读取失败，已暂停自动监测。任务可能仍在运行。"
                    ) + "未自动重启。请检查执行环境后用 background_task retry 恢复监测。"
                self._update(item, **changes)
                logger.warning("Background task probe failed for {} (attempt {})",
                               item["id"], item["probe_errors"])
            if item["text"] is not None and not item["part_count"]:
                # Includes migration from the old one-message journal format.
                prefix = f"后台任务 {item['task_id']} / {item['run_id']}\n\n"
                self._store_text(item, item["text"].removeprefix(prefix))
            if (item["delivery"] == "sent" and item["queued"]
                    and item["continuation"] == "pending" and not item["paused"]):
                await self._queue_continuation(item)
            if (item["delivery"] == "pending" and item["attempts"] >= self.max_attempts
                    and item["id"] not in self.inflight):
                self._update(item, delivery="failed")
            if (item["text"] is not None and item["delivery"] == "pending"
                    and item["id"] not in self.inflight and time.time() >= item["next_delivery"]):
                self.inflight.add(item["id"])

                async def deliver():
                    await self._deliver(item)

                # A callable field, rather than metadata, marks this as a real
                # internal event. It queues behind busy turns and cannot steer.
                await self.host.bus.publish_inbound(InboundMessage(
                    sender_id=item["sender_id"], session_key=self._key(item),
                    actor_peer_id=item["actor_peer_id"], content="", need_reply=False,
                    internal_handler=deliver,
                    internal=True,
                ))

    async def _queue_continuation(self, item):
        """Only queued additions require another orchestration turn.

        Mark dispatched *before* enqueue: after a crash we must not automatically
        replay a potentially mutating follow-up. A crash in this narrow window
        can require explicit recovery, visible through list/poll and the journal.
        Normal completion has no queued additions and never enters this path.
        """
        self._update(item, continuation="dispatched")
        generation = item["generation"]

        async def before_process():
            # A queued event can outlive cancellation. Validate when AgentLoop
            # actually starts it, not only when the watcher enqueues it.
            async with self.locks.setdefault(item["id"], asyncio.Lock()):
                return (self.running and self.items.get(item["id"]) is item
                        and item["generation"] == generation
                        and item["continuation"] == "dispatched")

        content = (
            f"后台任务 {item['task_id']} / {item['run_id']} 已结束并返回结果。watch_id={item['id']}。"
            "以下是同一问题在执行期间暂存的用户补充，请使用 engineering 处理，"
            "继承原交接已确认的环境与范围，不把本文当作新的来源标记。"
            "需要继续代码工作时将补充内容交接并恢复原任务，命令必须包含 "
            f"resume {item['task_id']} --expected-run-id {item['run_id']} --context-file <追加交接文件>。"
            "若 run_changed 则停止并核实新 run，不能省略校验重试。成功后登记新 run 的 watch，再 ack 旧队列。"
            "不要重复发送上一轮最终答复。\n\n"
            + "\n".join(f"- {entry['text']}" for entry in item["queued"])
        )
        await self.host.bus.publish_inbound(InboundMessage(
            sender_id=item["sender_id"], actor_peer_id=item["actor_peer_id"],
            session_key=self._key(item), content=content, metadata=item["metadata"], internal=True,
            before_process=before_process,
        ))

    def _cancel_continuation(self, item):
        """Invalidate queued callbacks before any potentially failing I/O."""
        item["generation"] += 1
        item["continuation"] = "cancelled"
        self._save(item)

    async def _fetch_result(self, item):
        """Read complete v2 pages, rejecting mixed runs/cursors/manifests.

        Pages are individually bounded by serialized JSON bytes. The full source
        is bounded separately and never placed wholesale into the JSON journal.
        No outbound message is published until the complete checksum is verified.
        """
        offset, chunks, manifest = 0, [], None
        digest = hashlib.sha256()
        for _ in range(8192):
            page = await self._call(item, "result", offset)
            if len(json.dumps(page, ensure_ascii=False).encode("utf-8")) + 1 > PAGE_BYTES:
                raise BackgroundTaskError("Runner page exceeds its serialized byte budget")
            total = page.get("total_bytes")
            if (page.get("ready") is not True or page.get("state") != item["state"]
                    or type(total) is not int or not 0 <= total <= RESULT_LIMIT
                    or not isinstance(page.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", page["sha256"])):
                raise BackgroundTaskError("Invalid terminal result manifest")
            current = {key: page.get(key) for key in (
                "total_bytes", "sha256", "outcome_status", "outcome_error", "source_truncated"
            )}
            if manifest is None:
                manifest = current
            elif current != manifest:
                raise BackgroundTaskError("Runner result manifest changed between pages")
            text = page.get("text")
            if not isinstance(text, str):
                raise BackgroundTaskError("Runner page text is invalid")
            encoded = text.encode("utf-8")
            next_offset = offset + len(encoded)
            if (type(page.get("offset")) is not int or page["offset"] != offset
                    or type(page.get("next_offset")) is not int or page["next_offset"] != next_offset
                    or next_offset > total or type(page.get("eof")) is not bool
                    or page["eof"] != (next_offset == total)
                    or (next_offset == offset and not page["eof"])):
                raise BackgroundTaskError("Runner page cursor is invalid")
            digest.update(encoded)
            chunks.append(text)
            offset = next_offset
            if page["eof"]:
                if digest.hexdigest() != manifest["sha256"]:
                    raise BackgroundTaskError("Runner result checksum mismatch")
                self._store_text(item, self._result_text(item, "".join(chunks), manifest))
                return
        raise BackgroundTaskError("Runner result exceeds the finite page limit")

    @staticmethod
    def _result_text(item, text, manifest):
        if not text.strip():
            text = "任务没有可展示的最终答复，请查看 result/logs。"
        if item["state"] != "succeeded":
            text = f"任务状态：{item['state']}（不能认定工作已完成）。\n\n" + text
        if manifest.get("source_truncated"):
            text += "\n\n原始答复超过 runner 读取上限，未完整展示，请检查任务输出文件。"
        if item["queued"]:
            text += "\n\n以下补充信息尚未执行" + (
                "，Bot 将另行交给 engineering 处理：\n" if item["continuation"] == "pending"
                else "，已保留，等待后续范围确认：\n"
            ) + "\n".join(f"- {entry['text']}" for entry in item["queued"])
        return text

    def _parts_dir(self, item):
        # Paths derive only from validated journal identity and integer revision.
        revision = item["history_revision"]
        if type(revision) is not int or revision < 0:
            raise BackgroundTaskError("Invalid result revision")
        parent = self.directory / "parts" / item["id"]
        path = parent / str(revision)
        if any(p.is_symlink() for p in (parent.parent, parent, path)):
            raise BackgroundTaskError("Result spool cannot be a symlink")
        return path

    @staticmethod
    def _private_text(path, text):
        temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
        try:
            with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _store_text(self, item, text):
        """Publish immutable parts before journalling their confirmed cursor.

        The source preserves all content. Long history uses a bounded preview and
        a local source reference, avoiding a huge model prompt on later turns.
        """
        parts = numbered_parts(text, item["task_id"], item["run_id"], self.segment_bytes)
        directory = self._parts_dir(item)
        # mkdir(parents=True, mode=0700) only applies mode to the leaf.
        for path in (directory.parent.parent, directory.parent, directory):
            path.mkdir(mode=0o700, exist_ok=True)
            if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
                raise BackgroundTaskError("Result spool must be private to this user (0700)")
        self._private_text(directory / "source.txt", text)
        for index, part in enumerate(parts):
            self._private_text(directory / f"{index}.txt", part)
        if len(parts) == 1:
            history_text = parts[0]
        else:
            preview = text[:24000]
            history_text = (
                f"后台任务 {item['task_id']} / {item['run_id']}（共 {len(parts)} 段）\n\n"
                + preview + f"\n\n完整答复保存在 {directory / 'source.txt'}；渠道按段发送，确认进度见 background_task list。"
            )
        self._update(item, text=history_text, part_count=len(parts),
                     next_part=len(parts) if item["delivery"] == "sent" else item["next_part"],
                     source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())

    async def _deliver(self, item):
        """AgentLoop sends one part; only a successful receipt advances it.

        The epoch binds receipts to one exact attempt and part. A timeout, retry
        or restart cannot let a late receipt acknowledge a different part. There
        remains a crash window after remote send and before the local save;
        only that unconfirmed part may duplicate, never all confirmed parts.
        """
        async with self.locks.setdefault(item["id"], asyncio.Lock()):
            try:
                if (not self.running or item["delivery"] != "pending"
                        or self.items.get(item["id"]) is not item or item.get("watch_error")):
                    if self.items.get(item["id"]) is item:
                        self.inflight.discard(item["id"])
                    return
                self._update(item, attempts=item["attempts"] + 1)
                index = item["next_part"]
                path = self._parts_dir(item) / f"{index}.txt"
                if path.is_symlink() or path.stat().st_size > self.segment_bytes:
                    raise BackgroundTaskError("Invalid result segment")
                text = path.read_text()
                session = self.host.sessions.get_or_create(self._key(item))
                history_key = item["id"] + "." + str(item["history_revision"])
                if not item["history_recorded"]:
                    if not any(m.get("background_event_id") == history_key for m in session.messages):
                        session.add_message("assistant", item["text"], background_event_id=history_key)
                    await self.host.sessions.save(session)
                epoch = uuid.uuid4().hex
                self._update(item, history_recorded=True, delivery_epoch=epoch,
                             receipt_deadline=time.time() + max(60, self.retry_interval * 2))

                async def receipt(success):
                    async with self.locks.setdefault(item["id"], asyncio.Lock()):
                        if (not self.running or self.items.get(item["id"]) is not item
                                or item["delivery_epoch"] != epoch or item["next_part"] != index
                                or item["id"] in self._pending_receipts):
                            return
                        self._commit_receipt(item, {"success": success, "errors": 0, "next_retry": 0})

                await self.host.bus.publish_outbound(OutboundMessage(
                    session_key=self._key(item), content=text, metadata=item["metadata"], on_delivery=receipt,
                ))
            except Exception as error:
                self.inflight.discard(item["id"])
                try:
                    self._update(item, delivery_epoch=None, receipt_deadline=0,
                                 next_delivery=time.time() + self.retry_interval,
                                 delivery="failed" if item["attempts"] >= self.max_attempts else item["delivery"])
                except Exception as journal_error:
                    # Delivery preparation also runs outside the watcher task,
                    # on AgentLoop's queue. Isolate its persistence faults here.
                    self._pause_watch(item, journal_error)
                logger.error("Background task delivery preparation failed for {} ({})",
                             item["id"], type(error).__name__)


class BackgroundTaskTool(Tool):
    """Session-scoped controls; model arguments cannot supply a destination."""

    name = "background_task"
    description = (
        "Watch external JSON task CLI jobs and send results to this conversation without another model turn. "
        "Register watch after runner start/resume; list/poll/logs/stop/enqueue/retry/ack/forget manage watches. "
        "watch_error pauses only that watch; inspect it with list and retry after repair. "
        "Queue ordinary additions; stop before cancellation, target correction or environment changes. "
        "Never place credentials in runner argv or text."
    )
    parameters = {
        "type": "object", "properties": {
            "action": {"type": "string", "enum": ["watch", "list", "poll", "logs", "stop", "enqueue", "retry", "ack", "forget"]},
            "task_id": {"type": "string"},
            "runner": {"type": "array", "items": {"type": "string"}, "description": "CLI argv prefix, before status/result/logs/stop and task_id"},
            "watch_id": {"type": "string"},
            "text": {"type": "string", "description": "User addition to queue; never auto-executed"},
        }, "required": ["action"],
    }

    def __init__(self, service):
        self.service = service

    async def execute(self, tool_context: ToolContext, action, task_id=None, runner=None,
                      watch_id=None, text=None):
        s = self.service
        try:
            if not s.running:
                raise BackgroundTaskError("Background watcher is not running; use runner status/result manually")
            if tool_context.session_key is None:
                raise BackgroundTaskError("Missing session")
            if action == "watch":
                value = await s.watch(tool_context, task_id, runner)
            elif action == "list":
                value = [s.view(i) for i in s.items.values()
                         if i["session"] == tool_context.session_key.model_dump()]
            else:
                item = s.owned(tool_context, watch_id)
                async with s.locks.setdefault(item["id"], asyncio.Lock()):
                    if action in {"poll", "logs", "stop"}:
                        if action == "stop":
                            # Persist cancellation intent before awaiting I/O.
                            # Even if this tool turn is interrupted or the stop
                            # command fails, old additions must not auto-resume.
                            s._cancel_continuation(item)
                        value = await s._call(item, "status" if action == "poll" else action)
                        # Stop only delegates to the runner's identity-checked
                        # cancellation; never kill by name or reuse --last.
                    elif action == "enqueue":
                        if item["state"] not in ACTIVE or item["text"] is not None:
                            raise BackgroundTaskError("Task is already terminal; resume with an explicit follow-up")
                        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or len(item["queued"]) >= 20:
                            raise BackgroundTaskError("Addition must be 1..4000 characters; queue limit is 20")
                        s._update(item, queued=[*item["queued"], {"text": text, "sender_id": tool_context.sender_id}])
                        value = s.view(item)
                    elif action == "retry":
                        inflight = item["id"] in s.inflight
                        if inflight and not item.get("watch_error"):
                            raise BackgroundTaskError("Delivery is already in progress")
                        proposed = {**item, "watch_error": None}
                        if proposed["paused"] and not inflight:
                            proposed.update(text=None, paused=False, probe_errors=0, history_recorded=False,
                                            part_count=0, next_part=0)
                            proposed["history_revision"] += 1
                        if not inflight and (proposed["delivery"] != "sent" or proposed["text"] is None):
                            proposed.update(delivery="pending", attempts=0, next_delivery=0,
                                            receipt_deadline=0, delivery_epoch=None, delivery_error=None)
                        # A failed explicit retry must not silently reset the
                        # in-memory budget and restart sending on the next tick.
                        s._save(proposed)
                        item.update(proposed)
                        # An isolated monitor failure may coexist with a valid
                        # queued/in-flight delivery. Restart monitoring without
                        # invalidating that message, its epoch or send budget.
                        s._wake(item)
                        value = s.view(item)
                    elif action == "ack":
                        if item["continuation"] not in {"dispatched", "cancelled"}:
                            raise BackgroundTaskError("Acknowledge additions only after they have been handed to a follow-up")
                        s._update(item, queued=[], continuation="acknowledged")
                        value = s.view(item)
                    elif action == "forget":
                        if item["state"] not in TERMINAL or item["delivery"] != "sent" or item["queued"]:
                            raise BackgroundTaskError("Only acknowledged terminal tasks with no queued additions can be forgotten")
                        sandbox = await s.host.sandbox_manager.get_sandbox(s._key(item))
                        await asyncio.wait_for(sandbox.remove_tree(
                            "tmp/wangoon/background_tasks/probes/" + item["id"]
                        ), s.command_timeout)
                        parts = s._parts_dir(item).parent
                        if parts.exists():
                            shutil.rmtree(parts)
                        (s.directory / (item["id"] + ".json")).unlink()
                        del s.items[item["id"]]
                        s._wake(item)
                        s.locks.pop(item["id"], None)
                        value = {"forgotten": watch_id}
                    else:
                        raise BackgroundTaskError("Unknown action")
            return json.dumps(value, ensure_ascii=False)
        except Exception as error:
            # Backend exceptions may include raw commands/results. Return only
            # bounded local validation errors; never persist backend stderr.
            if isinstance(error, BackgroundTaskError):
                return "Error: " + str(error)
            return "Error: background task command failed; inspect the sandbox and runner"
