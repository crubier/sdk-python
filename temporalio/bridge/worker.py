"""Worker using SDK Core."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable, List

import google.protobuf.internal.containers

import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_task
import temporalio.bridge.proto.common
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.bridge.temporal_sdk_bridge
import temporalio.common
import temporalio.converter
import temporalio.exceptions
from temporalio.bridge.temporal_sdk_bridge import PollShutdownError


@dataclass
class WorkerConfig:
    """Python representation of the Rust struct for configuring a worker."""

    namespace: str
    task_queue: str
    max_cached_workflows: int
    max_outstanding_workflow_tasks: int
    max_outstanding_activities: int
    max_outstanding_local_activities: int
    max_concurrent_wft_polls: int
    nonsticky_to_sticky_poll_ratio: float
    max_concurrent_at_polls: int
    no_remote_activities: bool
    sticky_queue_schedule_to_start_timeout_millis: int
    max_heartbeat_throttle_interval_millis: int
    default_heartbeat_throttle_interval_millis: int


class Worker:
    """SDK Core worker."""

    def __init__(
        self, client: temporalio.bridge.client.Client, config: WorkerConfig
    ) -> None:
        """Create SDK core worker with a client and config."""
        self._ref = temporalio.bridge.temporal_sdk_bridge.new_worker(
            client._ref, config
        )

    async def poll_workflow_activation(
        self,
    ) -> temporalio.bridge.proto.workflow_activation.WorkflowActivation:
        """Poll for a workflow activation."""
        return (
            temporalio.bridge.proto.workflow_activation.WorkflowActivation.FromString(
                await self._ref.poll_workflow_activation()
            )
        )

    async def poll_activity_task(
        self,
    ) -> temporalio.bridge.proto.activity_task.ActivityTask:
        """Poll for an activity task."""
        return temporalio.bridge.proto.activity_task.ActivityTask.FromString(
            await self._ref.poll_activity_task()
        )

    async def complete_workflow_activation(
        self,
        comp: temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion,
    ) -> None:
        """Complete a workflow activation."""
        await self._ref.complete_workflow_activation(comp.SerializeToString())

    async def complete_activity_task(
        self, comp: temporalio.bridge.proto.ActivityTaskCompletion
    ) -> None:
        """Complete an activity task."""
        await self._ref.complete_activity_task(comp.SerializeToString())

    def record_activity_heartbeat(
        self, comp: temporalio.bridge.proto.ActivityHeartbeat
    ) -> None:
        """Record an activity heartbeat."""
        self._ref.record_activity_heartbeat(comp.SerializeToString())

    def request_workflow_eviction(self, run_id: str) -> None:
        """Request a workflow be evicted."""
        self._ref.request_workflow_eviction(run_id)

    async def shutdown(self) -> None:
        """Shutdown the worker, waiting for completion."""
        await self._ref.shutdown()

    async def finalize_shutdown(self) -> None:
        """Finalize the worker.

        This will fail if shutdown hasn't completed fully due to internal
        reference count checks.
        """
        ref = self._ref
        self._ref = None
        await ref.finalize_shutdown()


def retry_policy_from_proto(
    p: temporalio.bridge.proto.common.RetryPolicy,
) -> temporalio.common.RetryPolicy:
    """Convert Core retry policy to Temporal retry policy."""
    return temporalio.common.RetryPolicy(
        initial_interval=p.initial_interval.ToTimedelta(),
        backoff_coefficient=p.backoff_coefficient,
        maximum_interval=p.maximum_interval.ToTimedelta()
        if p.HasField("maximum_interval")
        else None,
        maximum_attempts=p.maximum_attempts,
        non_retryable_error_types=p.non_retryable_error_types,
    )


def retry_policy_to_proto(
    p: temporalio.common.RetryPolicy,
    v: temporalio.bridge.proto.common.RetryPolicy,
) -> None:
    """Convert Temporal retry policy to Core retry policy."""
    v.initial_interval.FromTimedelta(p.initial_interval)
    v.backoff_coefficient = p.backoff_coefficient
    if p.maximum_interval:
        v.maximum_interval.FromTimedelta(p.maximum_interval)
    v.maximum_attempts = p.maximum_attempts
    if p.non_retryable_error_types:
        v.non_retryable_error_types.extend(p.non_retryable_error_types)


def from_bridge_payloads(
    payloads: Iterable[temporalio.bridge.proto.common.Payload],
) -> List[temporalio.api.common.v1.Payload]:
    """Convert from bridge payloads to API payloads."""
    return [from_bridge_payload(p) for p in payloads]


def from_bridge_payload(
    payload: temporalio.bridge.proto.common.Payload,
) -> temporalio.api.common.v1.Payload:
    """Convert from a bridge payload to an API payload."""
    return temporalio.api.common.v1.Payload(
        metadata=payload.metadata, data=payload.data
    )


def to_bridge_payloads(
    payloads: Iterable[temporalio.api.common.v1.Payload],
) -> List[temporalio.bridge.proto.common.Payload]:
    """Convert from API payloads to bridge payloads."""
    return [to_bridge_payload(p) for p in payloads]


def to_bridge_payload(
    payload: temporalio.api.common.v1.Payload,
) -> temporalio.bridge.proto.common.Payload:
    """Convert from an API payload to a bridge payload."""
    return temporalio.bridge.proto.common.Payload(
        metadata=payload.metadata, data=payload.data
    )


# See https://mypy.readthedocs.io/en/stable/runtime_troubles.html#using-classes-that-are-generic-in-stubs-but-not-at-runtime
if TYPE_CHECKING:
    BridgePayloadContainer = (
        google.protobuf.internal.containers.RepeatedCompositeFieldContainer[
            temporalio.bridge.proto.common.Payload
        ]
    )
else:
    BridgePayloadContainer = (
        google.protobuf.internal.containers.RepeatedCompositeFieldContainer
    )


async def _apply_to_bridge_payloads(
    payloads: BridgePayloadContainer,
    cb: Callable[
        [Iterable[temporalio.api.common.v1.Payload]],
        Awaitable[List[temporalio.api.common.v1.Payload]],
    ],
) -> None:
    """Apply API payload callback to bridge payloads."""
    if len(payloads) == 0:
        return
    new_payloads = to_bridge_payloads(await cb(from_bridge_payloads(payloads)))
    del payloads[:]
    # TODO(cretz): Copy too expensive?
    payloads.extend(new_payloads)


async def _apply_to_bridge_payload(
    payload: temporalio.bridge.proto.common.Payload,
    cb: Callable[
        [Iterable[temporalio.api.common.v1.Payload]],
        Awaitable[List[temporalio.api.common.v1.Payload]],
    ],
) -> None:
    """Apply API payload callback to bridge payload."""
    new_payload = (await cb([from_bridge_payload(payload)]))[0]
    payload.metadata.clear()
    payload.metadata.update(new_payload.metadata)
    payload.data = new_payload.data


async def _decode_bridge_payloads(
    payloads: BridgePayloadContainer,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Decode bridge payloads with the given codec."""
    return await _apply_to_bridge_payloads(payloads, codec.decode)


async def _decode_bridge_payload(
    payload: temporalio.bridge.proto.common.Payload,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Decode a bridge payload with the given codec."""
    return await _apply_to_bridge_payload(payload, codec.decode)


async def _encode_bridge_payloads(
    payloads: BridgePayloadContainer,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Encode bridge payloads with the given codec."""
    return await _apply_to_bridge_payloads(payloads, codec.encode)


async def _encode_bridge_payload(
    payload: temporalio.bridge.proto.common.Payload,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Decode a bridge payload with the given codec."""
    return await _apply_to_bridge_payload(payload, codec.encode)


async def decode_activation(
    act: temporalio.bridge.proto.workflow_activation.WorkflowActivation,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Decode the given activation with the codec."""
    for job in act.jobs:
        if job.HasField("cancel_workflow"):
            await _decode_bridge_payloads(job.cancel_workflow.details, codec)
        elif job.HasField("query_workflow"):
            await _decode_bridge_payloads(job.query_workflow.arguments, codec)
        elif job.HasField("resolve_activity"):
            if job.resolve_activity.result.HasField("cancelled"):
                await temporalio.exceptions.decode_failure(
                    job.resolve_activity.result.cancelled.failure, codec
                )
            elif job.resolve_activity.result.HasField("completed"):
                if job.resolve_activity.result.completed.HasField("result"):
                    await _decode_bridge_payload(
                        job.resolve_activity.result.completed.result, codec
                    )
            elif job.resolve_activity.result.HasField("failed"):
                await temporalio.exceptions.decode_failure(
                    job.resolve_activity.result.failed.failure, codec
                )
        elif job.HasField("resolve_child_workflow_execution"):
            if job.resolve_child_workflow_execution.result.HasField("cancelled"):
                await temporalio.exceptions.decode_failure(
                    job.resolve_child_workflow_execution.result.cancelled.failure, codec
                )
            elif job.resolve_child_workflow_execution.result.HasField(
                "completed"
            ) and job.resolve_child_workflow_execution.result.completed.HasField(
                "result"
            ):
                await _decode_bridge_payload(
                    job.resolve_child_workflow_execution.result.completed.result, codec
                )
            elif job.resolve_child_workflow_execution.result.HasField("failed"):
                await temporalio.exceptions.decode_failure(
                    job.resolve_child_workflow_execution.result.failed.failure, codec
                )
        elif job.HasField("resolve_child_workflow_execution_start"):
            if job.resolve_child_workflow_execution_start.HasField("cancelled"):
                await temporalio.exceptions.decode_failure(
                    job.resolve_child_workflow_execution_start.cancelled.failure, codec
                )
        elif job.HasField("resolve_request_cancel_external_workflow"):
            if job.resolve_request_cancel_external_workflow.HasField("failure"):
                await temporalio.exceptions.decode_failure(
                    job.resolve_request_cancel_external_workflow.failure, codec
                )
        elif job.HasField("resolve_signal_external_workflow"):
            if job.resolve_signal_external_workflow.HasField("failure"):
                await temporalio.exceptions.decode_failure(
                    job.resolve_signal_external_workflow.failure, codec
                )
        elif job.HasField("signal_workflow"):
            await _decode_bridge_payloads(job.signal_workflow.input, codec)
        elif job.HasField("start_workflow"):
            await _decode_bridge_payloads(job.start_workflow.arguments, codec)
            if job.start_workflow.HasField("continued_failure"):
                await temporalio.exceptions.decode_failure(
                    job.start_workflow.continued_failure, codec
                )
            for val in job.start_workflow.memo.fields.values():
                # This uses API payload not bridge payload
                new_payload = (await codec.decode([val]))[0]
                val.metadata.clear()
                val.metadata.update(new_payload.metadata)
                val.data = new_payload.data


async def encode_completion(
    comp: temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion,
    codec: temporalio.converter.PayloadCodec,
) -> None:
    """Recursively encode the given completion with the codec."""
    if comp.HasField("failed"):
        await temporalio.exceptions.encode_failure(comp.failed.failure, codec)
    elif comp.HasField("successful"):
        for command in comp.successful.commands:
            if command.HasField("complete_workflow_execution"):
                if command.complete_workflow_execution.HasField("result"):
                    await _encode_bridge_payload(
                        command.complete_workflow_execution.result, codec
                    )
            elif command.HasField("continue_as_new_workflow_execution"):
                await _encode_bridge_payloads(
                    command.continue_as_new_workflow_execution.arguments, codec
                )
                for val in command.continue_as_new_workflow_execution.memo.values():
                    await _encode_bridge_payload(val, codec)
            elif command.HasField("fail_workflow_execution"):
                await temporalio.exceptions.encode_failure(
                    command.fail_workflow_execution.failure, codec
                )
            elif command.HasField("respond_to_query"):
                if command.respond_to_query.HasField("failed"):
                    await temporalio.exceptions.encode_failure(
                        command.respond_to_query.failed, codec
                    )
                elif command.respond_to_query.HasField(
                    "succeeded"
                ) and command.respond_to_query.succeeded.HasField("response"):
                    await _encode_bridge_payload(
                        command.respond_to_query.succeeded.response, codec
                    )
            elif command.HasField("schedule_activity"):
                await _encode_bridge_payloads(
                    command.schedule_activity.arguments, codec
                )
            elif command.HasField("schedule_local_activity"):
                await _encode_bridge_payloads(
                    command.schedule_local_activity.arguments, codec
                )
            elif command.HasField("signal_external_workflow_execution"):
                await _encode_bridge_payloads(
                    command.signal_external_workflow_execution.args, codec
                )
            elif command.HasField("start_child_workflow_execution"):
                await _encode_bridge_payloads(
                    command.start_child_workflow_execution.input, codec
                )
                for val in command.start_child_workflow_execution.memo.values():
                    await _encode_bridge_payload(val, codec)
